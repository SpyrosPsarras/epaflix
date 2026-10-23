#!/usr/bin/env python3
"""t3-build and t3-run against a fake kubectl on PATH."""
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / 'env/files/t3-build.py'
RUN = ROOT / 'env/files/t3-run.sh'
TEMPLATE = ROOT.parent / '29.t3-build/files/kaniko-job.yaml'
DOCKER_CONFIG = '{"auths": {"registry.example": {"auth": "c3ludGhldGljLWNyZWRlbnRpYWw="}}}'

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
state = os.environ['FAKE_DIR']
argv = sys.argv[1:]
data = sys.stdin.buffer.read() if '-f' in argv or 'run' in argv else b''
with open(os.path.join(state, 'calls.jsonl'), 'a') as calls:
    calls.write(json.dumps({'argv': argv, 'stdin': data.decode(errors='replace')}) + '\n')
args = argv[4:]
if args[:3] == ['get', 'configmap', 'kaniko-job']:
    sys.stdout.write(open(os.environ['FAKE_TEMPLATE']).read())
elif args[:3] == ['create', '-f', '-']:
    if os.environ.get('FAKE_FAIL_CREATE') and os.environ['FAKE_FAIL_CREATE'] in data.decode():
        sys.exit('synthetic create failure')
elif args[:1] == ['logs']:
    print('synthetic kaniko log')
elif args[:2] == ['get', 'job']:
    sys.stdout.write(os.environ.get('FAKE_STATUS', '1,'))
elif args[:1] == ['run']:
    sys.stdout.buffer.write(data)
    sys.exit(int(os.environ.get('FAKE_RUN_CODE', '0')))
'''


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

    def run_tool(self, argv, stdin=b'', **env):
        return subprocess.run(argv, input=stdin, capture_output=True, timeout=30, env={
            **os.environ, 'PATH': f'{self.tmp / "bin"}{os.pathsep}{os.environ["PATH"]}',
            'FAKE_DIR': str(self.tmp), 'FAKE_TEMPLATE': str(TEMPLATE),
            'T3_BUILD_KUBECONFIG': str(self.kubeconfig), **env})

    def calls(self):
        path = self.tmp / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def created(self):
        return [yaml.safe_load(call['stdin']) for call in self.calls() if call['argv'][4:7] == ['create', '-f', '-']]


class T3BuildTest(Fixture):
    def build(self, *extra, **env):
        return self.run_tool(['python3', str(BUILD), str(self.context), '--destination',
                              'registry.example/team/app:1.0', *extra], **env)

    def test_builds_with_registry_auth_and_cleans_up(self):
        result = self.build('--dockerfile', 'docker/Alt.Dockerfile', '--docker-config', str(self.docker_config))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'synthetic kaniko log', result.stdout)
        for call in self.calls():
            self.assertEqual(call['argv'][:4], ['--kubeconfig', str(self.kubeconfig), '-n', 't3-build'])
        configmap, secret, job = self.created()
        name = job['metadata']['name']
        self.assertRegex(name, r'^t3-build-[0-9a-f]{8}$')
        self.assertEqual((configmap['kind'], configmap['metadata']['name']), ('ConfigMap', name))
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
        self.assertEqual(self.calls()[-1]['argv'][4:], ['delete', f'job/{name}', f'configmap/{name}',
                                                        f'secret/{name}', '--ignore-not-found', '--wait=false'])
        for private in (DOCKER_CONFIG.encode(), b'c3ludGhldGljLWNyZWRlbnRpYWw='):
            self.assertNotIn(private, result.stdout + result.stderr)

    def test_failed_job_exits_nonzero_and_still_cleans_up(self):
        result = self.build(FAKE_STATUS=',1')
        self.assertEqual(result.returncode, 1)
        self.assertEqual([c['kind'] for c in self.created()], ['ConfigMap', 'Job'])
        self.assertEqual(self.calls()[-1]['argv'][4], 'delete')

    def test_create_failure_cleans_up_without_echoing_secret(self):
        result = self.build('--docker-config', str(self.docker_config), FAKE_FAIL_CREATE='"Secret"')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls()[-1]['argv'][4], 'delete')
        self.assertFalse(any(c['argv'][4] == 'logs' for c in self.calls()))
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
        (call,) = self.calls()
        argv = call['argv']
        self.assertEqual(argv[:5], ['--kubeconfig', str(self.kubeconfig), '-n', 't3-build', 'run'])
        self.assertRegex(argv[5], r'^t3-run-[0-9]+-[0-9]+$')
        self.assertEqual(argv[6:], ['--rm', '-i', '-q', '--restart=Never', '--image=alpine:3',
                                    '--overrides={"spec":{"automountServiceAccountToken":false,"enableServiceLinks":false}}',
                                    '--', 'sh', '-c', 'cat'])

    def test_exit_code_and_no_command(self):
        result = self.run_tool([str(RUN), 'alpine:3'], FAKE_RUN_CODE='3')
        self.assertEqual(result.returncode, 3)
        self.assertNotIn('--', self.calls()[0]['argv'][6:])

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
