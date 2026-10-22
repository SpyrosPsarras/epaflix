#!/usr/bin/env python3
import base64
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('manager_kubeconfig', Path(__file__).with_name('manager-kubeconfig.py'))
TOKEN = b'synthetic-manager-credential'
CA = b'synthetic-manager-certificate'
DIAGNOSTIC = b'synthetic-private-kubectl-diagnostic'


def encoded(value):
    return base64.b64encode(value).decode()


def expected_config():
    return {'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'manager',
            'clusters': [{'name': 'manager', 'cluster': {
                'server': 'https://kubernetes.default.svc',
                'certificate-authority-data': encoded(CA)}}],
            'users': [{'name': 'manager-agent', 'user': {'token': TOKEN.decode()}}],
            'contexts': [{'name': 'manager', 'context': {'cluster': 'manager', 'user': 'manager-agent'}}]}


class FakeKubectl:
    def __init__(self):
        self.resources = {('serviceaccount', 'manager-agent'): {
            'apiVersion': 'v1', 'kind': 'ServiceAccount',
            'metadata': {'name': 'manager-agent', 'namespace': 't3code'},
            'automountServiceAccountToken': False}}
        self.calls = []
        self.created = []
        self.populate = True
        self.delay = 0
        self.polls = 0
        self.fail = False
        self.fail_create = False
        self.timeout = False

    def __call__(self, argv, *, input=None, capture_output=False, timeout=None):
        if argv[:5] != ['kubectl', '--context', 'epaflix', '-n', 't3code']:
            raise AssertionError('Unexpected context or namespace')
        if not capture_output or not timeout or timeout > 30:
            raise AssertionError('Subprocess must capture output and have a bounded timeout')
        self.calls.append(argv)
        args = argv[5:]
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, timeout, output=TOKEN, stderr=CA)
        if self.fail:
            return subprocess.CompletedProcess(argv, 1, DIAGNOSTIC, TOKEN + CA)
        if args[0] == 'get':
            if args[1:3] not in (['serviceaccount', 'manager-agent'],
                                  ['secret', 'manager-agent-kubeconfig'], ['secret', 'manager-kubeconfig']):
                raise AssertionError('Unexpected resource read')
            if args[3:] != ['--ignore-not-found', '-o', 'json']:
                raise AssertionError('Unexpected get arguments')
            resource = self.resources.get(tuple(args[1:3]))
            if args[1:3] == ['secret', 'manager-agent-kubeconfig'] and resource:
                self.polls += 1
                if self.populate and self.polls > self.delay:
                    resource.setdefault('data', {}).update({'token': encoded(TOKEN), 'ca.crt': encoded(CA)})
            raw = json.dumps(resource).encode() if resource else b''
            return subprocess.CompletedProcess(argv, 0, raw, DIAGNOSTIC)
        if args == ['create', '-f', '-']:
            if self.fail_create:
                return subprocess.CompletedProcess(argv, 1, DIAGNOSTIC, TOKEN + CA)
            resource = json.loads(input)
            if resource['kind'] != 'Secret' or resource['metadata']['namespace'] != 't3code':
                raise AssertionError('Only t3code Secrets may be created')
            key = ('secret', resource['metadata']['name'])
            if key in self.resources:
                return subprocess.CompletedProcess(argv, 1, DIAGNOSTIC, TOKEN + CA)
            self.created.append(copy.deepcopy(resource))
            self.resources[key] = resource
            return subprocess.CompletedProcess(argv, 0, DIAGNOSTIC, DIAGNOSTIC)
        raise AssertionError('Unexpected mutation')


