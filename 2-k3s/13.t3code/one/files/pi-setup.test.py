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
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        environment = patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://proxy.test',
                                             'ANTHROPIC_AUTH_TOKEN': 'fake-key',
                                             'CLIPROXYAPI_API_KEY': 'fake-key'})
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, base_url='http://proxy.test'):
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': base_url,
                                     'ANTHROPIC_AUTH_TOKEN': 'synthetic'}, clear=False):
            with patch.object(self.setup, 'fetch_models', return_value=[
                    {'id': 'claude/claude-opus-5-5'}, {'id': 'codex/gpt-6.1-sol'},
                    {'id': 'unrelated/model'}]):
                self.setup.write(self.home, self.safety, self.ssh)

    def test_settings_preserve_user_keys_and_rewrite_owned_resources(self):
        (self.agent / 'settings.json').write_text(json.dumps({
            'theme': 'light', 'packages': ['/tools/node_modules/@gotgenes/pi-permission-system'], 'skills': ['stale'],
            'pi-cliproxyapi-provider': {'userKey': True, 'gpt56ContextWindow': 'stale'},
            'retry': {'baseDelayMs': 1000, 'maxRetries': 3}}))
        self.write()
        settings = json.loads((self.agent / 'settings.json').read_text())
        self.assertEqual(settings['theme'], 'light')
        self.assertEqual(settings['skills'], ['!**/.agents/skills/ponytail/**'])
        self.assertEqual(settings['pi-cliproxyapi-provider'], {'userKey': True, 'gpt56ContextWindow': 'canonical'})
        self.assertEqual(settings['retry'], {'baseDelayMs': 1000, 'maxRetries': 8})
        self.assertEqual(settings['packages'], [
            '/tools/node_modules/pi-cliproxyapi-provider',
            '/tools/node_modules/@spences10/pi-redact',
            '/tools/node_modules/@juicesharp/rpiv-todo',
            '/tools/node_modules/@juicesharp/rpiv-ask-user-question',
            '/tools/node_modules/superpowers',
            '/tools/node_modules/@dietrichgebert/ponytail',
            '/tools/node_modules/not-enough-retry', self.safety])
        before = {p.relative_to(self.agent): p.read_bytes() for p in self.agent.rglob('*') if p.is_file()}
        self.write()
        self.assertEqual(before, {p.relative_to(self.agent): p.read_bytes() for p in self.agent.rglob('*') if p.is_file()})

    def test_textgen_provider_uses_anthropic_root_and_environment_key(self):
        self.write('http://proxy.test///')
        path = self.agent / 'models.json'
        self.assertTrue(path.exists(), 'Text generation provider is missing')
        self.assertEqual(json.loads(path.read_text()), {'providers': {'cliproxy-textgen': {
            'baseUrl': 'http://proxy.test', 'api': 'anthropic-messages',
            'apiKey': '$CLIPROXYAPI_API_KEY', 'models': [{
                'id': 'claude/claude-haiku-5-5', 'name': 'Claude Haiku 5.5 (text generation)',
                'reasoning': True, 'input': ['text'], 'contextWindow': 1000000,
                'maxTokens': 128000, 'compat': {'forceAdaptiveThinking': True}}]}}})
        self.assertNotIn('fake-key', path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_textgen_replaces_only_owned_provider(self):
        path = self.agent / 'models.json'
        other = {'baseUrl': 'http://other.test', 'models': [{'id': 'user-model'}]}
        path.write_text(json.dumps({'userKey': ['keep'], 'providers': {
            'other': other, 'cliproxy-textgen': {'models': [{'id': 'stale'}]}}}))
        self.write()
        data = json.loads(path.read_text())
        self.assertEqual(data['userKey'], ['keep'])
        self.assertEqual(set(data['providers']), {'other', 'cliproxy-textgen'})
        self.assertEqual(data['providers']['other'], other)
        self.assertEqual(data['providers']['cliproxy-textgen']['models'][0]['id'], 'claude/claude-haiku-5-5')
        before = path.read_bytes()
        self.write()
        self.assertEqual(path.read_bytes(), before)

    def test_textgen_atomic_write_replaces_symlink_without_changing_target(self):
        target = self.home / 'user-models.json'
        original = '{"userKey": "keep"}\n'
        target.write_text(original)
        path = self.agent / 'models.json'
        path.symlink_to(target)
        self.write()
        self.assertFalse(path.is_symlink())
        self.assertEqual(target.read_text(), original)
        self.assertIn('cliproxy-textgen', json.loads(path.read_text())['providers'])
        self.assertFalse(list(self.agent.glob('.pi-setup-*')))

    def test_empty_or_unset_base_url_warns_and_does_not_touch_models(self):
        path = self.agent / 'models.json'
        for base_url in ['', None]:
            for existing in [False, True]:
                with self.subTest(base_url=base_url, existing=existing):
                    path.unlink(missing_ok=True)
                    if existing:
                        path.write_text('{"userKey": "keep"}\n')
                        before = path.stat()
                    environment = {} if base_url is None else {'ANTHROPIC_BASE_URL': base_url}
                    with patch.dict(os.environ, environment, clear=True):
                        with patch.object(self.setup, 'fetch_models', side_effect=OSError('synthetic outage')):
                            with contextlib.redirect_stderr(io.StringIO()) as errors:
                                self.setup.write(self.home, self.safety, self.ssh)
                    self.assertIn('pi-setup: ANTHROPIC_BASE_URL is empty; previous models.json kept', errors.getvalue())
                    self.assertEqual(path.exists(), existing)
                    if existing:
                        self.assertEqual(path.read_text(), '{"userKey": "keep"}\n')
                        self.assertEqual(path.stat(), before)
                    self.assertTrue((self.agent / 'settings.json').exists())
                    self.assertTrue((self.agent / 'AGENTS.md').exists())

    def test_setup_does_not_create_permission_policy(self):
        self.write()
        self.assertFalse((self.agent / 'extensions/pi-permission-system/config.json').exists())
        self.assertFalse((self.agent / 'pi-permissions.jsonc').exists())

    def test_stale_permission_policies_remain_inert_across_restarts(self):
        paths = [self.agent / 'extensions/pi-permission-system/config.json',
                 self.agent / 'pi-permissions.jsonc']
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"permission": "ask"}')
        for boot in range(3):
            self.write()
            settings = json.loads((self.agent / 'settings.json').read_text())
            self.assertFalse(any('pi-permission-system' in package for package in settings['packages']))
            self.assertIn('/tools/node_modules/@spences10/pi-redact', settings['packages'])
            self.assertIn(self.safety, settings['packages'])
            for path in paths:
                self.assertEqual(path.read_text(), '{"permission": "ask"}')

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

    def test_aliases_action_refreshes_only_provider_config(self):
        self.write()
        before = {p.relative_to(self.agent): p.read_bytes() for p in self.agent.rglob('*') if p.is_file()}
        provider = Path('pi-cliproxyapi-provider/config.json')
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://proxy.test',
                                     'ANTHROPIC_AUTH_TOKEN': 'dummy'}):
            with patch.object(self.setup, 'fetch_models', return_value=[{'id': 'codex/new-model'}]) as fetch:
                with patch.object(sys, 'argv', ['pi-setup.py', 'aliases', str(self.home)]):
                    self.setup.main()
        fetch.assert_called_once_with('http://proxy.test/v1', 'dummy')
        config = json.loads((self.agent / provider).read_text())
        self.assertEqual(config['modelAliases']['codex/new-model'], 'openai/new-model')
        self.assertNotIn('codex/gpt-6.1-sol', config['modelAliases'])
        self.assertEqual({p.relative_to(self.agent): p.read_bytes() for p in self.agent.rglob('*')
                          if p.is_file() and p.relative_to(self.agent) != provider},
                         {p: data for p, data in before.items() if p != provider})

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

    def test_wrapper_refreshes_aliases_with_authentication_before_starting_pi(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get('Authorization')))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'data': [{'id': 'codex/new-model'}]}).encode())

            def log_message(self, *args):
                pass

        binary = self.home / 'fake-pi'
        binary.write_text("#!/usr/bin/env python3\nprint('wrapper OK')\n")
        binary.chmod(0o700)
        wrapper = self.home / 'pi.sh'
        wrapper.write_text((ROOT / 'pi.sh').read_text()
                           .replace('/tools/node_modules/.bin/pi', str(binary))
                           .replace('/scripts/pi-setup.py', str(ROOT / 'pi-setup.py')))
        server = HTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            result = subprocess.run(['bash', str(wrapper)],
                                    env={**os.environ, 'ANTHROPIC_AUTH_TOKEN': 'fake-key',
                                         'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{server.server_port}/',
                                         'HOME': str(self.home)},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'wrapper OK\n')
            self.assertEqual(requests, [('/v1/models', 'Bearer fake-key')])
            config = json.loads((self.agent / 'pi-cliproxyapi-provider/config.json').read_text())
            self.assertEqual(config['modelAliases']['codex/new-model'], 'openai/new-model')
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
assert os.environ['CLIPROXYAPI_BASE_URL'] == 'http://127.0.0.1:9/v1'
assert os.environ['CLIPROXYAPI_PROVIDER_NAME'] == 'cliproxy'
assert sys.argv[1:] == ['--mode', 'rpc', 'argument with spaces']
print('wrapper OK')
''')
        binary.chmod(0o700)
        wrapper = self.home / 'pi.sh'
        wrapper.write_text((ROOT / 'pi.sh').read_text()
                           .replace('/tools/node_modules/.bin/pi', str(binary))
                           .replace('/scripts/pi-setup.py', str(ROOT / 'pi-setup.py')))
        result = subprocess.run(['bash', str(wrapper), '--mode', 'rpc', 'argument with spaces'],
                                env={**os.environ, 'ANTHROPIC_AUTH_TOKEN': 'fake-key',
                                     'ANTHROPIC_BASE_URL': 'http://127.0.0.1:9/', 'HOME': str(self.home)},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'wrapper OK\n')
        self.assertIn('pi-setup: model discovery failed; previous provider config kept', result.stderr)
        environment = {**os.environ, 'ANTHROPIC_AUTH_TOKEN': 'fake-key',
                       'ANTHROPIC_BASE_URL': 'http://127.0.0.1:9/'}
        environment.pop('HOME', None)
        result = subprocess.run(['bash', str(wrapper), '--mode', 'rpc', 'argument with spaces'],
                                env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'wrapper OK\n')
        for name in ['ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL']:
            environment = {**os.environ, 'ANTHROPIC_AUTH_TOKEN': 'fake-key',
                           'ANTHROPIC_BASE_URL': 'http://127.0.0.1:9/', 'HOME': str(self.home)}
            environment.pop(name)
            result = subprocess.run(['bash', str(wrapper)], env=environment,
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(name, result.stderr)

    def test_setup_errors_identify_action_and_failure(self):
        settings = self.agent / 'settings.json'
        settings.write_text('{broken')
        result = subprocess.run(['python3', str(ROOT / 'pi-setup.py'), 'write',
                                 str(self.home), self.safety], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pi-setup write: JSONDecodeError:', result.stderr)

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
        self.assertEqual(set(result['mcpServers']), set(hub.SERVERS) | {'mine'})
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
