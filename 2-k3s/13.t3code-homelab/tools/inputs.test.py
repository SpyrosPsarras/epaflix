#!/usr/bin/env python3
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('inputs', Path(__file__).with_name('inputs.py'))


class InputsTest(unittest.TestCase):
    def setUp(self):
        self.module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(self.module)
        self.files = {name: b'{}' for name in ('bundle.json', 'policies.json', 'services.json', 'kubeconfig', 'authorized_keys')}
        self.grants = {'model-token': b'synthetic-model', 'hub-token': b'homelab.synthetic'}
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.skill_file = Path(temporary.name) / 'SKILL.md'
        self.skill_file.write_text('# Synthetic skill\n')

    def test_runtime_config_binds_exact_input_and_grant_bytes(self):
        config = self.module.runtime_config(self.files, self.grants)
        self.assertEqual(config['gitAuth'], 'public')
        self.assertEqual(config['environment'], 'homelab')
        self.assertEqual(config['hubUrl'], 'http://mcp-hub.mcp-hub-homelab.svc.cluster.local:8000')
        self.assertEqual(config['projectUrl'], 'https://github.com/SpyrosPsarras/epaflix.git')
        self.assertEqual(config['sha256']['bundle.json'], hashlib.sha256(b'{}').hexdigest())
        self.assertEqual(config['grantSha256']['model-token'], hashlib.sha256(b'synthetic-model').hexdigest())
        self.files['bundle.json'] = b'{}\n'
        self.assertNotEqual(config['sha256'], self.module.runtime_config(self.files, self.grants)['sha256'])

    def test_github_install_updates_mode_and_digest_without_replacing_other_grants(self):
        old = {'runtime.json': json.dumps(self.module.runtime_config(self.files, self.grants)).encode(), **self.files}
        token = 'github_pat_' + 'a' * 80
        files, grants = self.module.add_github_token(old, self.grants, token)
        self.assertEqual(grants['model-token'], b'synthetic-model')
        self.assertEqual(grants['hub-token'], b'homelab.synthetic')
        self.assertEqual(grants['github-token'], token.encode())
        config = json.loads(files['runtime.json'])
        self.assertEqual(config['gitAuth'], 'github-token')
        self.assertEqual(config['grantSha256']['github-token'], hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(files['kubeconfig'], b'{}')

    def test_classic_empty_and_malformed_github_tokens_are_rejected(self):
        for token in ('', 'ghp_' + 'a' * 40, 'github_pat_a', 'github_pat_' + 'a' * 80 + '\nother'):
            with self.subTest(kind=token[:4]):
                with self.assertRaises(ValueError):
                    self.module.add_github_token({}, self.grants, token)

    def test_input_inventory_cannot_silently_include_work_credentials(self):
        for name in ('work-auth.json', 'work-token'):
            files, grants = dict(self.files), dict(self.grants)
            (files if name.endswith('.json') else grants)[name] = b'{}'
            with self.assertRaises(ValueError):
                self.module.runtime_config(files, grants)

    def test_noninteractive_github_install_refuses_without_disclosing_supplied_input(self):
        supplied = 'yes\ngithub_pat_' + 'x' * 80 + '\n'
        result = subprocess.run([sys.executable, SPEC.origin, 'github'], input=supplied,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('github_pat_', result.stdout + result.stderr)
        self.assertNotIn('x' * 80, result.stdout + result.stderr)

    def test_generated_bundle_declares_required_policy_hook_checks_and_instruction_files(self):
        files = self.module.public_inputs(skill_file=self.skill_file)
        policies = json.loads(files['policies.json'])
        hook = json.loads(base64.b64decode(policies['files']['.claude/settings.json']['data']))
        self.assertIn('hook --coding-cli', hook['hooks']['PreToolUse'][0]['hooks'][0]['command'])
        self.assertTrue(policies['checks'])
        self.assertIn('.config/t3-policy/check.py', policies['files'])
        bundle = json.loads(files['bundle.json'])
        self.assertIn('instructions.md', bundle['files'])
        self.assertEqual(base64.b64decode(bundle['files']['skills/ponytail/SKILL.md']['data']), b'# Synthetic skill\n')

    def test_policy_check_accepts_both_deny_protocols_and_refuses_wrong_decisions(self):
        policies = json.loads(self.module.public_inputs(skill_file=self.skill_file)['policies.json'])
        checker = base64.b64decode(policies['files']['.config/t3-policy/check.py']['data'])
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            argv = [arg.replace('${HOME}', str(home)) for arg in policies['checks'][0]['argv']]
            check = Path(argv[1])
            check.parent.mkdir(parents=True)
            check.write_bytes(checker)
            cli = home / argv[2]
            cli.parent.mkdir(parents=True)
            for behavior, expected in (
                    ("process.exit(unsafe ? 2 : 0)", 0),
                    ("if (unsafe) console.log(JSON.stringify({hookSpecificOutput: {permissionDecision: 'deny'}}))", 0),
                    ("process.exit(0)", 1), ("process.exit(2)", 1), ("process.exit(3)", 1)):
                with self.subTest(behavior=behavior):
                    cli.write_text(
                        "let data = ''; process.stdin.on('data', chunk => data += chunk); "
                        "process.stdin.on('end', () => { const unsafe = JSON.parse(data).tool_input.command !== 'git status'; "
                        + behavior + "; });")
                    result = subprocess.run(argv,
                        env={'HOME': str(home), 'PATH': os.environ['PATH']}, capture_output=True)
                    self.assertEqual(result.returncode, expected)

    def test_github_install_rejects_active_or_recreatable_pod_before_prompting(self):
        class Cluster:
            def __init__(self, replicas, pod):
                self.replicas, self.pod = replicas, pod

            def get(self, kind, namespace, name):
                if kind == 'statefulset':
                    return {'spec': {'replicas': self.replicas}}
                if kind == 'pod':
                    return self.pod
                raise AssertionError('Unexpected resource read')

        for replicas, pod in ((1, None), (0, {'status': {'phase': 'Running'}}), (0, {'status': {'phase': 'Pending'}})):
            with self.subTest(replicas=replicas, pod_present=bool(pod)), patch.object(sys.stdin, 'isatty', return_value=True), patch('builtins.input', side_effect=AssertionError('Credential prompt reached')):
                with self.assertRaisesRegex(ValueError, 'stopped'):
                    self.module.install_github(Cluster(replicas, pod))
        self.module.require_stopped(Cluster(0, None))

    def test_hub_registration_preserves_other_clients_upstreams_and_resource_version(self):
        credentials = {'clients': {'existing': '1' * 64}, 'upstreams': {'synthetic': {'url': 'https://fixture.invalid/mcp', 'token': 'synthetic'}}}
        hub = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
               'metadata': {'name': 'mcp-hub-instance', 'namespace': 'mcp-hub-homelab', 'resourceVersion': 'original'},
               'data': {'credentials.json': base64.b64encode(json.dumps(credentials).encode()).decode(),
                        'services.json': base64.b64encode(b'{"environment":"homelab","services":["searxng"]}').decode()}}

        class Cluster:
            def get(self, kind, namespace, name):
                return hub

            def put(self, value):
                self.saved = value

        cluster = Cluster()
        services = self.module.register_hub_digest(cluster, b'homelab.synthetic')
        result = json.loads(base64.b64decode(cluster.saved['data']['credentials.json']))
        self.assertEqual(result['clients']['existing'], '1' * 64)
        self.assertEqual(result['clients']['t3code-homelab'], hashlib.sha256(b'homelab.synthetic').hexdigest())
        self.assertEqual(result['upstreams'], credentials['upstreams'])
        self.assertEqual(cluster.saved['metadata']['resourceVersion'], 'original')
        self.assertEqual(json.loads(services)['environment'], 'homelab')

    def test_secret_replace_preserves_original_version_instead_of_overwriting_concurrent_change(self):
        class Cluster(self.module.Cluster):
            def get(self, *args):
                return {'metadata': {'resourceVersion': 'newer'}}

            def command(self, *args, value=None):
                self.saved = json.loads(value)

        cluster = Cluster()
        resource = self.module.secret_resource('t3code-homelab', 'fixture', {'value': b'synthetic'})
        resource['metadata']['resourceVersion'] = 'original'
        cluster.put(resource)
        self.assertEqual(cluster.saved['metadata']['resourceVersion'], 'original')

    def test_kubeconfig_helper_uses_tracked_account_and_creates_no_rbac(self):
        class Cluster:
            def get(self, kind, namespace, name):
                return {'metadata': {'name': name, 'namespace': namespace}}

            def secret(self, namespace, name):
                return {'token': b'synthetic', 'ca.crt': b'synthetic-ca'}

            def put(self, resource):
                raise AssertionError('Existing account must not create or change resources')

        config = json.loads(self.module.agent_kubeconfig(Cluster()))
        self.assertEqual(config['current-context'], 'homelab')
        self.assertEqual(config['users'][0]['user'], {'token': 'synthetic'})
        self.assertEqual(config['clusters'][0]['cluster']['server'], 'https://kubernetes.default.svc')

    def test_registration_refuses_masked_prefix_collision_and_retains_other_clients(self):
        state = {'keys': ['t3-homelab-' + 'a' * 43], 'writes': 0}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                data = json.dumps({'api-keys': state['keys']}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(data)

            def do_PATCH(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                state['writes'] += 1
                if request['new'] not in state['keys']:
                    state['keys'].append(request['new'])
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')

        class Cluster:
            def secret(self, namespace, name):
                return {'management-password': b'synthetic-management'}

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(self.module, 'MODEL_URL', f'http://127.0.0.1:{server.server_port}'):
                with self.assertRaisesRegex(ValueError, 'prefix'):
                    self.module.register_model_key(Cluster(), b't3-homelab-' + b'b' * 43)
                self.assertEqual(state['writes'], 0)
                self.module.register_model_key(Cluster(), state['keys'][0].encode())
                self.assertEqual(state['writes'], 0)
                state['keys'] = ['other-client']
                self.module.register_model_key(Cluster(), b't3-homelab-' + b'b' * 43)
                self.assertEqual(state['keys'], ['other-client', 't3-homelab-' + 'b' * 43])
                self.assertEqual(state['writes'], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
