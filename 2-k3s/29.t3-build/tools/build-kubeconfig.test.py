#!/usr/bin/env python3
"""build-kubeconfig.py against an in-memory kubectl, like manager-kubeconfig.test.py."""
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

SPEC = importlib.util.spec_from_file_location('build_kubeconfig', Path(__file__).with_name('build-kubeconfig.py'))
TOKEN = b'synthetic-builder-credential'
CA = b'synthetic-builder-certificate'
DIAGNOSTIC = b'synthetic-private-kubectl-diagnostic'
TARGET = {'homelab': 't3code-homelab', 'work': 't3code-work'}


def encoded(value):
    return base64.b64encode(value).decode()


def expected_config(pod):
    return {'apiVersion': 'v1', 'kind': 'Config', 'current-context': 't3-build',
            'clusters': [{'name': 't3-build', 'cluster': {
                'server': 'https://kubernetes.default.svc', 'certificate-authority-data': encoded(CA)}}],
            'users': [{'name': 'builder', 'user': {'token': TOKEN.decode()}}],
            'contexts': [{'name': 't3-build', 'context': {
                'cluster': 't3-build', 'user': 'builder', 'namespace': f't3-build-{pod}'}}]}


class FakeKubectl:
    def __init__(self, pod):
        self.pod, self.builder = pod, f't3-build-{pod}'
        self.resources = {(self.builder, 'serviceaccount', 'builder'): {'kind': 'ServiceAccount'}}
        self.calls, self.created, self.violations = [], [], []
        self.populate, self.delay, self.polls = True, 0, 0
        self.fail = False

    def __call__(self, argv, *, input=None, capture_output=False, timeout=None):
        if argv[:3] != ['kubectl', '--context', 'epaflix'] or argv[3] != '-n' or not capture_output or not timeout:
            self.violations.append(argv)
            raise AssertionError('Unexpected invocation')
        namespace, args = argv[4], argv[5:]
        self.calls.append(argv)
        if self.fail:
            return subprocess.CompletedProcess(argv, 1, DIAGNOSTIC, TOKEN + CA)
        token = 'builder-token'
        if args[0] == 'get':
            allowed = {(self.builder, 'serviceaccount', 'builder'), (self.builder, 'secret', token),
                       (TARGET[self.pod], 'secret', 't3-build-kubeconfig')}
            if (namespace, *args[1:3]) not in allowed or args[3:] != ['--ignore-not-found', '-o', 'json']:
                self.violations.append(argv)
                raise AssertionError('Unexpected read')
            resource = self.resources.get((namespace, *args[1:3]))
            if args[1:3] == ['secret', token] and resource:
                self.polls += 1
                if self.populate and self.polls > self.delay:
                    resource.setdefault('data', {}).update({'token': encoded(TOKEN), 'ca.crt': encoded(CA)})
            return subprocess.CompletedProcess(argv, 0, json.dumps(resource).encode() if resource else b'', DIAGNOSTIC)
        if args == ['create', '-f', '-']:
            resource = json.loads(input)
            name = resource['metadata']['name']
            if resource['kind'] != 'Secret' or resource['metadata']['namespace'] != namespace or (namespace, name) not in (
                    (self.builder, token), (TARGET[self.pod], 't3-build-kubeconfig')):
                self.violations.append(argv)
                raise AssertionError('Unexpected create')
            self.created.append(copy.deepcopy(resource))
            self.resources[(namespace, 'secret', name)] = resource
            return subprocess.CompletedProcess(argv, 0, DIAGNOSTIC, DIAGNOSTIC)
        self.violations.append(argv)
        raise AssertionError('Unexpected mutation')


