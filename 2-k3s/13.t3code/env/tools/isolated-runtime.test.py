#!/usr/bin/env python3
"""Offline runtime, private-input, policy, laptop and render fixtures."""
import base64
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'env/files'), str(ROOT / 'one/files'), str(ROOT / 'env/tools')]
runtime = importlib.import_module('isolated-config')
fixture_spec = importlib.util.spec_from_file_location('private_fixture', ROOT / 'env/tools/private-config.test.py')
private_fixture = importlib.util.module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(private_fixture)
laptop = importlib.import_module('prepare-laptop')


def encoded(value, executable=False):
    if not isinstance(value, str):
        value = json.dumps(value)
    return {'data': base64.b64encode(value.encode()).decode(), 'executable': executable}


def refresh(directory):
    path = directory / 'runtime.json'
    config = json.loads(path.read_text())
    config['sha256'] = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in runtime.INPUTS}
    path.write_text(json.dumps(config))


def fixture(directory, model_url='http://127.0.0.1:18317'):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'bundle.json').write_text(json.dumps(private_fixture.fixture()))
    (directory / 'services.json').write_text(json.dumps({'environment': 'homelab', 'services': ['searxng']}))
    guard = '''import json, sys
command = sys.argv[1] if len(sys.argv) > 1 else json.load(sys.stdin)['tool_input']['command']
sys.exit(2 if command.startswith('git push') else 0)
'''
    policies = {'environment': 'homelab', 'files': {
        '.cc-safety-net/policy.json': encoded({'version': 1, 'secret_protection': {'deny_paths': ['/fixture/restricted']}}),
        '.claude/settings.json': encoded({'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [
            {'type': 'command', 'command': 'python3 "$HOME/.config/t3-policy/guard.py"'}]}]}}),
        '.config/t3-policy/guard.py': encoded(guard, executable=True)},
        'checks': [{'argv': ['python3', '${HOME}/.config/t3-policy/guard.py', 'git push'], 'exit': 2},
                   {'argv': ['python3', '${HOME}/.config/t3-policy/guard.py', 'git status'], 'exit': 0}]}
    (directory / 'policies.json').write_text(json.dumps(policies))
    kube = {'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'homelab',
            'clusters': [{'name': 'homelab', 'cluster': {'server': 'https://kube.homelab.example.test:6443',
                         'certificate-authority-data': base64.b64encode(b'synthetic-ca').decode()}}],
            'users': [{'name': 'homelab', 'user': {'token': 'synthetic-kube-grant'}}],
            'contexts': [{'name': 'homelab', 'context': {'cluster': 'homelab', 'user': 'homelab'}}]}
    (directory / 'kubeconfig').write_text(json.dumps(kube))
    with tempfile.TemporaryDirectory() as tmp:
        key = Path(tmp) / 'key'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
        (directory / 'authorized_keys').write_text(key.with_suffix('.pub').read_text())
    (directory / 'runtime.json').write_text(json.dumps({'environment': 'homelab',
        'hubUrl': 'http://mcp-hub.mcp-hub-homelab.svc.cluster.local:8000',
        'modelUrl': model_url, 'projectUrl': 'https://github.com/homelab/fixture.git',
        'gitAuth': 'public', 'approvals': {name: 'synthetic-offline-review' for name in runtime.APPROVALS},
        'grantSha256': {name: hashlib.sha256(value.encode()).hexdigest() for name, value in {
            'model-token': 'synthetic-model-grant', 'hub-token': 'homelab.' + 'h' * 43}.items()}, 'sha256': {}}))
    refresh(directory)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.inputs, self.home = self.root / 'inputs', self.root / 'home'
        self.home.mkdir()
        fixture(self.inputs)
        self.env = patch.dict(os.environ, {'PATH': os.environ['PATH'], 'MCP_HUB_TOKEN': 'homelab.' + 'h' * 43,
            'ANTHROPIC_AUTH_TOKEN': 'synthetic-model-grant'}, clear=True)
        self.env.start()
        self.requirements = self.root / 'etc/codex/requirements.toml'
        self.requirements_patch = patch.object(runtime, 'CODEX_REQUIREMENTS', self.requirements, create=True)
        self.requirements_patch.start()
        self.guidance_patch = patch.object(runtime, 'GUIDANCE', ROOT / 'env/files/isolated-guidance.md')
        self.guidance_patch.start()

    def tearDown(self):
        self.guidance_patch.stop()
        self.requirements_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def validate(self):
        return runtime.validate(self.inputs, self.home, 'homelab')

    def prepare(self):
        runtime.prepare(self.inputs, self.home, 'homelab')
        path = self.home / '.pi/agent/pi-cliproxyapi-provider/config.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'providerName': 'cliproxy', 'baseUrl': 'http://127.0.0.1:18317/v1',
                                    'modelAliases': {'claude/fixture': 'anthropic/fixture'}}))

    def change(self, name, transform):
        path = self.inputs / name
        obj = json.loads(path.read_text())
        transform(obj)
        path.write_text(json.dumps(obj))
        refresh(self.inputs)

    def test_isolated_instructions_keep_their_ssh_guides(self):
        payload = (self.inputs / 'bundle.json').read_bytes()
        guidance = (ROOT / 'env/files/isolated-guidance.md').read_text()
        source = base64.b64decode(json.loads(payload)['files']['instructions.md']['data']).decode()
        expected = source.replace('/fixture/', str(self.home) + '/') + guidance
        setup = importlib.import_module('pi-setup')
        ssh = ROOT.parent / '13.t3code-homelab/files/homelab-ssh.md'
        for _ in range(2):
            self.prepare()
            with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://127.0.0.1:18317'}), \
                    patch.object(setup, 'fetch_models', return_value=[{'id': 'codex/fixture'}]):
                setup.write(self.home, '/fixture/safety', ssh)
            for name in ('.claude/AGENTS.md', '.codex/AGENTS.md'):
                self.assertEqual((self.home / name).read_text(), expected)
            self.assertEqual((self.home / '.pi/agent/AGENTS.md').read_text(), expected + '\n' + ssh.read_text())
            self.assertIn(ssh.read_text(), (self.home / '.pi/agent/AGENTS.md').read_text())
            append = next(line for line in (ROOT / 'env/files/isolated-entrypoint.sh').read_text().splitlines()
                          if line.startswith('cat ') and 'AGENTS.override.md' in line)
            subprocess.run(['bash', '-eu', '-c', append], check=True,
                           env={**os.environ, 'HOME': str(self.home), 'ssh_instructions': str(ssh)})
            self.assertIn(ssh.read_text(), (self.home / '.codex/AGENTS.override.md').read_text())
            self.assertEqual((self.inputs / 'bundle.json').read_bytes(), payload)

    def test_google_inventory_digest_mismatch_stops_startup(self):
        before = json.loads((self.inputs / 'runtime.json').read_text())
        other_inputs = {name: (self.inputs / name).read_bytes() for name in runtime.INPUTS if name != 'services.json'}
        stored_services_bytes = b'{"environment":"homelab","services":["searxng","gmail","drive"]}\n'
        (self.inputs / 'services.json').write_bytes(stored_services_bytes)
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            runtime.prepare(self.inputs, self.home, 'homelab')
        self.assertFalse((self.home / '.config/t3-runtime/services.json').exists())
        self.assertFalse((self.home / '.pi/agent/mcp.json').exists())
        reviewed = copy.deepcopy(before)
        reviewed['sha256']['services.json'] = hashlib.sha256(stored_services_bytes).hexdigest()
        (self.inputs / 'runtime.json').write_text(json.dumps(reviewed))
        self.validate()
        self.prepare()
        runtime.apply(self.inputs, self.home, 'homelab')
        after = json.loads((self.inputs / 'runtime.json').read_text())
        runtime_sha256 = after['sha256']
        self.assertEqual(runtime_sha256['services.json'], hashlib.sha256(stored_services_bytes).hexdigest())
        self.assertEqual(after['approvals'], before['approvals'])
        self.assertEqual(after['grantSha256'], before['grantSha256'])
        self.assertEqual({key: value for key, value in runtime_sha256.items() if key != 'services.json'},
                         {key: value for key, value in before['sha256'].items() if key != 'services.json'})
        self.assertEqual({name: (self.inputs / name).read_bytes() for name in other_inputs}, other_inputs)
        self.assertEqual((self.inputs / 'services.json').read_bytes(), stored_services_bytes)
        self.assertEqual(json.loads((self.home / '.config/t3-runtime/services.json').read_text()),
                         {'environment': 'homelab', 'services': ['searxng', 'gmail', 'drive']})
        homelab_config = json.loads((self.home / '.pi/agent/mcp.json').read_text())
        self.assertEqual(set(homelab_config['mcpServers']), {'searxng', 'gmail', 'drive'})
        for name, entry in homelab_config['mcpServers'].items():
            self.assertEqual(entry['url'], 'http://mcp-hub.mcp-hub-homelab.svc.cluster.local:8000/' + name)
            self.assertEqual(entry['headers'], {'Authorization': 'Bearer ${MCP_HUB_TOKEN}'})

    def test_reviewed_services_snapshot_and_removed_client_cleanup_on_restart(self):
        self.change('services.json', lambda obj: obj.update(services=['searxng', 'gmail', 'drive']))
        (self.inputs / 'services.json').chmod(0o440)
        self.prepare()
        runtime.apply(self.inputs, self.home, 'homelab')
        snapshot = self.home / '.config/t3-runtime/services.json'
        self.assertEqual(snapshot.read_bytes(), (self.inputs / 'services.json').read_bytes())
        self.assertEqual(snapshot.stat().st_uid, os.getuid())
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(json.loads((self.home / '.pi/agent/mcp.json').read_text())['mcpServers']),
                         {'searxng', 'gmail', 'drive'})
        (self.inputs / 'services.json').chmod(0o600)
        self.change('services.json', lambda obj: obj.update(services=['searxng']))
        runtime.prepare(self.inputs, self.home, 'homelab')
        runtime.apply(self.inputs, self.home, 'homelab')
        for name in ('.pi/agent/mcp.json', '.claude.json', '.claude/.claude.json'):
            self.assertEqual(set(json.loads((self.home / name).read_text())['mcpServers']), {'searxng'})
        codex = tomllib.loads((self.home / '.codex/config.toml').read_text())
        self.assertEqual(set(codex['mcp_servers']), {'searxng'})
        self.assertEqual(snapshot.read_bytes(), (self.inputs / 'services.json').read_bytes())
        (self.inputs / 'services.json').write_text('{}')
        with self.assertRaises(ValueError):
            runtime.prepare(self.inputs, self.home, 'homelab')
        self.assertFalse(snapshot.exists())

    def test_t3_rejects_every_vault_client_variable_including_empty_values(self):
        with patch.dict(os.environ, {'T3_VAULT_CLIENT': '1'}):
            self.validate()
            for name in ('BW_EMAIL', 'BW_PASSWORD', 'BW_SERVER', 'BW_ORGANIZATION_ID', 'BW_COLLECTION_ID',
                         'BW_SESSION', 'BW_CLIENTID', 'BW_CLIENTSECRET', 'BW_UNKNOWN', 'BITWARDENCLI_APPDATA_DIR'):
                for value in ('synthetic-unapproved', ''):
                    with self.subTest(name=name, present=bool(value)), patch.dict(os.environ, {name: value}):
                        with self.assertRaises(ValueError):
                            self.validate()

    def test_clean_home_policies_hub_and_provider_state_survive_restart(self):
        self.validate()
        self.prepare()
        tool = self.home / '.local/bin/installed-tool'
        tool.parent.mkdir(parents=True)
        tool.write_text('#!/bin/sh\nexit 0\n')
        tool.chmod(0o755)
        planted_python = tool.with_name('python3')
        planted_python.write_text('#!/bin/sh\nexit 97\n')
        planted_python.chmod(0o755)
        os.environ['PATH'] = '/tools/node_modules/.bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:' + str(tool.parent)
        gitconfig = self.home / '.gitconfig'
        identity = '[user]\n\tname = Spyros Psarras\n\temail = 13405649+SpyrosPsarras@users.noreply.github.com\n'
        gitconfig.write_text(identity)
        pi = self.home / '.pi/agent/mcp.json'
        pi.parent.mkdir(parents=True, exist_ok=True)
        pi.write_text(json.dumps({'mcpServers': {'unapproved': {'url': 'https://work.example.test'}}}))
        state = self.home / '.t3/userdata'
        state.mkdir(parents=True)
        (state / 'settings.json').write_text(json.dumps({'providerInstances': {'work': {'driver': 'pi'}},
                                                      'theme': 'fixture'}))
        runtime.apply(self.inputs, self.home, 'homelab')
        (state / 'history-fixture').write_text('homelab-only-history')
        runtime.prepare(self.inputs, self.home, 'homelab')
        runtime.apply(self.inputs, self.home, 'homelab')
        self.assertEqual(subprocess.check_output(['bash', '-c', 'command -v installed-tool'], text=True).strip(), str(tool))
        self.assertEqual(subprocess.run(['installed-tool']).returncode, 0)
        self.assertEqual(subprocess.check_output(['bash', '-c', 'command -v python3'], text=True).strip(), '/usr/bin/python3')
        self.assertEqual(subprocess.check_output(['python3', '-c', 'import sys; print(sys.executable)'], text=True).strip(), '/usr/bin/python3')
        self.assertEqual((state / 'history-fixture').read_text(), 'homelab-only-history')
        self.assertEqual(gitconfig.read_text(), identity)
        settings = json.loads((state / 'settings.json').read_text())
        self.assertEqual(set(settings['providerInstances']), {'pi', 'codex', 'claudeAgent'})
        self.assertEqual(settings['theme'], 'fixture')
        for name in ('.pi/agent/mcp.json', '.claude.json', '.claude/.claude.json'):
            servers = json.loads((self.home / name).read_text())['mcpServers']
            self.assertEqual(set(servers), {'searxng'})
            self.assertEqual(servers['searxng']['url'], 'http://mcp-hub.mcp-hub-homelab.svc.cluster.local:8000/searxng')
            self.assertNotIn(os.environ['MCP_HUB_TOKEN'], (self.home / name).read_text())
        import tomllib
        codex = tomllib.loads((self.home / '.codex/config.toml').read_text())
        self.assertEqual(set(codex['mcp_servers']), {'searxng'})
        self.assertEqual(codex['model_provider'], 'cliproxy')
        safety = json.loads((self.home / '.cc-safety-net/policy.json').read_text())
        self.assertIn('/fixture/restricted', safety['secret_protection']['deny_paths'])
        self.assertIn(str(self.inputs), safety['secret_protection']['deny_paths'])
        self.assertIn(str(self.home / '.config/t3-sshd'), safety['secret_protection']['deny_paths'])
        hook = self.home / '.config/t3-policy/guard.py'
        result = subprocess.run(['python3', str(hook)], input=json.dumps({'tool_input': {'command': 'git push'}}), text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.home / runtime.LEGACY_PERMISSION_PATH).exists())

    def test_legacy_permission_input_is_reviewed_but_not_installed(self):
        self.change('policies.json', lambda obj: obj['files'].update({
            runtime.LEGACY_PERMISSION_PATH: encoded({'permission': 'ask'})}))
        self.validate()
        self.prepare()
        runtime.apply(self.inputs, self.home, 'homelab')
        policy = self.home / runtime.LEGACY_PERMISSION_PATH
        self.assertFalse(policy.exists())
        policy.parent.mkdir(parents=True)
        policy.write_text('stale rollback policy')
        runtime.apply(self.inputs, self.home, 'homelab')
        self.assertEqual(policy.read_text(), 'stale rollback policy')
        path = self.inputs / 'policies.json'
        path.write_text(path.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            self.validate()

    def test_legacy_permission_acceptance_checks_are_not_silently_skipped(self):
        self.change('policies.json', lambda obj: obj['files'].update({
            runtime.LEGACY_PERMISSION_PATH: encoded({'permission': 'ask'})}))
        self.change('policies.json', lambda obj: obj['checks'].append({
            'argv': ['python3', '-c', 'from pathlib import Path; import sys; assert Path(sys.argv[1]).exists()',
                     '${HOME}/' + runtime.LEGACY_PERMISSION_PATH], 'exit': 0}))
        self.prepare()
        with self.assertRaisesRegex(ValueError, 'policy acceptance check failed'):
            runtime.apply(self.inputs, self.home, 'homelab')

    def test_legacy_permission_input_still_requires_valid_encoding(self):
        self.change('policies.json', lambda obj: obj['files'].update({
            runtime.LEGACY_PERMISSION_PATH: {'data': '!invalid-base64!'}}))
        with self.assertRaises(ValueError):
            self.validate()

    def test_required_safety_and_hook_inputs_cannot_be_omitted(self):
        for path in runtime.POLICY_PATHS:
            fixture(self.inputs)
            self.change('policies.json', lambda obj: obj['files'].pop(path))
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate()

    def test_compliant_project_files_are_preserved_byte_for_byte(self):
        self.prepare()
        for content in ('{ "unrelatedPreference": "fixture" }\n',
                        '{ "mcpServers": {}, "disableAllHooks": false,\n'
                        '  "enableAllProjectMcpServers": false, "enabledMcpjsonServers": [] }\n'):
            paths = [self.home / name for name in ('.claude/settings.local.json',
                'projects/homelab/.claude/settings.json', 'projects/homelab/.claude/settings.local.json')]
            paths.append(self.home / 'projects/homelab/.mcp.json')
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            before = {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in paths}
            runtime.apply(self.inputs, self.home, 'homelab')
            for path in paths:
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino), before[path])

    def test_project_mcp_json_only_changes_its_server_map(self):
        self.prepare()
        path = self.home / 'projects/homelab/.mcp.json'
        path.parent.mkdir(parents=True)
        original = {'mcpServers': {'work': {'url': 'https://work.example.test/mcp'}},
                    'unrelatedPreference': 'fixture'}
        path.write_text(json.dumps(original))
        runtime.apply(self.inputs, self.home, 'homelab')
        self.assertEqual(json.loads(path.read_text()), {**original, 'mcpServers': {}})
        before = path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino
        runtime.apply(self.inputs, self.home, 'homelab')
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino), before)

    def test_codex_requirements_match_reviewed_provider_and_hub_inventory(self):
        self.change('services.json', lambda obj: obj.update(services=['searxng', 'gmail', 'drive']))
        self.prepare()
        runtime.apply(self.inputs, self.home, 'homelab')
        codex = tomllib.loads((self.home / '.codex/config.toml').read_text())
        requirements = tomllib.loads(self.requirements.read_text())
        self.assertEqual(requirements['model_provider'], codex['model_provider'])
        self.assertEqual(requirements['model_providers'], codex['model_providers'])
        self.assertEqual(requirements['mcp_servers'], {
            'searxng': {'identity': {'url': 'http://mcp-hub.mcp-hub-homelab.svc.cluster.local:8000/searxng'}},
            'gmail': {'identity': {'url': 'http://mcp-hub.mcp-hub-homelab.svc.cluster.local:8000/gmail'}},
            'drive': {'identity': {'url': 'http://mcp-hub.mcp-hub-homelab.svc.cluster.local:8000/drive'}}})
        self.assertEqual(set(codex['mcp_servers']), {'searxng', 'gmail', 'drive'})
        for name, server in codex['mcp_servers'].items():
            self.assertEqual(server['url'], requirements['mcp_servers'][name]['identity']['url'])
            self.assertEqual(server['bearer_token_env_var'], 'MCP_HUB_TOKEN')
        self.assertEqual(self.requirements.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(os.environ['MCP_HUB_TOKEN'], self.requirements.read_text())

    def test_codex_last_good_config_survives_failed_write_and_replace(self):
        self.prepare()
        runtime.apply(self.inputs, self.home, 'homelab')
        path = self.home / '.codex/config.toml'
        previous = path.read_bytes()
        previous_files = set(path.parent.iterdir())
        original_fdopen = os.fdopen
        class FailedWrite:
            def __init__(self, fd, mode):
                self.file = original_fdopen(fd, mode)
            def __enter__(self):
                return self
            def write(self, data):
                self.file.write(data[:10])
                self.file.flush()
                raise OSError('synthetic failed write')
            def __exit__(self, *args):
                self.file.close()
        def failed_replace(source, destination):
            self.assertEqual(Path(source).parent, path.parent)
            self.assertEqual(Path(source).stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(destination), path)
            raise OSError('synthetic failed replacement')
        for name, replacement in [('fdopen', FailedWrite), ('replace', failed_replace)]:
            with self.subTest(failure=name), patch.object(runtime.os, name, replacement):
                with self.assertRaises(OSError):
                    runtime.write_toml(path, {'model': 'synthetic-new-value'})
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(set(path.parent.iterdir()), previous_files)

    def test_policy_last_good_bytes_and_modes_survive_failed_replacement(self):
        path = self.home / 'policy-fixture'
        path.write_bytes(b'last-good-policy')
        path.chmod(0o600)
        before = set(self.home.iterdir())
        with patch.object(runtime.os, 'replace', side_effect=OSError('synthetic replace failure')):
            with self.assertRaises(OSError): runtime.write_private(path, b'new-policy', 0o700)
        self.assertEqual(path.read_bytes(), b'last-good-policy')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(self.home.iterdir()), before)
        runtime.write_private(path, b'new-policy', 0o700)
        self.assertEqual(path.read_bytes(), b'new-policy')
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_unmanaged_toml_values_round_trip(self):
        values = tomllib.loads('''text = "quote \\" and Unicode 🏠 and \\u007f"
flag = true
count = 42
ratio = 1.5
unbounded = inf
instant = 2026-01-01T12:00:00Z
day = 2026-01-01
clock = 12:00:00
nested = {"🏠" = ["fixture", {enabled = false}]}
''')
        encoded = '\n'.join(runtime.toml_value(key) + ' = ' + runtime.toml_value(value) for key, value in values.items())
        self.assertEqual(tomllib.loads(encoded), values)

    def test_pi_text_generation_selections_and_options_survive_restart(self):
        self.prepare()
        path = self.home / '.t3/userdata/settings.json'
        path.parent.mkdir(parents=True)
        canonical = {'instanceId': 'pi', 'model': 'cliproxy-textgen/claude/claude-haiku-5-5'}
        options = [{'id': 'thinking', 'value': 'high'}]
        cases = [
            ({'instanceId': 'pi', 'model': 'cliproxy/claude/fixture', 'options': options},
             {**canonical, 'options': options}),
            ({'instanceId': 'pi', 'model': 'other/fixture', 'options': []},
             {**canonical, 'options': []}),
            ({'instanceId': 'pi', 'model': 'cliproxy-textgen/'}, canonical),
            ({'instanceId': 'pi', 'model': 'other/fixture', 'options': 'invalid'}, canonical),
            ({'instanceId': 'pi'}, canonical),
            ({'instanceId': 'pi', 'model': 'cliproxy-textgen/claude/fixture', 'options': options}, None),
            ({'instanceId': 'codex', 'model': 'codex/fixture', 'options': options}, canonical)]
        default = {'instanceId': 'pi', 'model': 'cliproxy/claude/fixture', 'options': options}
        for selection, expected in cases:
            with self.subTest(selection=selection):
                path.write_text(json.dumps({'defaultModelSelection': default,
                                            'textGenerationModelSelection': selection}))
                for restart in range(2):
                    with self.subTest(restart=restart):
                        runtime.apply(self.inputs, self.home, 'homelab')
                        settings = json.loads(path.read_text())
                        self.assertEqual(settings['textGenerationModelSelection'],
                                         selection if expected is None else expected)
                        self.assertEqual(settings['defaultModelSelection'], default)
                        if restart == 0:
                            first = path.read_bytes()
                        else:
                            self.assertEqual(path.read_bytes(), first)

    def test_pi_launch_args_reset_to_canonical_direct_tools_on_restart(self):
        self.prepare()
        path = self.home / '.t3/userdata/settings.json'
        path.parent.mkdir(parents=True)
        for original in ('--unapproved', '--extension /fixture/unapproved.ts',
                         '--provider fixture --model fixture --api-key synthetic-fixture',
                         '--no-extensions', '--thinking high --tools=+codemode,-write',
                         '--extension "unterminated'):
            with self.subTest(launch_args=original):
                path.write_text(json.dumps({'providerInstances': {'pi': {'driver': 'pi', 'config': {
                    'launchArgs': original}}}}))
                for _ in range(2):
                    runtime.prepare(self.inputs, self.home, 'homelab')
                    runtime.apply(self.inputs, self.home, 'homelab')
                    args = json.loads(path.read_text())['providerInstances']['pi']['config'].get('launchArgs', '')
                    self.assertEqual(shlex.split(args), [
                        '--tools=+mcp__t3-code__orchestrator_capabilities,'
                        '+mcp__t3-code__delegate_task,+mcp__t3-code__task_status'])

    def test_provider_preferences_and_codex_trust_survive_managed_configuration(self):
        self.prepare()
        state = self.home / '.t3/userdata'
        state.mkdir(parents=True)
        settings = {'providerInstances': {
            'pi': {'driver': 'pi', 'displayName': 'Homelab Pi', 'enabled': False,
                   'config': {'binaryPath': '/unapproved/pi', 'launchArgs': '--unapproved',
                              'customModels': ['cliproxy/claude/fixture', 'work/fixture']}},
            'codex': {'driver': 'codex', 'displayName': 'Homelab Codex', 'enabled': True,
                      'config': {'launchArgs': '-c model_provider="work"',
                                 'customModels': ['codex/fixture', 'work/fixture']}},
            'claudeAgent': {'driver': 'claudeAgent', 'enabled': True},
            'work': {'driver': 'codex', 'enabled': True}},
            'providers': {'work': {}}, 'theme': 'fixture',
            'defaultModelSelection': {'instanceId': 'codex', 'model': 'codex/fixture',
                                      'options': [{'id': 'reasoningEffort', 'value': 'high'}]},
            'textGenerationModelSelection': {'instanceId': 'work', 'model': 'work/fixture'}}
        (state / 'settings.json').write_text(json.dumps(settings))
        project = str(self.home / 'projects/homelab')
        codex_path = self.home / '.codex/config.toml'
        codex_path.parent.mkdir(exist_ok=True)
        codex_path.write_text('model = "codex/fixture"\nmodel_reasoning_effort = "high"\n'
            'model_provider = "work"\nprofile = "work"\n'
            '[model_providers.work]\nbase_url = "https://models.work.example.test/v1"\n'
            '[model_providers.cliproxy]\nbase_url = "https://unapproved.example.test/v1"\n'
            '[mcp_servers.work]\nurl = "https://mcp.work.example.test"\n'
            '[profiles.work]\nmodel_provider = "work"\n'
            '[features]\nundo = true\n[tui]\nnotifications = ["agent-turn-complete"]\n'
            '[history]\nmax_bytes = 100000\n'
            '[projects.' + json.dumps(project) + ']\ntrust_level = "trusted"\n'
            '[projects.' + json.dumps(project + '/child') + ']\ntrust_level = "trusted"\n'
            '[projects."/fixture/work"]\ntrust_level = "trusted"\n')
        for restart in range(2):
            with self.subTest(restart=restart):
                runtime.apply(self.inputs, self.home, 'homelab')
                retained = json.loads((state / 'settings.json').read_text())
                instances = retained['providerInstances']
                self.assertEqual(set(instances), {'pi', 'codex', 'claudeAgent'})
                self.assertEqual(instances['pi']['displayName'], 'Homelab Pi')
                self.assertFalse(instances['pi']['enabled'])
                self.assertFalse(instances['codex']['enabled'])
                self.assertFalse(instances['claudeAgent']['enabled'])
                self.assertEqual(instances['pi']['config'], {'binaryPath': '/scripts/pi.sh',
                    'launchArgs': '--tools=+mcp__t3-code__orchestrator_capabilities,'
                                  '+mcp__t3-code__delegate_task,+mcp__t3-code__task_status',
                    'customModels': ['cliproxy/claude/fixture']})
                self.assertEqual(instances['codex']['config']['customModels'], ['codex/fixture'])
                self.assertIn('http://127.0.0.1:18317/v1', instances['codex']['config']['launchArgs'])
                self.assertNotIn('work', instances['codex']['config']['launchArgs'])
                self.assertEqual(retained['defaultModelSelection'],
                                 {'instanceId': 'pi', 'model': 'cliproxy/claude/claude-opus-5-5'})
                self.assertEqual(retained['textGenerationModelSelection']['instanceId'], 'pi')
                self.assertNotIn('providers', retained)
                self.assertEqual(retained['theme'], 'fixture')
                codex = tomllib.loads(codex_path.read_text())
                self.assertEqual(codex['model'], 'codex/fixture')
                self.assertEqual(codex['model_reasoning_effort'], 'high')
                self.assertEqual(codex['features'], {'undo': True})
                self.assertEqual(codex['tui'], {'notifications': ['agent-turn-complete']})
                self.assertEqual(codex['history'], {'max_bytes': 100000})
                self.assertEqual(set(codex['projects']), {project, project + '/child'})
                self.assertEqual(codex['projects'][project], {'trust_level': 'trusted'})
                self.assertEqual(codex['model_provider'], 'cliproxy')
                self.assertEqual(codex['model_providers'], {'cliproxy': {'name': 'cliproxy',
                    'base_url': 'http://127.0.0.1:18317/v1', 'env_key': 'ANTHROPIC_AUTH_TOKEN', 'wire_api': 'responses'}})
                self.assertEqual(set(codex['mcp_servers']), {'searxng'})
                self.assertNotIn('profile', codex)
                self.assertNotIn('profiles', codex)

    def test_nested_claude_project_mcp_is_cleared_without_losing_safe_state_or_hooks(self):
        self.prepare()
        project = str(self.home / 'projects/homelab')
        unapproved = {'work': {'command': 'unapproved-fixture'}}
        project_state = {'mcpServers': unapproved, 'enableAllProjectMcpServers': True,
                         'enabledMcpjsonServers': ['work'], 'hasTrustDialogAccepted': True,
                         'allowedTools': ['Read'], 'unrelatedPreference': 'fixture'}
        for name in ('.claude.json', '.claude/.claude.json'):
            path = self.home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'mcpServers': unapproved, 'projects': {project: project_state},
                                        'theme': 'fixture', 'enableAllProjectMcpServers': True,
                                        'enabledMcpjsonServers': ['work']}))
        hook = {'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'true'}]}]}
        for name in ('.claude/settings.local.json', 'projects/homelab/.claude/settings.json',
                     'projects/homelab/.claude/settings.local.json', 'projects/homelab/.mcp.json'):
            path = self.home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({**project_state, 'hooks': hook, 'disableAllHooks': True}))
        for restart in range(2):
            with self.subTest(restart=restart):
                runtime.apply(self.inputs, self.home, 'homelab')
                for name in ('.claude.json', '.claude/.claude.json'):
                    state = json.loads((self.home / name).read_text())
                    self.assertEqual(set(state['mcpServers']), {'searxng'})
                    self.assertEqual(state['theme'], 'fixture')
                    self.assertFalse(state['enableAllProjectMcpServers'])
                    self.assertEqual(state['enabledMcpjsonServers'], [])
                    nested = state['projects'][project]
                    self.assertEqual(nested, {**project_state, 'mcpServers': {},
                                             'enableAllProjectMcpServers': False, 'enabledMcpjsonServers': []})
                for name in ('.claude/settings.local.json', 'projects/homelab/.claude/settings.json',
                             'projects/homelab/.claude/settings.local.json', 'projects/homelab/.mcp.json'):
                    state = json.loads((self.home / name).read_text())
                    self.assertEqual(state['mcpServers'], {})
                    self.assertEqual(state['hooks'], hook)
                    if name.endswith('/.mcp.json'):
                        self.assertEqual(state, {**project_state, 'mcpServers': {}, 'hooks': hook,
                                                 'disableAllHooks': True})
                    else:
                        self.assertFalse(state['enableAllProjectMcpServers'])
                        self.assertEqual(state['enabledMcpjsonServers'], [])
                        self.assertFalse(state['disableAllHooks'])
                    self.assertEqual(state['unrelatedPreference'], 'fixture')
                approved = json.loads((self.home / '.claude/settings.json').read_text())
                policies = json.loads((self.inputs / 'policies.json').read_text())
                self.assertEqual(approved, json.loads(base64.b64decode(policies['files']['.claude/settings.json']['data'])))

    def test_approved_claude_policy_cannot_auto_enable_project_servers(self):
        for key, value in (('enableAllProjectMcpServers', True), ('enabledMcpjsonServers', ['work']),
                           ('mcpServers', {'work': {'command': 'unapproved-fixture'}})):
            fixture(self.inputs)
            def add_setting(policies):
                settings = json.loads(base64.b64decode(policies['files']['.claude/settings.json']['data']))
                settings[key] = value
                policies['files']['.claude/settings.json'] = encoded(settings)
            self.change('policies.json', add_setting)
            with self.subTest(key=key), self.assertRaises(ValueError): self.validate()

    def test_mixed_home_or_wrong_environment_cannot_be_adopted(self):
        (self.home / '.t3').mkdir()
        (self.home / '.t3/mixed-history').write_text('must not import')
        with self.assertRaises(ValueError): self.validate()
        shutil.rmtree(self.home / '.t3')
        runtime.prepare(self.inputs, self.home, 'homelab')
        (self.home / '.azure').mkdir()
        with self.assertRaises(ValueError): self.validate()
        (self.home / '.azure').rmdir()
        (self.home / '.config/t3-runtime/environment').write_text('work\n')
        with self.assertRaises(ValueError): self.validate()

    def test_missing_inputs_digests_grants_and_credential_environment_fail_closed(self):
        (self.inputs / 'services.json').write_text('{}')
        with self.assertRaises(ValueError): self.validate()
        fixture(self.inputs)
        for name, value in [('MCP_HUB_TOKEN', 'legacy'), ('MCP_HUB_TOKEN', 'work.' + 'w' * 43),
                            ('ANTHROPIC_AUTH_TOKEN', ''), ('ANTHROPIC_AUTH_TOKEN', 'unreviewed-model-grant'), ('GITHUB_TOKEN', 'unapproved'),
                            ('SSH_AUTH_SOCK', '/fixture/agent'), ('GH_TOKEN', 'unapproved'), ('AZURE_CONFIG_DIR', '/fixture/cloud'),
                            ('BW_SESSION', 'unapproved')]:
            with self.subTest(name=name), patch.dict(os.environ, {name: value}):
                with self.assertRaises(ValueError): self.validate()
        (self.inputs / 'policies.json').unlink()
        with self.assertRaises(ValueError): self.validate()

    def test_invalid_hub_inventory_urls_and_vault_are_rejected(self):
        for services in (['searxng', 'vaultwarden'], ['vault-secret'], ['unknown'], [], ['searxng', 'searxng']):
            with self.subTest(services=services):
                self.change('services.json', lambda obj: obj.update(services=services))
                with self.assertRaises(ValueError): self.validate()
        fixture(self.inputs)
        self.change('services.json', lambda obj: obj.update(environment='work'))
        with self.assertRaises(ValueError): self.validate()
        fixture(self.inputs)
        self.change('runtime.json', lambda obj: obj.update(hubUrl='http://mcp-hub.mcp-hub.svc.cluster.local:8000'))
        with self.assertRaises(ValueError): self.validate()
        self.change('runtime.json', lambda obj: obj.update(modelUrl='https://user:pass@model.example.test'))
        with self.assertRaises(ValueError): self.validate()

    def test_kubeconfig_external_auth_and_multiple_contexts_are_rejected(self):
        for key, value in [('exec', {'command': 'az'}), ('auth-provider', {'name': 'azure'}),
                           ('client-key', '/fixture/key')]:
            fixture(self.inputs)
            self.change('kubeconfig', lambda obj: obj['users'][0]['user'].update({key: value}))
            with self.subTest(key=key), self.assertRaises(ValueError): self.validate()
        fixture(self.inputs)
        self.change('kubeconfig', lambda obj: obj['contexts'].append(copy.deepcopy(obj['contexts'][0])))
        with self.assertRaises(ValueError): self.validate()

    def test_policy_must_keep_hooks_and_secret_protection(self):
        for path, replacement in [('.claude/settings.json', {'disableAllHooks': True}),
                                 ('.cc-safety-net/policy.json', {'version': 1, 'secret_protection': {'overrides': {'x': 'off'}}})]:
            fixture(self.inputs)
            self.change('policies.json', lambda obj: obj['files'].update({path: encoded(replacement)}))
            with self.subTest(path=path), self.assertRaises(ValueError): self.validate()
        fixture(self.inputs)
        self.change('policies.json', lambda obj: obj['files'].update({'../escape': encoded('unsafe')}))
        with self.assertRaises(ValueError): self.validate()
        fixture(self.inputs)
        self.prepare()
        self.change('policies.json', lambda obj: obj['checks'][0].update(exit=0))
        with self.assertRaises(ValueError): runtime.apply(self.inputs, self.home, 'homelab')

    def test_unreviewed_model_endpoint_and_extra_private_files_fail_closed(self):
        self.prepare()
        path = self.home / '.pi/agent/pi-cliproxyapi-provider/config.json'
        data = json.loads(path.read_text())
        data['baseUrl'] = 'https://models.work.example.test/v1'
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError): runtime.apply(self.inputs, self.home, 'homelab')
        (self.inputs / 'unapproved-login').write_text('fixture')
        with self.assertRaises(ValueError): self.validate()

    def test_laptop_preparation_is_offline_pinned_and_without_an_agent(self):
        directory = self.root / 'laptop'
        laptop.prepare(directory, 'ssh.homelab.example.test', 'node', 2222, self.inputs / 'authorized_keys')
        original = (directory / 'id_ed25519.pub').read_bytes()
        laptop.prepare(directory, 'ssh.homelab.example.test', 'node', 2222, self.inputs / 'authorized_keys')
        self.assertEqual((directory / 'id_ed25519.pub').read_bytes(), original)
        settings = subprocess.check_output(['ssh', '-G', '-F', str(directory / 'ssh_config'), 't3-homelab'], text=True)
        for expected in ('forwardagent no', 'identityagent none', 'identitiesonly yes', 'stricthostkeychecking true',
                         'passwordauthentication no', 'kbdinteractiveauthentication no'):
            self.assertIn(expected, settings)
        self.assertEqual((directory / 'id_ed25519').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError): laptop.prepare(directory, 'host\nForwardAgent yes', 'node', 22, self.inputs / 'authorized_keys')

    def test_laptop_tunnel_rejects_bad_inputs_without_connecting(self):
        config = self.root / 'ssh_config'
        config.write_text('synthetic-config')
        script = str(ROOT / 'env/tools/connect-laptop.sh')
        for path, port in [('relative', '13773'), (str(config), '1024'), (str(config), '65536'), (str(config), 'bad')]:
            result = subprocess.run(['bash', script, path, port], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
        binary = self.root / 'ssh'
        binary.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        binary.chmod(0o700)
        with patch.dict(os.environ, {'PATH': str(self.root) + ':' + os.environ['PATH']}):
            args = subprocess.check_output(['bash', script, str(config), '13773'], text=True)
        self.assertIn('IdentityAgent=none', args)
        self.assertIn('ForwardAgent=no', args)
        self.assertIn('127.0.0.1:13773:127.0.0.1:3773', args)

    def test_gitops_ssh_services_have_distinct_pinned_addresses(self):
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            for name in ('13.t3code', '13.t3code-homelab', '13.t3code-work'):
                shutil.copytree(ROOT.parent / name, tree / name, ignore=shutil.ignore_patterns('__pycache__'))
            private_fixture.overlay(tree / '13.t3code/one')
            addresses = []
            for overlay, expected in (('13.t3code/one', '192.168.10.112'),
                                      ('13.t3code-homelab', '192.168.10.113'),
                                      ('13.t3code-work', '192.168.10.114')):
                with self.subTest(overlay=overlay):
                    docs = list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(tree / overlay)])))
                    if overlay == '13.t3code/one':
                        sts = next(doc for doc in docs if doc['kind'] == 'StatefulSet')
                        self.assertEqual(sts['metadata']['name'], 't3code')
                        role = next(doc for doc in docs if doc['kind'] == 'Role')
                        self.assertEqual([rule['resourceNames'] for rule in role['rules']],
                                         [['t3code'], ['t3code-0'], ['t3code-0']])
                    if overlay != '13.t3code/one':
                        sts = next(doc for doc in docs if doc['kind'] == 'StatefulSet')
                        governing = next(doc for doc in docs if doc['kind'] == 'Service'
                                         and doc['metadata']['name'] == sts['spec']['serviceName'])
                        self.assertEqual(governing['spec']['ports'], [{'name': 'ssh', 'port': 2222, 'targetPort': 'ssh'}])
                        self.assertFalse(any(port['port'] == 3773 for doc in docs if doc['kind'] == 'Service'
                                             for port in doc['spec']['ports']))
                    service = next(doc for doc in docs if doc and doc['kind'] == 'Service'
                                   and doc['metadata']['name'] == 't3code-ssh')
                    self.assertEqual(service['spec']['type'], 'LoadBalancer')
                    address = service['spec'].get('loadBalancerIP')
                    self.assertEqual(address, expected)
                    addresses.append(address)
            self.assertEqual(len(set(addresses)), 3)

    def test_gitops_environment_labels_are_t3_only_and_content_hashed(self):
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            for name in ('13.t3code', '13.t3code-homelab', '13.t3code-work'):
                shutil.copytree(ROOT.parent / name, tree / name, ignore=shutil.ignore_patterns('__pycache__'))
            private_fixture.overlay(tree / '13.t3code/one')
            for overlay, label in (('13.t3code/one', 'manager'),
                                   ('13.t3code-homelab', 'homelab'), ('13.t3code-work', 'work')):
                with self.subTest(overlay=overlay):
                    directory = tree / overlay
                    docs = list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(directory)])))
                    pod = next(doc for doc in docs if doc and doc['kind'] == 'StatefulSet')['spec']['template']['spec']
                    t3 = next(container for container in pod['containers'] if container['name'] == 't3')
                    mount = next(mount for mount in t3['volumeMounts'] if mount['mountPath'] == '/etc/machine-info')
                    self.assertEqual(mount, {'name': 'machine-info', 'mountPath': '/etc/machine-info',
                                             'subPath': 'machine-info', 'readOnly': True})
                    volume = next(volume for volume in pod['volumes'] if volume['name'] == mount['name'])
                    reference = volume['configMap']['name']
                    config = next(doc for doc in docs if doc and doc['kind'] == 'ConfigMap'
                                  and doc['metadata']['name'] == reference)
                    self.assertEqual(config['data'], {'machine-info': f'PRETTY_HOSTNAME="{label}"'})
                    for container in pod['containers'] + pod['initContainers']:
                        if container['name'] != 't3':
                            self.assertFalse(any(mount['name'] == 'machine-info' or mount['mountPath'] == '/etc/machine-info'
                                                 for mount in container['volumeMounts']))
                    manifest = directory / 'kustomization.yaml'
                    before = manifest.read_text()
                    manifest.write_text(before.replace(f'PRETTY_HOSTNAME="{label}"', 'PRETTY_HOSTNAME="changed"'))
                    changed = list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(directory)])))
                    current = next(doc for doc in changed if doc and doc['kind'] == 'StatefulSet')['spec']['template']['spec']
                    updated = next(volume for volume in current['volumes'] if volume['name'] == 'machine-info')
                    self.assertNotEqual(reference, updated['configMap']['name'])
                    self.assertEqual(t3['image'], next(container for container in current['containers']
                                                       if container['name'] == 't3')['image'])

    def test_gitops_render_uses_stable_external_secrets_and_tracked_rollout_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / '2-k3s'
            tree.mkdir()
            shutil.copytree(ROOT, tree / ROOT.name, ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copytree(ROOT.parent / '13.t3code-homelab', tree / '13.t3code-homelab')
            overlay = tree / '13.t3code-homelab'
            import yaml
            def render():
                return [doc for doc in yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(overlay)])) if doc]
            docs = render()
            self.assertTrue(all(doc['metadata'].get('namespace', 't3code-homelab') == 't3code-homelab' for doc in docs))
            sts = next(doc for doc in docs if doc['kind'] == 'StatefulSet')
            spec = sts['spec']['template']['spec']
            self.assertEqual(sts['metadata']['name'], 'homelab')
            rollout = next(doc for doc in docs if doc['kind'] == 'CronJob')
            self.assertEqual(rollout['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['env'],
                             [{'name': 'STATEFULSET_NAME', 'value': 'homelab'}])
            role = next(doc for doc in docs if doc['kind'] == 'Role')
            self.assertEqual([rule['resourceNames'] for rule in role['rules']],
                             [['homelab'], ['homelab-0'], ['homelab-0']])
            self.assertEqual(sts['spec']['updateStrategy']['type'], 'OnDelete')
            self.assertFalse(spec['automountServiceAccountToken'])
            self.assertEqual({v['persistentVolumeClaim']['claimName'] for v in spec['volumes'] if 'persistentVolumeClaim' in v}, {'t3code-home', 't3code-vault'})
            self.assertNotIn('hostPath', json.dumps(spec))
            self.assertEqual({v['secret']['secretName'] for v in spec['volumes'] if 'secret' in v},
                             {'t3-runtime-inputs', 't3-build-kubeconfig'})
            build = next(v['secret'] for v in spec['volumes'] if v['name'] == 't3-build-kubeconfig')
            self.assertEqual((build['secretName'], build['optional'], build['items']),
                             ('t3-build-kubeconfig', True, [{'key': 'kubeconfig', 'path': 'kubeconfig'}]))
            for container in spec['containers'][:2]:
                self.assertIn({'name': 't3-build-kubeconfig', 'mountPath': '/run/t3-build', 'readOnly': True},
                              container['volumeMounts'])
            self.assertIn({'name': 'T3_BUILD_KUBECONFIG', 'value': '/run/t3-build/kubeconfig'}, spec['containers'][0]['env'])
            self.assertNotIn('KUBECONFIG', {e['name'] for e in spec['containers'][0]['env']})
            image = spec['containers'][0]['image']
            self.assertIn(':homelab-inputs-', image)
            self.assertEqual(spec['containers'][1]['image'], image)
            for doc in docs:
                if doc['kind'] in {'Job', 'CronJob'}:
                    job = doc['spec']['jobTemplate']['spec'] if doc['kind'] == 'CronJob' else doc['spec']
                    self.assertEqual(job['template']['spec']['containers'][0]['image'], image)
                    self.assertEqual(job['template']['spec']['affinity'], spec['affinity'])
            cm = next(doc for doc in docs if doc['kind'] == 'ConfigMap' and 'entrypoint.sh' in doc['data'])
            self.assertNotIn('vault-run.py', cm['data'])
            self.assertNotIn('aks-auth.sh', cm['data'])
            (tree / ROOT.name / 'env/files/isolated-entrypoint.sh').write_text('changed-script')
            changed = next(doc for doc in render() if doc['kind'] == 'ConfigMap' and 'entrypoint.sh' in doc['data'])
            self.assertNotEqual(cm['metadata']['name'], changed['metadata']['name'])
            self.assertFalse(any(doc['kind'] == 'Secret' for doc in docs))
            annotation = 't3code.epaflix.com/inputs-grants-revision'
            refs = {env['name']: env['valueFrom']['secretKeyRef'] for env in spec['containers'][0]['env'] if 'valueFrom' in env}
            self.assertEqual(refs, {
                'ANTHROPIC_AUTH_TOKEN': {'name': 't3-runtime-grants', 'key': 'model-token'},
                'MCP_HUB_TOKEN': {'name': 't3-runtime-grants', 'key': 'hub-token'},
                'GITHUB_TOKEN': {'name': 't3-runtime-grants', 'key': 'github-token', 'optional': True}})
            manifest = overlay / 'statefulset.yaml'
            previous = next(doc for doc in render() if doc['kind'] == 'StatefulSet')['spec']['template']
            sources = list(yaml.safe_load_all(manifest.read_text()))
            source = next(doc for doc in sources if doc['kind'] == 'StatefulSet')
            annotations = source['spec']['template']['metadata']['annotations']
            new_revision = annotations[annotation] + '-changed'
            annotations[annotation] = new_revision
            manifest.write_text(yaml.safe_dump_all(sources))
            current = next(doc for doc in render() if doc['kind'] == 'StatefulSet')['spec']['template']
            self.assertNotEqual(previous, current)
            self.assertEqual(current['metadata']['annotations'][annotation], new_revision)
            self.assertEqual(previous['spec'], current['spec'])
            binding = next(doc for doc in docs if doc['kind'] == 'RoleBinding')
            self.assertEqual(binding['subjects'][0]['namespace'], 't3code-homelab')
            app = yaml.safe_load((ROOT.parent / '11.argocd/apps/app-t3code-homelab.yaml').read_text())
            self.assertTrue(app['spec']['syncPolicy']['automated']['prune'])
            self.assertFalse(app['spec']['syncPolicy']['automated'].get('selfHeal', False))
            self.assertEqual(app['spec']['source']['path'], '2-k3s/13.t3code-homelab')
            parent = yaml.safe_load((ROOT.parent / '11.argocd/apps/app-of-apps.yaml').read_text())
            self.assertTrue(parent['spec']['syncPolicy']['automated']['selfHeal'])
            service = next(doc for doc in docs if doc['kind'] == 'Service' and doc['metadata']['name'] == 't3code-ssh')
            self.assertEqual(service['spec']['type'], 'LoadBalancer')


def check_pinned_codex_project_requirements():
    reviewed = tomllib.loads((Path.home() / '.codex/config.toml').read_text())
    with tempfile.TemporaryDirectory(prefix='codex-project-fixture-') as temporary:
        root = Path(temporary)
        home, project, worktree = root / 'home', root / 'project', root / 'new-worktree'
        child = worktree / 'child'
        home.mkdir()
        subprocess.run(['git', 'init', '-q', '--initial-branch=main', str(project)], check=True)
        subprocess.run(['git', '-C', str(project), '-c', 'user.name=fixture', '-c',
                        'user.email=fixture@localhost', 'commit', '-q', '--allow-empty', '-m', 'fixture'], check=True)
        subprocess.run(['git', '-C', str(project), 'worktree', 'add', '-q', str(worktree)], check=True)
        child.mkdir()
        codex = copy.deepcopy(reviewed)
        codex['projects'] = {str(path): {'trust_level': 'trusted'} for path in (project, worktree, child)}
        codex['model_provider'] = 'work'
        codex['model_providers']['cliproxy']['base_url'] = 'https://models.work.example.test/v1'
        codex['model_providers']['work'] = {'name': 'work', 'base_url': 'https://models.work.example.test/v1',
                                          'wire_api': 'responses'}
        runtime.write_toml(home / '.codex/config.toml', codex)
        env = {**os.environ, 'HOME': str(home), 'CODEX_HOME': str(home / '.codex')}
        paths = (project, worktree, child)
        sources = {}
        for path in paths:
            source = path / '.codex/config.toml'
            source.parent.mkdir()
            source.write_text('model_reasoning_effort = "high"\nmodel_provider = "work"\n'
                '[model_providers.cliproxy]\nbase_url = "https://models.work.example.test/v1"\n'
                '[mcp_servers.unapproved_project_server]\nurl = "https://work.example.test/mcp"\n')
            sources[source] = source.read_bytes()
            result = subprocess.run(['codex', 'mcp', 'list', '--json'], cwd=path, env=env,
                                    capture_output=True, text=True, check=True, timeout=15)
            servers = {server['name']: server for server in json.loads(result.stdout)}
            assert servers['unapproved_project_server']['enabled'] is False, result.stdout
            assert 'requirements' in servers['unapproved_project_server']['disabled_reason'], result.stdout
            assert {name for name, server in servers.items() if server['enabled']} == set(reviewed['mcp_servers'])
            for name, server in reviewed['mcp_servers'].items():
                assert servers[name]['transport']['url'] == server['url']
                assert servers[name]['transport']['bearer_token_env_var'] == 'MCP_HUB_TOKEN'
            messages = [{'id': 1, 'method': 'initialize', 'params': {
                'clientInfo': {'name': 'offline-fixture', 'version': '0'},
                'capabilities': {'experimentalApi': True}}},
                {'id': 2, 'method': 'config/read', 'params': {'cwd': str(path), 'includeLayers': True}}]
            with tempfile.TemporaryFile() as errors, subprocess.Popen(['codex', 'app-server'], cwd=path,
                    env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, bufsize=0) as process:
                buffered = b''
                try:
                    for message in messages:
                        process.stdin.write((json.dumps(message) + '\n').encode())
                        deadline = time.monotonic() + 15
                        while True:
                            remaining = deadline - time.monotonic()
                            assert remaining > 0, 'Codex config API timed out'
                            if b'\n' not in buffered:
                                assert select.select([process.stdout], [], [], remaining)[0], 'Codex config API timed out'
                                chunk = os.read(process.stdout.fileno(), 65536)
                                assert chunk, 'Codex config API closed before responding'
                                buffered += chunk
                                continue
                            line, buffered = buffered.split(b'\n', 1)
                            response = json.loads(line)
                            if response.get('id') == message['id']:
                                assert 'error' not in response, response
                                break
                    actual = response['result']['config']
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            assert actual['model_provider'] == reviewed['model_provider'], actual['model_provider']
            for key, value in reviewed['model_providers']['cliproxy'].items():
                assert actual['model_providers']['cliproxy'][key] == value, key
            assert actual['model_reasoning_effort'] == 'high'
            print('isolated smoke: pinned Codex requirements enforce provider/MCP in ' + path.name + ' PASS')
        name = next(iter(reviewed['mcp_servers']))
        redirected = child / '.codex/config.toml'
        redirected.write_text(redirected.read_text() + '\n[mcp_servers.' + runtime.toml_value(name) + ']\n'
                              'url = "https://work.example.test/mcp"\n')
        sources[redirected] = redirected.read_bytes()
        result = subprocess.run(['codex', 'mcp', 'list', '--json'], cwd=child, env=env,
                                capture_output=True, text=True, check=True, timeout=15)
        servers = {server['name']: server for server in json.loads(result.stdout)}
        assert servers[name]['enabled'] is False, result.stdout
        assert 'requirements' in servers[name]['disabled_reason']
        assert all(not server['enabled'] for server in servers.values())
        assert all(path.read_bytes() == before for path, before in sources.items())
        print('isolated smoke: pinned Codex denies approved-name URL redirection without editing project files PASS')


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'fixture':
        fixture(Path(sys.argv[2]))
    elif sys.argv[1:] == ['codex-projects']:
        check_pinned_codex_project_requirements()
    else:
        unittest.main()
