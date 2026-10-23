#!/usr/bin/env python3
"""t3-build and t3-run against a fake kubectl on PATH."""
import base64
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tarfile
import tempfile
import time
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / 'env/files/t3-build.py'
RUN = ROOT / 'env/files/t3-run.sh'
TEMPLATE = ROOT.parent / '29.t3-build/base/files/kaniko-job.yaml'
DOCKER_CONFIG = '{"auths": {"registry.example": {"auth": "c3ludGhldGljLWNyZWRlbnRpYWw="}}}'

FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
state = os.environ['FAKE_DIR']
argv = sys.argv[1:]
data = sys.stdin.buffer.read() if '-f' in argv or 'run' in argv else b''
with open(os.path.join(state, 'calls.jsonl'), 'a') as calls:
    calls.write(json.dumps({'argv': argv, 'stdin': data.decode(errors='replace')}) + '\n')
args = argv[2:]
if args[:3] == ['get', 'configmap', 'kaniko-job']:
    sys.stdout.write(open(os.environ['FAKE_TEMPLATE']).read())
elif args[:3] == ['create', '-f', '-']:
    if os.environ.get('FAKE_FAIL_CREATE') and os.environ['FAKE_FAIL_CREATE'] in data.decode():
        sys.exit('synthetic create failure')
    if '-o' in args:
        sys.stdout.write('synthetic-uid')
elif args[:1] == ['logs']:
    print('synthetic kaniko log')
elif args[:2] == ['get', 'job']:
    sys.stdout.write(os.environ.get('FAKE_STATUS', 'Complete'))
elif args[:1] == ['run']:
    if os.environ.get('FAKE_RUN_HANG'):
        time.sleep(60)
    sys.stdout.buffer.write(data)
    sys.exit(int(os.environ.get('FAKE_RUN_CODE', '0')))
elif args[:1] == ['delete'] and os.environ.get('FAKE_FAIL_DELETE'):
    sys.exit('synthetic delete failure')