class BuildKubeconfigTest(unittest.TestCase):
    def setUp(self):
        self.module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(self.module)

    def invoke(self, cluster, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        provision = self.module.provision
        with patch.object(sys, 'argv', [SPEC.origin, *args]), \
                patch.object(self.module, 'provision', side_effect=lambda c, p: provision(c, p, run=cluster)), \
                patch.object(self.module.time, 'sleep') as sleep, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                self.module.main()
                code = 0
            except SystemExit as error:
                code = error.code
                if isinstance(code, str):
                    stderr.write(code)
                    code = 1
        self.assertEqual(cluster.violations, [])
        return code, stdout.getvalue(), stderr.getvalue(), sleep

    def test_each_pod_gets_its_own_scoped_grant_in_its_namespace(self):
        for pod in ('homelab', 'work'):
            with self.subTest(pod=pod):
                cluster = FakeKubectl(pod)
                cluster.delay = 2
                code, stdout, stderr, sleep = self.invoke(cluster, ['--context', 'epaflix', '--pod', pod])
                self.assertEqual((code, stdout, stderr), (0, f't3-build kubeconfig prepared for {pod}.\n', ''))
                self.assertEqual(sleep.call_count, 2)
                account, grant = cluster.created
                self.assertEqual(account['metadata'], {'name': 'builder-token', 'namespace': f't3-build-{pod}',
                    'annotations': {'kubernetes.io/service-account.name': 'builder'}})
                self.assertEqual(account['type'], 'kubernetes.io/service-account-token')
                self.assertEqual(grant['metadata'], {'name': 't3-build-kubeconfig', 'namespace': TARGET[pod]})
                self.assertEqual((grant['type'], set(grant['data'])), ('Opaque', {'kubeconfig'}))
                self.assertEqual(json.loads(base64.b64decode(grant['data']['kubeconfig'])), expected_config(pod))
                before = copy.deepcopy(cluster.resources)
                self.assertEqual(self.invoke(cluster, ['--context', 'epaflix', '--pod', pod])[0], 0)
                self.assertEqual(cluster.resources, before)
                self.assertEqual(len(cluster.created), 2)

    def test_failures_are_quiet_and_leave_existing_grant(self):
        for scenario in ('account', 'unpopulated', 'mismatch', 'kubectl'):
            with self.subTest(scenario=scenario):
                cluster = FakeKubectl('work')
                grant = {'type': 'Opaque', 'data': {'kubeconfig': encoded(b'{"current-context":"other"}')}}
                if scenario == 'account':
                    cluster.resources.clear()
                elif scenario == 'unpopulated':
                    cluster.populate = False
                elif scenario == 'mismatch':
                    cluster.resources[('t3code-work', 'secret', 't3-build-kubeconfig')] = grant
                else:
                    cluster.fail = True
                code, stdout, stderr, sleep = self.invoke(cluster, ['--context', 'epaflix', '--pod', 'work'])
                self.assertNotEqual(code, 0)
                self.assertEqual(stdout, '')
                for private in (TOKEN, CA, DIAGNOSTIC):
                    self.assertNotIn(private.decode(), stderr)
                    self.assertNotIn(encoded(private), stderr)
                self.assertNotIn('Traceback', stderr)
                self.assertTrue(all(item['metadata']['name'] != 't3-build-kubeconfig' for item in cluster.created))
                self.assertLessEqual(sleep.call_count, 30)

    def test_context_and_pod_are_required_before_any_kubectl_call(self):
        for args in ([], ['--context', 'epaflix'], ['--pod', 'work'], ['--context', ' ', '--pod', 'work'],
                     ['--context', 'epaflix', '--pod', 'manager']):
            with self.subTest(args=args):
                cluster = FakeKubectl('work')
                code, stdout, _, _ = self.invoke(cluster, args)
                self.assertNotEqual(code, 0)
                self.assertEqual((stdout, cluster.calls), ('', []))

    def test_real_cli_hides_kubectl_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            kubectl = Path(temporary) / 'kubectl'
            kubectl.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdout.write("synthetic-stdout-private")\n'
                               'sys.stderr.write("synthetic-stderr-private")\nsys.exit(1)\n')
            kubectl.chmod(0o700)
            result = subprocess.run([sys.executable, SPEC.origin, '--context', 'epaflix', '--pod', 'homelab'],
                                    env={**os.environ, 'PATH': temporary + os.pathsep + os.environ['PATH']},
                                    capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')
        self.assertNotIn(b'synthetic-', result.stderr)
        self.assertNotIn(b'Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