class ManagerKubeconfigTest(unittest.TestCase):
    def setUp(self):
        self.module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(self.module)
        self.cluster = FakeKubectl()

    def existing_grant(self):
        self.cluster.resources[('secret', 'manager-kubeconfig')] = {
            'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
            'metadata': {'name': 'manager-kubeconfig', 'namespace': 't3code', 'resourceVersion': '1'},
            'data': {'kubeconfig': encoded(json.dumps(expected_config(), indent=2).encode())}}

    def invoke_cli(self, args=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        provision = self.module.provision
        with patch.object(sys, 'argv', [SPEC.origin, *(args if args is not None else ['--context', 'epaflix'])]), \
                patch.object(self.module, 'provision', side_effect=lambda context: provision(context, run=self.cluster)), \
                patch.object(self.module.time, 'sleep') as sleep, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                self.module.main()
            except SystemExit as error:
                code = error.code
                if isinstance(code, str):
                    stderr.write(code)
                    code = 1
            else:
                code = 0
        return code, stdout.getvalue(), stderr.getvalue(), sleep

    def test_kubeconfig_has_one_manager_context(self):
        config = json.loads(self.module.kubeconfig(TOKEN, CA))
        self.assertEqual(config, expected_config())
        self.assertEqual(len(config['contexts']), 1)
        self.assertEqual(len(config['clusters']), 1)
        self.assertEqual(len(config['users']), 1)
        self.assertEqual(set(config['users'][0]['user']), {'token'})
        self.assertEqual(set(config['clusters'][0]['cluster']), {'server', 'certificate-authority-data'})

    def test_provision_uses_tracked_account_and_explicit_context(self):
        code, stdout, stderr, _ = self.invoke_cli()
        self.assertEqual(code, 0)
        self.assertEqual(stdout, 'Manager kubeconfig prepared.\n')
        self.assertEqual(stderr, '')
        self.assertTrue(self.cluster.calls)
        self.assertEqual(len(self.cluster.created), 2)
        account, grant = self.cluster.created
        self.assertEqual(account, {'apiVersion': 'v1', 'kind': 'Secret',
            'metadata': {'name': 'manager-agent-kubeconfig', 'namespace': 't3code',
                         'annotations': {'kubernetes.io/service-account.name': 'manager-agent'}},
            'type': 'kubernetes.io/service-account-token', 'data': {}})
        self.assertEqual(grant['type'], 'Opaque')
        self.assertEqual(grant['metadata'], {'name': 'manager-kubeconfig', 'namespace': 't3code'})
        self.assertEqual(set(grant['data']), {'kubeconfig'})
        self.assertEqual(json.loads(base64.b64decode(grant['data']['kubeconfig'])), expected_config())

    def test_existing_identical_grant_is_idempotent(self):
        self.existing_grant()
        code, _, _, _ = self.invoke_cli()
        self.assertEqual(code, 0)
        before = copy.deepcopy(self.cluster.resources)
        code, stdout, stderr, _ = self.invoke_cli()
        self.assertEqual(code, 0)
        self.assertEqual(stdout, 'Manager kubeconfig prepared.\n')
        self.assertEqual(stderr, '')
        self.assertEqual(self.cluster.resources, before)
        self.assertEqual(len(self.cluster.created), 1)

    def test_waits_for_token_controller_without_recreating_secret(self):
        self.cluster.delay = 2
        code, _, _, sleep = self.invoke_cli()
        self.assertEqual(code, 0)
        self.assertEqual(self.cluster.polls, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(all(call.args == (1,) for call in sleep.call_args_list))
        self.assertEqual(len(self.cluster.created), 2)

    def test_missing_account_unpopulated_token_or_mismatched_grant_fails_quietly(self):
        for scenario in ('account', 'unpopulated', 'missing-ca', 'missing-token', 'grant', 'extra-key',
                         'grant-type', 'account-annotation', 'account-type', 'invalid-base64',
                         'invalid-json', 'kubectl', 'create', 'timeout'):
            with self.subTest(scenario=scenario):
                self.cluster = FakeKubectl()
                self.existing_grant()
                if scenario == 'account':
                    del self.cluster.resources[('serviceaccount', 'manager-agent')]
                elif scenario in ('unpopulated', 'missing-ca', 'missing-token'):
                    self.cluster.populate = False
                    data = {} if scenario == 'unpopulated' else {
                        'token' if scenario == 'missing-ca' else 'ca.crt':
                            encoded(TOKEN if scenario == 'missing-ca' else CA)}
                    self.cluster.resources[('secret', 'manager-agent-kubeconfig')] = {
                        'type': 'kubernetes.io/service-account-token',
                        'metadata': {'name': 'manager-agent-kubeconfig', 'namespace': 't3code',
                                     'annotations': {'kubernetes.io/service-account.name': 'manager-agent'}},
                        'data': data}
                elif scenario in ('account-annotation', 'account-type', 'invalid-base64'):
                    self.cluster.populate = False
                    self.cluster.resources[('secret', 'manager-agent-kubeconfig')] = {
                        'type': 'Opaque' if scenario == 'account-type' else 'kubernetes.io/service-account-token',
                        'metadata': {'name': 'manager-agent-kubeconfig', 'namespace': 't3code',
                                     'annotations': {'kubernetes.io/service-account.name':
                                                     'other-agent' if scenario == 'account-annotation' else 'manager-agent'}},
                        'data': {'token': '%%%' if scenario == 'invalid-base64' else encoded(TOKEN), 'ca.crt': encoded(CA)}}
                elif scenario == 'kubectl':
                    self.cluster.fail = True
                elif scenario == 'create':
                    self.cluster.fail_create = True
                elif scenario == 'timeout':
                    self.cluster.timeout = True
                else:
                    grant = self.cluster.resources[('secret', 'manager-kubeconfig')]
                    if scenario == 'grant':
                        grant['data']['kubeconfig'] = encoded(b'{"current-context":"other"}')
                    elif scenario == 'extra-key':
                        grant['data']['other'] = encoded(b'synthetic-other')
                    elif scenario == 'grant-type':
                        grant['type'] = 'kubernetes.io/service-account-token'
                    elif scenario == 'invalid-json':
                        grant['data']['kubeconfig'] = encoded(b'not json')
                before = copy.deepcopy(self.cluster.resources[('secret', 'manager-kubeconfig')])
                code, stdout, stderr, sleep = self.invoke_cli()
                self.assertNotEqual(code, 0)
                self.assertEqual(stdout, '')
                self.assertTrue(stderr)
                for private in (TOKEN, CA, DIAGNOSTIC):
                    self.assertNotIn(private.decode(), stdout + stderr)
                    self.assertNotIn(encoded(private), stdout + stderr)
                self.assertNotIn('Traceback', stderr)
                self.assertEqual(self.cluster.resources[('secret', 'manager-kubeconfig')], before)
                self.assertLessEqual(self.cluster.polls, 30)
                self.assertLessEqual(sleep.call_count, 30)
                if scenario in ('unpopulated', 'missing-ca', 'missing-token'):
                    self.assertEqual(self.cluster.polls, 30)
                if scenario == 'account':
                    self.assertEqual(self.cluster.created, [])
                self.assertTrue(all(item['metadata']['name'] != 'manager-kubeconfig' for item in self.cluster.created))

    def test_context_is_required_before_any_kubectl_call(self):
        for args in ([], ['--context', ''], ['--context', '   ']):
            with self.subTest(args=args):
                code, stdout, _, _ = self.invoke_cli(args)
                self.assertNotEqual(code, 0)
                self.assertEqual(stdout, '')
                self.assertEqual(self.cluster.calls, [])

    def test_real_cli_captures_kubectl_failure_without_exposing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            kubectl = Path(temporary) / 'kubectl'
            kubectl.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdout.write("synthetic-stdout-private")\n'
                               'sys.stderr.write("synthetic-stderr-private")\nsys.exit(1)\n')
            kubectl.chmod(0o700)
            result = subprocess.run([sys.executable, SPEC.origin, '--context', 'epaflix'],
                env={**os.environ, 'PATH': temporary + os.pathsep + os.environ['PATH']},
                capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')
        self.assertTrue(result.stderr)
        self.assertNotIn(b'synthetic-', result.stderr)
        self.assertNotIn(b'Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
