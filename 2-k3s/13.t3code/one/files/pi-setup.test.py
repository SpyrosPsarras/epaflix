#!/usr/bin/env python3
import base64
import contextlib
import copy
from http.client import HTTPException
import io
import importlib.util
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decision(rules, value):
    result = 'allow'
    for pattern, action in rules.items():
        regex = re.escape(pattern).replace(r'\ ', ' ').replace(r'\*', '.*').replace(r'\?', '.')
        if regex.endswith(' .*'):
            regex = regex[:-3] + '( .*)?'
        if re.fullmatch(regex, value, re.S):
            result = action
    return result


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / 'pi-setup.py').exists(), 'Pi setup implementation is missing')
        self.setup = load('pi-setup')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.agent = self.home / '.pi/agent'
        self.agent.mkdir(parents=True)
        (self.home / '.claude').mkdir()
        (self.home / '.claude/AGENTS.md').write_text('Bundle instructions\n')
        self.ssh = self.home / 'ssh.md'
        self.ssh.write_text('SSH instructions\n')
        self.safety = '/opt/safety/dist/pi/index.js'

    def write(self):
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://proxy.test',
                                     'ANTHROPIC_AUTH_TOKEN': 'synthetic'}, clear=False):
            with patch.object(self.setup, 'fetch_models', return_value=[
                    {'id': 'claude/claude-opus-5-5'}, {'id': 'codex/gpt-6.1-sol'},
                    {'id': 'unrelated/model'}]):
                self.setup.write(self.home, self.safety, self.ssh)

    def test_settings_preserve_user_keys_and_rewrite_owned_resources(self):
        (self.agent / 'settings.json').write_text(json.dumps({
            'theme': 'light', 'packages': ['stale'], 'skills': ['stale'],
            'pi-cliproxyapi-provider': {'userKey': True, 'gpt56ContextWindow': 'stale'}}))
        self.write()
        settings = json.loads((self.agent / 'settings.json').read_text())
        self.assertEqual(settings['theme'], 'light')
        self.assertEqual(settings['skills'], ['!**/.agents/skills/ponytail/**'])
        self.assertEqual(settings['pi-cliproxyapi-provider'], {'userKey': True, 'gpt56ContextWindow': 'canonical'})
        self.assertEqual(settings['packages'], [
            '/tools/node_modules/pi-cliproxyapi-provider',
            '/tools/node_modules/@gotgenes/pi-permission-system',
            '/tools/node_modules/@spences10/pi-redact',
            '/tools/node_modules/@juicesharp/rpiv-todo',
            '/tools/node_modules/jev-guard', '/tools/node_modules/superpowers',
            '/tools/node_modules/@dietrichgebert/ponytail', self.safety])
        before = {p.relative_to(self.agent): p.read_bytes() for p in self.agent.rglob('*') if p.is_file()}
        self.write()
        self.assertEqual(before, {p.relative_to(self.agent): p.read_bytes() for p in self.agent.rglob('*') if p.is_file()})

    def test_policy_blocks_commands_and_file_tools(self):
        self.write()
        policy = json.loads((self.agent / 'extensions/pi-permission-system/config.json').read_text())['permission']
        for command in ['printenv HOME', 'cat /proc/1/environ', 'cat /run/jev/openrouter-key',
                        'cat /run/jev-guard/config.json', 'env', 'env HOME', 'set', 'set -o',
                        'set +o', 'export', 'export -p', 'declare -p', 'typeset -p',
                        'mkfs.ext4 /dev/sda', 'dd if=x of=/dev/sda',
                        'kubectl delete pod x', 'kubectl drain node', 'kubectl cordon node',
                        'helm uninstall release', 'reboot', 'shutdown now', 'poweroff',
                        'qm stop 100', 'qm shutdown 100', 'kubectl -n a delete pod x',
                        'kubectl --context prod drain node', 'kubectl -n a cordon node',
                        'helm -n a uninstall release', 'qm --skiplock stop 100',
                        'qm --skiplock shutdown 100', 'ps e', 'ps auxe', 'ps eww']:
            with self.subTest(command=command):
                self.assertEqual(decision(policy['bash'], command), 'deny')
        for command in ['git status', 'kubectl get pods', 'helm list', 'qm status 100',
                        'set -euo pipefail', 'set -- arg', 'export NAME=value',
                        'ps aux', 'ps -ef', 'ps -o pid,cmd']:
            self.assertEqual(decision(policy['bash'], command), 'allow', command)
        self.assertEqual(policy['*'], 'allow')
        for target in ['/run/jev', '/run/jev/key', '/run/jev-guard', '/run/jev-guard/config.json']:
            self.assertEqual(decision(policy['path'], target), 'deny')
        self.assertEqual(decision(policy['path'], '/workspace/README.md'), 'allow')

    def test_policy_uses_global_config_path_and_removes_legacy_file(self):
        legacy = self.agent / 'pi-permissions.jsonc'
        legacy.write_text('{"permission": "ask"}')
        self.write()
        self.assertTrue((self.agent / 'extensions/pi-permission-system/config.json').is_file())
        self.assertFalse(legacy.exists())

    def test_irreversible_mcp_calls_ask_using_shared_hub_set(self):
        hub = load('hub_clients')
        self.write()
        policy = json.loads((self.agent / 'extensions/pi-permission-system/config.json').read_text())['permission']
        self.assertFalse(any(name.startswith('mcp__') for name in policy))
        mcp = policy['mcp']
        self.assertEqual(next(iter(mcp)), '*')
        self.assertEqual(decision(mcp, 'mcp__gmail__gmail_send'), 'ask')
        self.assertEqual(decision(mcp, 'mcp__vaultwarden__vault_trash'), 'ask')
        self.assertEqual(decision(mcp, 'mcp__kubernetes_epaflix__pods_delete'), 'ask')
        for server, tools in hub.ASK.items():
            server = re.sub(r'[^A-Za-z0-9_]', '_', server)
            for tool in tools:
                self.assertEqual(decision(mcp, f'mcp__{server}__{tool}'), 'ask')
        for name in ['mcp__gmail__gmail_search', 'mcp__vaultwarden__vault_list',
                     'mcp__kubernetes_epaflix__pods_list', 'mcp__drive__search_drive_files',
                     'mcp__jev__jev_noul', 'mcp__jev__jev_screen']:
            self.assertEqual(decision(mcp, name), 'allow')
        with patch.dict(self.setup.ASK, {'gmail': ['future_irreversible']}, clear=True):
            self.assertEqual(decision(self.setup.permissions()['permission']['mcp'],
                                      'mcp__gmail__future_irreversible'), 'ask')

    def test_aliases_and_fetch_failure_preserve_previous_file(self):
        self.write()
        path = self.agent / 'pi-cliproxyapi-provider/config.json'
        config = json.loads(path.read_text())
        self.assertEqual(config['baseUrl'], 'http://proxy.test/v1')
        self.assertEqual(config['providerName'], 'cliproxy')
        self.assertEqual(config['modelAliases'], {
            'claude/claude-opus-5-5': 'anthropic/claude-opus-5-5',
            'codex/gpt-6.1-sol': 'openai/gpt-6.1-sol',
            'openrouter/or-glm-5.3-flash': 'openrouter/z-ai/glm-5.3-flash',
            'openrouter/or-deepseek-v4-flash': 'openrouter/deepseek/deepseek-v4-flash',
            'openrouter/or-minimax-m3:free': 'openrouter/minimax/minimax-m3',
            'or-minimax-m3:free': 'openrouter/minimax/minimax-m3'})
        before = path.read_bytes()
        with patch.object(self.setup, 'fetch_models', side_effect=OSError('synthetic outage')):
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.setup.write(self.home, self.safety, self.ssh)
        self.assertIn('previous provider config kept', errors.getvalue())
        self.assertEqual(path.read_bytes(), before)

    def test_instructions_replace_symlink_without_changing_bundle(self):
        source = self.home / '.claude/AGENTS.md'
        (self.agent / 'AGENTS.md').symlink_to(source)
        self.write()
        self.assertEqual(source.read_text(), 'Bundle instructions\n')
        self.assertEqual((self.agent / 'AGENTS.md').read_text(), 'Bundle instructions\n\nSSH instructions\n')
        self.ssh.unlink()
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.write()
        self.assertIn('previous AGENTS.md kept', errors.getvalue())
        self.assertEqual((self.agent / 'AGENTS.md').read_text(), 'Bundle instructions\n\nSSH instructions\n')

    def test_all_discovery_exceptions_keep_aliases_and_finish_instructions(self):
        self.write()
        path = self.agent / 'pi-cliproxyapi-provider/config.json'
        before = path.read_bytes()
        for error in [OSError('outage'), TypeError('shape'), HTTPException('truncated'),
                      RuntimeError('unexpected'), Exception('unknown')]:
            with self.subTest(error=type(error).__name__):
                (self.agent / 'AGENTS.md').unlink(missing_ok=True)
                with patch.object(self.setup, 'fetch_models', side_effect=error):
                    with contextlib.redirect_stderr(io.StringIO()) as errors:
                        self.setup.write(self.home, self.safety, self.ssh)
                self.assertEqual(path.read_bytes(), before)
                self.assertIn('previous provider config kept', errors.getvalue())
                self.assertEqual((self.agent / 'AGENTS.md').read_text(), 'Bundle instructions\n\nSSH instructions\n')

    def test_discovery_without_supported_models_keeps_previous_aliases(self):
        self.write()
        path = self.agent / 'pi-cliproxyapi-provider/config.json'
        before = path.read_bytes()
        for models in [[], [{'id': 'unrelated/model'}]]:
            with self.subTest(models=models):
                with patch.object(self.setup, 'fetch_models', return_value=models):
                    with contextlib.redirect_stderr(io.StringIO()) as errors:
                        self.setup.write(self.home, self.safety, self.ssh)
                self.assertEqual(path.read_bytes(), before)
                self.assertIn('previous provider config kept', errors.getvalue())

    def test_fetch_uses_authenticated_model_endpoint_and_rejects_invalid_data(self):
        response = {'data': [{'id': 'claude/claude-opus-5-5'}]}
        truncated = False
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get('Authorization')))
                self.send_response(200)
                if truncated:
                    self.send_header('Content-Length', '1000')
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/v1'
            self.assertEqual(self.setup.fetch_models(url, 'fake-key'), [{'id': 'claude/claude-opus-5-5'}])
            self.assertEqual(requests, [('/v1/models', 'Bearer fake-key')])
            for response in [{'data': [{'id': 1}]}, [], None, {}]:
                with self.subTest(response=response):
                    with self.assertRaises(ValueError):
                        self.setup.fetch_models(url, 'fake-key')
            self.write()
            path = self.agent / 'pi-cliproxyapi-provider/config.json'
            before = path.read_bytes()
            with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': url.removesuffix('/v1'),
                                         'ANTHROPIC_AUTH_TOKEN': 'fake-key'}):
                for response in [[], None, {}, {'data': []}, {'data': [{'id': 'other/model'}]}]:
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.setup.write(self.home, self.safety, self.ssh)
                    self.assertEqual(path.read_bytes(), before)
                response = {'data': [{'id': 'codex/different'}]}
                truncated = True
                (self.agent / 'AGENTS.md').unlink()
                with contextlib.redirect_stderr(io.StringIO()):
                    self.setup.write(self.home, self.safety, self.ssh)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual((self.agent / 'AGENTS.md').read_text(), 'Bundle instructions\n\nSSH instructions\n')
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_wrapper_transfers_credentials_unsets_direct_auth_and_forwards_args(self):
        binary = self.home / 'fake-pi'
        binary.write_text('''#!/usr/bin/env python3
import json, os, sys
assert 'ANTHROPIC_AUTH_TOKEN' not in os.environ
assert os.environ['CLIPROXYAPI_API_KEY'] == 'fake-key'
assert os.environ['CLIPROXYAPI_BASE_URL'] == 'http://proxy.test/v1'
assert os.environ['CLIPROXYAPI_PROVIDER_NAME'] == 'cliproxy'
assert os.environ['JEV_GUARD_CONFIG'] == '/run/jev-guard/config.json'
assert os.environ['JEV_GUARD_ASK_SCORE'] == '3'
assert os.environ['JEV_GUARD_ASK_P'] == '1'
assert os.environ['JEV_GUARD_SKIP_SCAN'] == os.environ['JEV_GUARD_SKIP_TOOLS']
assert set(os.environ['JEV_GUARD_SKIP_TOOLS'].split(',')) == {
    'mcp__vaultwarden__vault_' + name for name in
    ['add', 'attach', 'attachment', 'get', 'list', 'trash', 'update']}
assert sys.argv[1:] == ['--mode', 'rpc', 'argument with spaces']
print('wrapper OK')
''')
        binary.chmod(0o700)
        wrapper = self.home / 'pi.sh'
        wrapper.write_text((ROOT / 'pi.sh').read_text().replace('/tools/node_modules/.bin/pi', str(binary)))
        result = subprocess.run(['bash', str(wrapper), '--mode', 'rpc', 'argument with spaces'],
                                env={**os.environ, 'ANTHROPIC_AUTH_TOKEN': 'fake-key',
                                     'ANTHROPIC_BASE_URL': 'http://proxy.test/'},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'wrapper OK\n')
        for name in ['ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL']:
            environment = {**os.environ, 'ANTHROPIC_AUTH_TOKEN': 'fake-key',
                           'ANTHROPIC_BASE_URL': 'http://proxy.test/'}
            environment.pop(name)
            result = subprocess.run(['bash', str(wrapper)], env=environment,
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(name, result.stderr)

    def test_secret_config_is_atomic_private_and_silent(self):
        key = self.home / 'fake-key'
        key.write_text('  fake-test-key\n')
        output = self.home / 'runtime/config.json'
        command = ['python3', str(ROOT / 'pi-setup.py'), 'jev-config', str(key), str(output)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, '')
        self.assertEqual(json.loads(output.read_text()), {'openRouterApiKey': 'fake-test-key'})
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        before = output.read_bytes()
        key.write_text(' \n')
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pi-setup jev-config: ValueError: Empty key file', result.stderr)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(list(output.parent.iterdir()), [output])

    def test_setup_errors_identify_action_and_failure_without_key_contents(self):
        settings = self.agent / 'settings.json'
        settings.write_text('{broken')
        result = subprocess.run(['python3', str(ROOT / 'pi-setup.py'), 'write',
                                 str(self.home), self.safety], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pi-setup write: JSONDecodeError:', result.stderr)
        key = self.home / 'key'
        key.write_text('synthetic-key-never-log')
        output = self.home / 'not-a-directory'
        output.write_text('fixture')
        result = subprocess.run(['python3', str(ROOT / 'pi-setup.py'), 'jev-config',
                                 str(key), str(output / 'config.json')], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pi-setup jev-config: FileExistsError:', result.stderr)
        self.assertNotIn('synthetic-key-never-log', result.stdout + result.stderr)

    def test_three_boots_converge_with_legacy_pi_instructions(self):
        private = load('private-config')
        payload = json.dumps({'sourceHome': '/fixture', 'files': {
            'instructions.md': {'data': base64.b64encode(b'Fixture instructions').decode(), 'executable': False},
            'skills/test/SKILL.md': {'data': base64.b64encode(b'Fixture skill').decode(), 'executable': False}}})
        for initial in ['fresh', 'symlink', 'file-and-backup', 'backup-only']:
            with self.subTest(initial=initial), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                agent = home / '.pi/agent'
                agent.mkdir(parents=True)
                instructions = agent / 'AGENTS.md'
                backup = agent / 'AGENTS.md.before-git'
                if initial == 'symlink':
                    instructions.symlink_to(home / '.claude/AGENTS.md')
                if initial == 'file-and-backup':
                    instructions.write_text('Stale combined instructions')
                if initial in ['file-and-backup', 'backup-only']:
                    backup.write_text('Previous migration backup')
                for boot in range(3):
                    private.install(payload.encode(), home)
                    with patch.object(self.setup, 'fetch_models', return_value=[{'id': 'codex/model'}]):
                        self.setup.write(home, self.safety, self.ssh)
                    self.assertFalse(instructions.is_symlink(), (initial, boot))
                    self.assertEqual(instructions.read_text(), 'Fixture instructions\nSSH instructions\n')
                    if initial in ['file-and-backup', 'backup-only']:
                        self.assertEqual(backup.read_text(), 'Previous migration backup')
                    else:
                        self.assertFalse(backup.exists())

    def test_hub_pi_preserves_custom_servers_and_removes_legacy(self):
        hub = load('hub_clients')
        result = hub.pi({'mcpServers': {'mine': {'url': 'https://mine'}, 'keepass': {},
                        'old': {'command': 'keepass-remote.sh'}}, 'user': True},
                        'https://hub/', 'MCP_HUB_TOKEN')
        self.assertEqual(set(result['mcpServers']), set(hub.SERVERS) | set(hub.JEV) | {'mine'})
        self.assertEqual(result['mcpServers']['jev'], {
            'url': 'https://hub/jev', 'headers': {'Authorization': 'Bearer ${MCP_HUB_TOKEN}'}})
        self.assertEqual(result['mcpServers']['vaultwarden'], {
            'url': 'https://hub/vaultwarden', 'headers': {'Authorization': 'Bearer ${MCP_HUB_TOKEN}'}})
        self.assertTrue(result['user'])
        before = copy.deepcopy(result)
        self.assertEqual(hub.pi(copy.deepcopy(result), 'https://hub', 'MCP_HUB_TOKEN'), before)
        self.assertEqual(result, before)


class PackageTests(unittest.TestCase):
    def test_list_requires_every_registered_package_as_a_complete_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            agent = home / '.pi/agent'
            agent.mkdir(parents=True)
            package = home / 'pkg'
            package.mkdir()
            safety = home / 'index.js'
            safety.touch()
            (agent / 'settings.json').write_text(json.dumps({'packages': [str(package), str(safety)]}))
            command = ['python3', str(ROOT / 'pi-setup.py'), 'check-packages', str(home)]
            for listing, success in [(f'User packages:\n  {package}\n    {package}\n  {safety}\n    {safety}\n', True),
                                     (f'  {package}-extra\n  {safety}\n', False),
                                     (f'  {package}\n', False)]:
                result = subprocess.run(command, input=listing, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, success, result.stderr)
                if not success:
                    missing = package if '-extra' in listing else safety
                    self.assertIn(str(missing), result.stderr)
                    self.assertLess(result.stderr.index(str(missing)),
                                    result.stderr.index('pi-setup check-packages: ValueError:'))

    def test_real_pi_listing_rejects_missing_directory_or_file(self):
        listing = '''User packages:
  /fixture/provider
    /fixture/provider
  /fixture/real
    /fixture/real
  /fixture/missing
'''
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            agent = home / '.pi/agent'
            agent.mkdir(parents=True)
            provider = home / 'provider'
            provider.mkdir()
            real = home / 'real'
            real.mkdir()
            for missing in [home / 'missing', home / 'index.js']:
                with self.subTest(missing=missing.name):
                    fixture = listing.replace('/fixture/provider', str(provider))
                    fixture = fixture.replace('/fixture/real', str(real))
                    fixture = fixture.replace('/fixture/missing', str(missing))
                    (agent / 'settings.json').write_text(json.dumps({
                        'packages': [str(provider), str(real), str(missing)]}))
                    command = ['python3', str(ROOT / 'pi-setup.py'), 'check-packages', str(home)]
                    result = subprocess.run(command, input=fixture, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(str(missing), result.stderr)
                    self.assertLess(result.stderr.index(str(missing)),
                                    result.stderr.index('pi-setup check-packages: ValueError:'))
                    if missing.suffix:
                        missing.touch()
                    else:
                        missing.mkdir()
                    result = subprocess.run(command, input=fixture, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