'''
OWNER = {'apiVersion': 'batch/v1', 'kind': 'Job', 'uid': 'synthetic-uid'}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / 'bin').mkdir()
        (self.tmp / 'bin/kubectl').write_text(FAKE)
        (self.tmp / 'bin/kubectl').chmod(0o755)
        self.kubeconfig = self.tmp / 'kubeconfig'
        self.kubeconfig.write_text('synthetic')
        self.context = self.tmp / 'ctx'
        (self.context / '.git').mkdir(parents=True)
        (self.context / '.git/HEAD').write_text('ref')
        (self.context / 'Dockerfile').write_text('FROM scratch\n')
        (self.context / 'docker').mkdir()
        (self.context / 'docker/Alt.Dockerfile').write_text('FROM scratch\n')
        self.docker_config = self.tmp / 'config.json'
        self.docker_config.write_text(DOCKER_CONFIG)

    def tearDown(self):
        subprocess.run(['rm', '-rf', str(self.tmp)], check=True)

    def env(self, **env):
        return {**os.environ, 'PATH': f'{self.tmp / "bin"}{os.pathsep}{os.environ["PATH"]}',
                'FAKE_DIR': str(self.tmp), 'FAKE_TEMPLATE': str(TEMPLATE),
                'T3_BUILD_KUBECONFIG': str(self.kubeconfig), **env}

    def run_tool(self, argv, stdin=b'', **env):
        return subprocess.run(argv, input=stdin, capture_output=True, timeout=30, env=self.env(**env))

    def calls(self):
        path = self.tmp / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def created(self):
        return [yaml.safe_load(call['stdin']) for call in self.calls() if call['argv'][2:5] == ['create', '-f', '-']]

    def deletes(self):
        return [call['argv'][2:] for call in self.calls() if call['argv'][2] == 'delete']


class T3BuildTest(Fixture):
    def build(self, *extra, **env):
        return self.run_tool(['python3', str(BUILD), str(self.context), '--destination',
                              'registry.example/team/app:1.0', *extra], **env)

    def test_builds_with_registry_auth_and_cleans_up(self):
        result = self.build('--dockerfile', 'docker/Alt.Dockerfile', '--docker-config', str(self.docker_config))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'synthetic kaniko log', result.stdout)
        for call in self.calls():
            # The namespace comes from the kubeconfig's context: t3-build-homelab or t3-build-work.
            self.assertEqual(call['argv'][:2], ['--kubeconfig', str(self.kubeconfig)])
            self.assertNotIn('-n', call['argv'])
        job, secret, configmap = self.created()
        name = job['metadata']['name']
        self.assertRegex(name, r'^t3-build-[0-9a-f]{8}$')
        self.assertEqual((configmap['kind'], configmap['metadata']['name']), ('ConfigMap', name))
        for owned in (configmap, secret):
            self.assertEqual(owned['metadata']['ownerReferences'], [{**OWNER, 'name': name}])
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(configmap['binaryData']['context.tar.gz']))) as tar:
            names = tar.getnames()
        self.assertIn('./Dockerfile', names)
        self.assertIn('./docker/Alt.Dockerfile', names)
        self.assertFalse(any(n.startswith('./.git') for n in names))
        self.assertEqual((secret['kind'], secret['metadata']['name']), ('Secret', name))
        self.assertEqual(base64.b64decode(secret['data']['config.json']).decode(), DOCKER_CONFIG)
        template = yaml.safe_load(TEMPLATE.read_text())
        container = job['spec']['template']['spec']['containers'][0]
        self.assertEqual(container['image'], template['spec']['template']['spec']['containers'][0]['image'])
        self.assertEqual(container['args'], ['--context', 'tar:///context/context.tar.gz', '--dockerfile',
                                             'docker/Alt.Dockerfile', '--destination', 'registry.example/team/app:1.0'])
        volumes = {v['name']: v for v in job['spec']['template']['spec']['volumes']}
        self.assertEqual(volumes['context']['configMap']['name'], name)
        self.assertEqual(volumes['docker-config']['secret']['secretName'], name)
        # The Secret is created after the Job, so the pod must wait for it, not mount it empty.
        self.assertIs(volumes['docker-config']['secret']['optional'], False)
        self.assertEqual(self.deletes(), [['delete', f'job/{name}', f'configmap/{name}',
                                           f'secret/{name}', '--ignore-not-found', '--wait=false']])
        self.assertEqual(job['spec']['activeDeadlineSeconds'], 1800)
        for private in (DOCKER_CONFIG.encode(), b'c3ludGhldGljLWNyZWRlbnRpYWw='):
            self.assertNotIn(private, result.stdout + result.stderr)

    def test_unauthenticated_build_keeps_the_secret_mount_optional(self):
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        job, configmap = self.created()
        volumes = {v['name']: v for v in job['spec']['template']['spec']['volumes']}
        self.assertIs(volumes['docker-config']['secret']['optional'], True)

    def test_failed_job_exits_nonzero_and_still_cleans_up(self):
        for status in ('Failed', 'FailureTarget Failed'):
            with self.subTest(status=status):
                (self.tmp / 'calls.jsonl').unlink(missing_ok=True)
                result = self.build(FAKE_STATUS=status)
                self.assertEqual(result.returncode, 1)
                self.assertEqual([c['kind'] for c in self.created()], ['Job', 'ConfigMap'])
                self.assertEqual(self.calls()[-1]['argv'][2], 'delete')
                self.assertEqual(sum(c['argv'][2:4] == ['get', 'job'] for c in self.calls()), 1)

    def test_times_out_when_the_job_never_finishes(self):
        result = self.build(FAKE_STATUS='', T3_BUILD_TIMEOUT='0')
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'timed out', result.stderr)
        self.assertEqual(len(self.deletes()), 1)

    def test_failed_cleanup_is_reported_retried_and_exits_nonzero(self):
        for status, code in (('Complete', 2), ('Failed', 1)):
            with self.subTest(status=status):
                (self.tmp / 'calls.jsonl').unlink(missing_ok=True)
                result = self.build(FAKE_STATUS=status, FAKE_FAIL_DELETE='1')
                self.assertEqual(result.returncode, code)
                self.assertEqual(len(self.deletes()), 2)
                self.assertIn(b'synthetic delete failure', result.stderr)

    def test_create_failure_cleans_up_without_echoing_secret(self):
        result = self.build('--docker-config', str(self.docker_config), FAKE_FAIL_CREATE='"Secret"')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls()[-1]['argv'][2], 'delete')
        self.assertFalse(any(c['argv'][2] == 'logs' for c in self.calls()))
        self.assertNotIn(b'c3ludGhldGljLWNyZWRlbnRpYWw=', result.stdout + result.stderr)

    def test_refuses_without_kubeconfig(self):
        self.kubeconfig.unlink()
        result = self.build()
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'build-kubeconfig.py', result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertEqual(self.calls(), [])

    def test_rejects_bad_input_before_any_kubectl_call(self):
        (self.context / 'big').write_bytes(os.urandom(1_100_000))
        bad_config = self.tmp / 'bad.json'
        bad_config.write_text('not json')
        cases = {
            'size': [str(self.context), '--destination', 'registry.example/app:1'],
            'destination': [str(self.tmp), '--destination', 'registry.example/app:1"\n  x'],
            'dockerfile': [str(self.tmp), '--destination', 'registry.example/app:1', '--dockerfile', '../Dockerfile'],
            'absolute': [str(self.tmp), '--destination', 'registry.example/app:1', '--dockerfile', '/etc/passwd'],
            'context': [str(self.tmp / 'missing'), '--destination', 'registry.example/app:1'],
            'config': [str(self.tmp), '--destination', 'registry.example/app:1', '--docker-config', str(bad_config)],
        }
        for case, argv in cases.items():
            with self.subTest(case=case):
                result = self.run_tool(['python3', str(BUILD), *argv])
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(b'Traceback', result.stderr)
                self.assertEqual(self.calls(), [])


class T3RunTest(Fixture):
    def test_runs_one_shot_pod_with_stdin(self):
        result = self.run_tool([str(RUN), 'alpine:3', '--', 'sh', '-c', 'cat'], stdin=b'synthetic input')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b'synthetic input')
        run, delete = (call['argv'] for call in self.calls())
        self.assertEqual(run[:3], ['--kubeconfig', str(self.kubeconfig), 'run'])
        self.assertRegex(run[3], r'^t3-run-[0-9a-f]{8}$')
        self.assertEqual(run[4:], ['--rm', '-i', '-q', '--restart=Never', '--image=alpine:3',
                                   '--overrides={"spec":{"automountServiceAccountToken":false,"enableServiceLinks":false}}',
                                   '--', 'sh', '-c', 'cat'])
        self.assertEqual(delete[2:], ['delete', 'pod', run[3], '--ignore-not-found', '--wait=false'])

    def test_exit_code_and_no_command(self):
        result = self.run_tool([str(RUN), 'alpine:3'], FAKE_RUN_CODE='3')
        self.assertEqual(result.returncode, 3)
        self.assertNotIn('--', self.calls()[0]['argv'][4:])

    def test_interrupt_deletes_the_pod(self):
        process = subprocess.Popen([str(RUN), 'alpine:3', '--', 'sleep', '600'], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env=self.env(FAKE_RUN_HANG='1'))
        try:
            for _ in range(100):
                if self.calls():
                    break
                time.sleep(0.1)
            process.send_signal(signal.SIGINT)
            self.assertEqual(process.wait(timeout=10), 130)
        finally:
            process.kill()
        run, *rest = (call['argv'] for call in self.calls())
        self.assertEqual([argv[2:] for argv in rest], [['delete', 'pod', run[3], '--ignore-not-found', '--wait=false']])

    def test_refuses_without_kubeconfig_or_image(self):
        self.kubeconfig.unlink()
        result = self.run_tool([str(RUN), 'alpine:3'])
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'build-kubeconfig.py', result.stderr)
        self.kubeconfig.write_text('synthetic')
        for argv in ([], ['--', 'sh'], ['alpine:3', 'sh']):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_tool([str(RUN), *argv]).returncode, 2)
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()
