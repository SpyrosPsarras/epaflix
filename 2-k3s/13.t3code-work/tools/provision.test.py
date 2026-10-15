#!/usr/bin/env python3
import base64
import copy
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('provision', Path(__file__).with_name('provision.py'))
provision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provision)


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(provision.sys.stdin, 'isatty', return_value=True))
        self.enterContext(patch.object(provision.sys.stdout, 'isatty', return_value=True))

    def test_proxy_reload_gets_a_bounded_discovery_retry(self):
        delayed = provision.urllib.error.HTTPError(provision.MODEL_URL + '/v1/models', 401, 'reload', {}, None)
        with patch.object(provision, 'read_secret', return_value={'management-password': b'fixture'}), \
                patch.object(provision, 'request', side_effect=[{'api-keys': []}, {}, delayed, {'data': []}]) as request, \
                patch.object(provision.time, 'sleep') as sleep:
            provision.register_model(b'fixture-client')
        self.assertEqual(request.call_count, 4)
        sleep.assert_called_once_with(1)

    def test_noninteractive_owner_enrollment_refuses_before_reading_a_token(self):
        with patch.object(provision.sys.stdin, 'isatty', return_value=False), \
                patch.object(provision.getpass, 'getpass') as getpass, \
                self.assertRaisesRegex(ValueError, '^Owner enrollment requires a private interactive terminal$'):
            provision.github()
        getpass.assert_not_called()

    def bootstrap(self):
        stored = {}

        def create(namespace_name, name, data):
            self.assertNotIn((namespace_name, name), stored)
            stored[namespace_name, name] = data
            return data

        self.enterContext(patch.object(provision, 'read_secret', side_effect=lambda *key: stored.get(key)))
        self.create_secret = self.enterContext(patch.object(provision, 'create_secret', side_effect=create))
        self.enterContext(patch.object(provision, 'namespace'))
        self.register_model = self.enterContext(patch.object(provision, 'register_model'))
        provision.grants()
        return stored

    def test_bootstrap_mints_independent_grants_and_exact_search_inventory(self):
        stored = self.bootstrap()
        first = copy.deepcopy(stored)
        provision.grants()
        self.assertEqual(first, stored)
        runtime = stored[provision.RUNTIME_GRANTS]
        self.assertEqual(set(runtime), {'model-token', 'hub-token'})
        self.assertTrue(runtime['hub-token'].startswith(b'work.'))
        self.assertNotEqual(runtime['model-token'], runtime['hub-token'])
        backend = stored['mcp-hub-work', 'mcp-hub-searxng']['token']
        self.assertNotEqual(backend, runtime['hub-token'])
        self.assertFalse(backend.startswith(b'work.'))
        hub = stored['mcp-hub-work', 'mcp-hub-instance']
        self.assertEqual(json.loads(hub['services.json']), {'environment': 'work', 'services': ['searxng']})
        credentials = json.loads(hub['credentials.json'])
        self.assertEqual(credentials['clients'], {'t3code-work': provision.digest(runtime['hub-token'])})
        self.assertEqual(credentials['upstreams']['searxng']['token'].encode(), backend)
        self.register_model.assert_called_with(runtime['model-token'])

    def test_foreign_clients_upstreams_and_inventory_survive_reruns(self):
        stored = self.bootstrap()
        hub = stored['mcp-hub-work', 'mcp-hub-instance']
        credentials = json.loads(hub['credentials.json'])
        credentials['clients'].update(homepc='foreign-homepc-digest', laptop='foreign-laptop-digest')
        credentials['upstreams']['drive'] = {'url': 'https://work.example.test/mcp', 'token': 'foreign-fixture'}
        hub['credentials.json'] = json.dumps(credentials).encode()
        hub['services.json'] = json.dumps({'environment': 'work', 'services': ['searxng', 'drive']}).encode()
        first = copy.deepcopy(stored)
        self.create_secret.reset_mock()
        self.register_model.reset_mock()
        provision.grants()
        self.assertEqual(stored, first)
        self.create_secret.assert_not_called()
        self.register_model.assert_called_once_with(stored[provision.RUNTIME_GRANTS]['model-token'])

    def test_changed_owned_entries_refuse_without_rotation(self):
        stored = self.bootstrap()
        hub = stored['mcp-hub-work', 'mcp-hub-instance']
        original = json.loads(hub['credentials.json'])
        for group, name in (('clients', 't3code-work'), ('upstreams', 'searxng')):
            with self.subTest(group=group):
                credentials = copy.deepcopy(original)
                credentials[group][name] = 'changed-owned-entry'
                hub['credentials.json'] = json.dumps(credentials).encode()
                self.create_secret.reset_mock()
                self.register_model.reset_mock()
                with self.assertRaises(ValueError):
                    provision.grants()
                self.create_secret.assert_not_called()
                self.register_model.assert_not_called()

    def test_register_model_appends_without_replacing_other_client_keys(self):
        with patch.object(provision, 'read_secret', return_value={'management-password': b'private-management'}), \
                patch.object(provision, 'request', side_effect=[{'api-keys': ['other-client']}, {}, {'data': []}]) as request:
            provision.register_model(b'work-client')
        calls = request.call_args_list
        self.assertEqual(calls[1].args[2], 'PATCH')
        self.assertEqual(calls[1].args[3], {'old': '', 'new': 'work-client'})
        self.assertTrue(calls[2].args[0].endswith('/v1/models'))

    def test_attribution_prefix_collision_refuses_without_printing_or_mutating_keys(self):
        for current in (['t3-work-other-fixture'], ['t3-wrong-other-fixture']):
            with self.subTest(current=current), \
                    patch.object(provision, 'read_secret', return_value={'management-password': b'fixture'}), \
                    patch.object(provision, 'request', return_value={'api-keys': current}) as request, \
                    self.assertRaisesRegex(ValueError, '^Another client key shares the work attribution prefix; refusing registration$'):
                provision.register_model(b't3-work-own-fixture')
            self.assertEqual(request.call_count, 1)

    def test_existing_own_key_is_idempotent_and_homelab_prefix_does_not_collide(self):
        current = ['t3-work-own-fixture', 't3-homelab-other-fixture']
        with patch.object(provision, 'read_secret', return_value={'management-password': b'fixture'}), \
                patch.object(provision, 'request', side_effect=[{'api-keys': current}, {'data': []}]) as request:
            provision.register_model(b't3-work-own-fixture')
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(call.args[2:] == () for call in request.call_args_list))

    def enroll(self, responses=None, cluster=None, token=None):
        self.token = token if token is not None else 'github_pat_' + 'f' * 30
        self.grant = {'metadata': {'resourceVersion': '5'},
                      'data': {'model-token': 'bQ==', 'hub-token': 'aA=='}}
        self.repo = {'name': 'private-repository-name', 'owner': {'type': 'Organization', 'login': 'private-org-name'},
                     'private': True}
        self.prompt = self.enterContext(patch.object(provision.getpass, 'getpass', return_value=self.token))
        self.confirm = self.enterContext(patch('builtins.input', side_effect=AssertionError('No confirmation needed')))
        self.api = self.enterContext(patch.object(provision, 'request', side_effect=(
            responses if responses is not None else [{'login': 'private-account-name'},
                [{'login': 'private-org-name'}], [self.repo], []])))
        self.cluster = self.enterContext(patch.object(provision, 'kubectl', side_effect=(
            cluster if cluster is not None else [{'items': []}, self.grant, self.grant])))
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(provision.sys, 'argv', ['provision.py', 'github']), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
                patch.object(output, 'isatty', return_value=True):
            status = provision.main()
        self.output = output.getvalue()
        combined = self.output + errors.getvalue()
        for value in (self.token, 'private-account-name', 'private-repository-name', 'private-org-name'):
            if value:
                self.assertNotIn(value, combined)
        return status, errors.getvalue().strip()

    def test_token_only_flow_patches_only_github_key_with_concurrency_check(self):
        self.assertEqual(self.enroll(), (0, ''))
        self.prompt.assert_called_once_with('Fine-grained GitHub PAT, hidden: ')
        self.confirm.assert_not_called()
        self.assertIn('Work github staged. No Applications synced.', self.output)
        self.assertEqual(self.api.call_args_list[0].args[0], 'https://api.github.com/user')
        self.assertEqual(self.api.call_args_list[1].args[0], 'https://api.github.com/user/orgs?per_page=100&page=1')
        self.assertEqual(self.api.call_args_list[2].args[0],
                         'https://api.github.com/orgs/private-org-name/repos?type=private&per_page=100&page=1')
        self.assertEqual(self.api.call_args_list[3].args[0],
                         'https://api.github.com/user/repos?visibility=private&affiliation=owner&per_page=100&page=1')
        operations = json.loads(self.cluster.call_args.kwargs['payload'])
        self.assertEqual(operations[0], {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '5'})
        self.assertEqual(operations[1]['path'], '/data/github-token')
        self.assertEqual(base64.b64decode(operations[1]['value']).decode(), self.token)
        self.assertEqual(len(operations), 2)

    def test_invalid_or_classic_pat_has_a_safe_message(self):
        for token in ('ghp_classic-fixture', 'github_pat_short', 'not-a-token', ''):
            with self.subTest(token=token):
                self.assertEqual(self.enroll(token=token), (1,
                    'Token is not a fine-grained PAT (must start with github_pat_)'))
                self.api.assert_not_called()
                self.assertEqual(self.cluster.call_count, 2)

    def test_github_http_failures_show_status_without_response_details(self):
        for code in (401, 403, 429):
            with self.subTest(code=code):
                failure = provision.urllib.error.HTTPError('https://api.github.com/private-org-name', code,
                    'private-repository-name', {}, None)
                self.assertEqual(self.enroll(responses=[failure]),
                                 (1, f'GitHub rejected the token (HTTP {code})'))
                self.assertEqual(self.cluster.call_count, 2)

    def test_github_transport_and_invalid_response_messages(self):
        cases = [(provision.urllib.error.URLError('private-org-name'),
                  'Cannot reach GitHub: check your network connection'),
                 (TimeoutError('private-repository-name'),
                  'Cannot reach GitHub: check your network connection'),
                 (ValueError('private-account-name'),
                  'GitHub returned an unreadable response: retry later')]
        for failure, message in cases:
            with self.subTest(message=message):
                self.assertEqual(self.enroll(responses=[failure]), (1, message))

    def github_fixture(self):
        user = {'login': 'private-account-name'}
        orgs = [{'login': 'private-org-name'}]
        personal = {'name': 'private-repository-name',
                    'owner': {'type': 'User', 'login': user['login']}, 'private': False}
        organization = {'name': 'private-repository-name',
                        'owner': {'type': 'Organization', 'login': orgs[0]['login']}, 'private': False}
        visible = [copy.deepcopy(personal) for _ in range(23)] + [organization]
        private = [{**copy.deepcopy(organization), 'private': True} for _ in range(49)]
        return user, orgs, visible, private

    def test_real_response_shape_allows_public_personal_repos_and_separate_private_org_listing(self):
        user, orgs, visible, private = self.github_fixture()
        unavailable = provision.urllib.error.HTTPError('https://api.github.com/user/orgs', 403, 'fixture', {}, None)
        self.assertEqual(self.enroll(responses=[user, unavailable, visible, private, []]), (0, ''))
        self.assertIn('affiliation=owner,collaborator,organization_member', self.api.call_args_list[2].args[0])
        self.assertIn('/orgs/private-org-name/repos?type=private', self.api.call_args_list[3].args[0])
        self.assertEqual(self.enroll(responses=[user, orgs, private, []]), (0, ''))
        self.assertFalse(any('collaborator' in call.args[0] for call in self.api.call_args_list))

    def test_unavailable_private_personal_enumeration_is_accepted_after_org_access(self):
        user, orgs, visible, private = self.github_fixture()
        for code in (403, 404, 422):
            with self.subTest(code=code):
                unavailable = provision.urllib.error.HTTPError('https://api.github.com/private-org-name', code,
                    'private-repository-name', {}, None)
                self.assertEqual(self.enroll(responses=[user, orgs, private, unavailable]), (0, ''))
                self.assertIn("Private personal repository check unavailable: not verified. Check the token's resource owner on the PAT page.", self.output)

    def test_readable_private_personal_repos_are_refused_on_every_page(self):
        user, orgs, visible, private = self.github_fixture()
        personal = [{**visible[0], 'private': True}]
        message = 'Private personal repository access is readable: create the token with the organization as resource owner'
        self.assertEqual(self.enroll(responses=[user, orgs, private, personal]), (1, message))
        self.assertEqual(self.cluster.call_count, 2)
        self.assertEqual(self.enroll(responses=[user, orgs, private, visible * 4 + visible[:4], personal]), (1, message))
        self.assertTrue(self.api.call_args_list[-1].args[0].endswith('page=2'))
        unavailable = provision.urllib.error.HTTPError('https://api.github.com/user/orgs', 403, 'fixture', {}, None)
        self.assertEqual(self.enroll(responses=[user, unavailable, personal]), (1, message))
        unavailable = provision.urllib.error.HTTPError('https://api.github.com/user/repos', 403, 'fixture', {}, None)
        self.assertEqual(self.enroll(responses=[user, orgs, private, personal * 100, unavailable]), (1, message))

    def test_empty_readable_membership_list_does_not_fall_back_to_visible_repos(self):
        user, orgs, visible, private = self.github_fixture()
        self.assertEqual(self.enroll(responses=[user, []]),
                         (1, 'Token organization could not be determined: check organization access and token approval'))
        self.assertEqual(self.api.call_count, 2)
        self.assertEqual(self.cluster.call_count, 2)

    def test_multiple_memberships_select_only_the_org_with_private_access(self):
        user, orgs, visible, private = self.github_fixture()
        memberships = [{'login': 'another-org'}, *orgs]
        self.assertEqual(self.enroll(responses=[user, memberships, [], private, []]), (0, ''))
        for code in (403, 404, 422):
            with self.subTest(code=code):
                unavailable = provision.urllib.error.HTTPError('https://api.github.com/private-org-name', code,
                                                              'private-repository-name', {}, None)
                self.assertEqual(self.enroll(responses=[user, memberships, unavailable, private, []]), (0, ''))
        self.assertEqual(self.enroll(responses=[user, orgs * 100, memberships[:1], [], private, []]), (0, ''))
        self.assertIn('user/orgs?per_page=100&page=2', self.api.call_args_list[2].args[0])

    def test_private_access_in_multiple_orgs_is_ambiguous_and_no_access_is_refused(self):
        user, orgs, visible, private = self.github_fixture()
        memberships = [{'login': 'another-org'}, *orgs]
        other_private = [{**private[0], 'owner': {'type': 'Organization', 'login': 'another-org'}}]
        self.assertEqual(self.enroll(responses=[user, memberships, other_private, private]),
                         (1, 'Token organization is ambiguous: use a token for one organization'))
        self.assertEqual(self.cluster.call_count, 2)
        self.assertEqual(self.enroll(responses=[user, memberships, [], []]), (1,
                         'Private organization repository access is empty: approve the token request or select a private organization repository'))
        self.assertEqual(self.cluster.call_count, 2)

    def test_failed_membership_lookup_uses_visible_orgs_and_checks_private_access(self):
        user, orgs, visible, private = self.github_fixture()
        unavailable = provision.urllib.error.HTTPError('https://api.github.com/user/orgs', 403, 'fixture', {}, None)
        other_visible = {**visible[-1], 'owner': {'type': 'Organization', 'login': 'another-org'}}
        self.assertEqual(self.enroll(responses=[user, unavailable, [*visible, other_visible], [], private, []]), (0, ''))

    def test_org_lookup_empty_and_malformed_responses(self):
        user, orgs, visible, private = self.github_fixture()
        unavailable = provision.urllib.error.HTTPError('https://api.github.com/user/orgs', 403, 'fixture', {}, None)
        cases = [([user, unavailable, []], 'Token organization could not be determined: check organization access and token approval'),
                 ([user, [{}]], 'GitHub returned incomplete organization details: retry later'),
                 ([user, [{'login': 'invalid/org'}]], 'GitHub returned incomplete organization details: retry later'),
                 ([user, {'unexpected': 'private-org-name'}], 'GitHub returned an unexpected organization lookup response: retry later'),
                 ([user, unavailable, [{}]], 'GitHub returned incomplete repository details: retry later'),
                 ([user, unavailable, [{**visible[-1], 'owner': {'type': 'Organization', 'login': 'invalid/org'}}]],
                  'GitHub returned incomplete organization details: retry later')]
        for responses, message in cases:
            with self.subTest(message=message):
                self.assertEqual(self.enroll(responses=responses), (1, message))
                self.assertEqual(self.cluster.call_count, 2)
        self.assertEqual(self.enroll(responses=[{}]), (1, 'GitHub did not return a user for this token'))
        self.assertEqual(self.enroll(responses=[user, None]),
                         (1, 'GitHub returned an unreadable response: retry later'))

    def test_private_org_access_empty_public_only_or_rejected_has_check_specific_message(self):
        user, orgs, visible, private = self.github_fixture()
        message = 'Private organization repository access is empty: approve the token request or select a private organization repository'
        for repos in ([], [visible[-1]]):
            with self.subTest(repos=repos):
                self.assertEqual(self.enroll(responses=[user, orgs, repos]), (1, message))
        failure = provision.urllib.error.HTTPError('https://api.github.com/private-org-name', 403,
                                                  'private-repository-name', {}, None)
        self.assertEqual(self.enroll(responses=[user, orgs, failure]),
                         (1, 'GitHub rejected the private organization repository check (HTTP 403)'))
        failure = provision.urllib.error.HTTPError('https://api.github.com/user/repos', 429, 'fixture', {}, None)
        self.assertEqual(self.enroll(responses=[user, orgs, private, failure]),
                         (1, 'GitHub rejected the private personal repository check (HTTP 429)'))

    def test_all_org_private_repository_pages_are_checked(self):
        user, orgs, visible, private = self.github_fixture()
        self.assertEqual(self.enroll(responses=[user, orgs, private * 2 + private[:2], private[:1], []]), (0, ''))
        self.assertTrue(self.api.call_args_list[3].args[0].endswith('page=2'))

    def test_running_pod_missing_secret_and_existing_grant_fail_before_prompt(self):
        incomplete = {'metadata': {'resourceVersion': '5'}, 'data': {}}
        existing = {'metadata': {'resourceVersion': '5'}, 'data': {'github-token': 'Zg=='}}
        cases = [([{'items': [{}]}], 'Work pod is running: stop it first'),
                 ([{'items': []}, None], 'Work grants Secret is missing: run provision.py grants first'),
                 ([{'items': []}, incomplete], 'Work grants Secret is incomplete: provision the model and hub grants first'),
                 ([{'items': []}, existing], 'Work GitHub grant already exists: refusing to overwrite it')]
        for cluster, message in cases:
            with self.subTest(message=message):
                self.assertEqual(self.enroll(cluster=cluster), (1, message))
                self.prompt.assert_not_called()
                self.api.assert_not_called()

    def test_cluster_failure_messages_suppress_raw_diagnostics(self):
        cases = [(b'Unable to connect to the server: private-org-name',
                  'Cluster unreachable: check kubectl context epaflix'),
                 (b'error: context "epaflix" does not exist: private-org-name',
                  'Cluster unreachable: check kubectl context epaflix'),
                 (b'Error from server (Forbidden): private-repository-name',
                  'Cluster access denied: check permissions for kubectl context epaflix'),
                 (b'Error from server (Unauthorized): private-account-name',
                  'Cluster authentication failed: refresh access for kubectl context epaflix'),
                 (b'testing value /metadata/resourceVersion failed: test failed private-org-name',
                  'Cluster state changed: rerun the command; no existing grant was overwritten'),
                 (b'Error from server (NotFound): private-org-name',
                  'Cluster request failed: check kubectl context epaflix and permissions'),
                 (b'private-repository-name',
                  'Cluster request failed: check kubectl context epaflix and permissions')]
        for raw, message in cases:
            with self.subTest(message=message), \
                    patch.object(provision.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'', raw)), \
                    self.assertRaisesRegex(ValueError, '^' + message + '$'):
                provision.kubectl('get', 'pods')
        for failure, message in [(FileNotFoundError(), 'kubectl is not installed: install it and check context epaflix'),
                                 (subprocess.TimeoutExpired('kubectl', 60), 'Cluster unreachable: check kubectl context epaflix')]:
            with self.subTest(message=message), patch.object(provision.subprocess, 'run', side_effect=failure), \
                    self.assertRaisesRegex(ValueError, '^' + message + '$'):
                provision.kubectl('get', 'pods')
        with patch.object(provision.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'private-org-name', b'')), \
                self.assertRaisesRegex(ValueError, '^Cluster returned an unreadable response: check kubectl context epaflix$'):
            provision.kubectl('get', 'pods')

    def test_safe_cluster_errors_are_printed_and_unexpected_exceptions_are_not(self):
        message = 'Cluster unreachable: check kubectl context epaflix'
        self.assertEqual(self.enroll(cluster=provision.ProvisionError(message)), (1, message))
        self.assertEqual(self.enroll(cluster=RuntimeError('private-org-name')), (1,
                         'Work provisioning refused or failed. Private diagnostics suppressed.'))

    def test_deliberate_value_errors_keep_refused_or_failed_wording(self):
        for action in ('grants', 'inputs'):
            with self.subTest(action=action), \
                    patch.object(provision.sys, 'argv', ['provision.py', action, '--directory', '/fixture']), \
                    patch.object(provision, action, side_effect=ValueError('private-repository-name')), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(provision.main(), 1)
                self.assertEqual(errors.getvalue().strip(),
                                 'Work provisioning refused or failed. Private diagnostics suppressed.')

    def test_reviewed_inputs_preserve_policies_and_only_bind_new_grant_digests(self):
        fixture_path = Path(__file__).resolve().parents[2] / '13.t3code/env/tools/work-runtime.test.py'
        spec = importlib.util.spec_from_file_location('work_fixture', fixture_path)
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'inputs'
            fixture.fixture(directory)
            kube = json.loads((directory / 'kubeconfig').read_bytes())
            for group in ('clusters', 'contexts', 'users'):
                kube[group] = kube[group][:1]
            (directory / 'kubeconfig').write_text(json.dumps(kube))
            auth = json.loads((directory / 'work-auth.json').read_bytes())
            auth['contexts'] = auth['contexts'][:1]
            (directory / 'work-auth.json').write_text(json.dumps(auth))
            config = json.loads((directory / 'runtime.json').read_bytes())
            config.update(modelUrl=provision.MODEL_URL, gitAuth='github-token')
            (directory / 'runtime.json').write_text(json.dumps(config))
            fixture.refresh(directory)
            runtime_grants = {'model-token': b'fixture-model', 'hub-token': ('work.' + 'h' * 43).encode(),
                              'github-token': b'github_pat_' + b'f' * 30}
            with patch.object(provision, 'require_stopped'), \
                    patch.object(provision, 'read_secret', return_value=runtime_grants), \
                    patch.object(provision, 'create_secret') as create:
                provision.inputs(directory)
            stored = create.call_args.args[2]
            self.assertEqual(stored['policies.json'], (directory / 'policies.json').read_bytes())
            self.assertEqual(stored['bundle.json'], (directory / 'bundle.json').read_bytes())
            self.assertEqual(json.loads(stored['runtime.json'])['grantSha256'],
                             {name: provision.digest(value) for name, value in runtime_grants.items()})
            config = json.loads((directory / 'runtime.json').read_bytes())
            config['modelUrl'] += '/v1'
            (directory / 'runtime.json').write_text(json.dumps(config))
            with patch.object(provision, 'require_stopped'), patch.object(provision, 'create_secret') as create, self.assertRaises(ValueError):
                provision.inputs(directory)
            create.assert_not_called()
            config['modelUrl'] = provision.MODEL_URL
            (directory / 'runtime.json').write_text(json.dumps(config))
            auth['contexts'].append(auth['contexts'][0])
            (directory / 'work-auth.json').write_text(json.dumps(auth))
            with patch.object(provision, 'require_stopped'), patch.object(provision, 'create_secret') as create, self.assertRaises(ValueError):
                provision.inputs(directory)
            create.assert_not_called()

    def test_secret_writer_uses_stdin_and_create_not_apply(self):
        with patch.object(provision, 'kubectl', return_value={}) as kubectl:
            provision.create_secret(*provision.RUNTIME_GRANTS, {'model-token': b'private-fixture'})
        args, kwargs = kubectl.call_args
        self.assertEqual(args, ('create', '-f', '-', '-o', 'json'))
        payload = json.loads(kwargs['payload'])
        self.assertEqual(base64.b64decode(payload['data']['model-token']), b'private-fixture')
        self.assertNotIn('annotations', payload['metadata'])


if __name__ == '__main__':
    unittest.main()
