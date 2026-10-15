#!/usr/bin/env python3
import base64
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
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
                patch.object(provision.getpass, 'getpass') as getpass, self.assertRaises(ValueError):
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

    def test_classic_pat_is_refused_before_api_or_secret_access(self):
        with patch.object(provision, 'require_stopped'), \
                patch.object(provision.getpass, 'getpass', side_effect=['shared-user', 'work/project', 'ghp_classic-fixture']), \
                patch.object(provision, 'request') as request, patch.object(provision, 'kubectl') as kubectl, \
                self.assertRaises(ValueError):
            provision.github()
        request.assert_not_called()
        kubectl.assert_not_called()

    def test_org_pat_patches_only_github_key_with_concurrency_check(self):
        token = 'github_pat_' + 'f' * 30
        obj = {'metadata': {'resourceVersion': '5'}, 'data': {'model-token': 'bQ==', 'hub-token': 'aA=='}}
        with patch.object(provision, 'require_stopped'), \
                patch.object(provision.getpass, 'getpass', side_effect=['shared-user', 'work/project', token]), \
                patch('builtins.input', return_value='yes'), \
                patch.object(provision, 'request', side_effect=[{'login': 'shared-user'},
                    {'full_name': 'work/project', 'owner': {'type': 'Organization'}, 'private': True}]), \
                patch.object(provision, 'kubectl', return_value=obj) as kubectl:
            provision.github()
        operations = json.loads(kubectl.call_args.kwargs['payload'])
        self.assertEqual(operations[0], {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '5'})
        self.assertEqual(operations[1]['path'], '/data/github-token')
        self.assertEqual(base64.b64decode(operations[1]['value']).decode(), token)
        self.assertEqual(len(operations), 2)

    def test_personal_or_public_repository_is_refused(self):
        for owner_type, private in (('User', True), ('Organization', False), ('Organization', None)):
            with self.subTest(owner_type=owner_type, private=private), \
                    patch.object(provision, 'require_stopped'), \
                    patch.object(provision.getpass, 'getpass', side_effect=['shared-user', 'work/project', 'github_pat_' + 'f' * 30]), \
                    patch.object(provision, 'request', side_effect=[{'login': 'shared-user'},
                        {'full_name': 'work/project', 'owner': {'type': owner_type}, 'private': private}]), \
                    patch.object(provision, 'kubectl') as kubectl, self.assertRaises(ValueError):
                provision.github()
            kubectl.assert_not_called()

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
