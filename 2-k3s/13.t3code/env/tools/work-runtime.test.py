#!/usr/bin/env python3
"""Synthetic reviewed work inputs. No live accounts or endpoints are read."""
import base64
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
spec = importlib.util.spec_from_file_location('isolated_fixture', Path(__file__).with_name('isolated-runtime.test.py'))
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
runtime = common.runtime
ROOT = common.ROOT


def refresh(directory):
    path = directory / 'runtime.json'
    config = json.loads(path.read_text())
    config['sha256'] = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                        for name in runtime.WORK_INPUTS}
    path.write_text(json.dumps(config))


def fixture(directory):
    common.fixture(directory)
    for name in ('runtime.json', 'services.json', 'policies.json'):
        path = directory / name
        obj = json.loads(path.read_text())
        obj['environment'] = 'work'
        if name == 'runtime.json':
            obj['hubUrl'] = 'http://mcp-hub.mcp-hub-work.svc.cluster.local:8000'
            obj['projectUrl'] = 'https://github.com/work/fixture.git'
            del obj['approvals']['kubeconfig']
            obj['skillsUrl'] = 'https://github.com/work/skills-fixture.git'
            obj['grantSha256']['hub-token'] = hashlib.sha256(('work.' + 'h' * 43).encode()).hexdigest()
            obj['grantSha256']['model-token'] = hashlib.sha256(b'tok-fixture-model').hexdigest()
        path.write_text(json.dumps(obj))
    (directory / 'kubeconfig').unlink()
    refresh(directory)


class WorkRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.inputs, self.home = self.root / 'inputs', self.root / 'home'
        self.home.mkdir()
        fixture(self.inputs)
        self.env = patch.dict(os.environ, {'PATH': os.environ['PATH'], 'MCP_HUB_TOKEN': 'work.' + 'h' * 43,
            'ANTHROPIC_AUTH_TOKEN': 'tok-fixture-model'}, clear=True)
        self.env.start()
        self.requirements = self.root / 'etc/codex/requirements.toml'
        self.requirements_patch = patch.object(runtime, 'CODEX_REQUIREMENTS', self.requirements)
        self.requirements_patch.start()
        self.guidance_patch = patch.object(runtime, 'GUIDANCE', ROOT / 'env/files/isolated-guidance.md')
        self.guidance_patch.start()

    def tearDown(self):
        self.guidance_patch.stop()
        self.requirements_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def validate(self):
        return runtime.validate(self.inputs, self.home, 'work')

    def prepare(self):
        runtime.prepare(self.inputs, self.home, 'work')
        path = self.home / '.pi/agent/pi-cliproxyapi-provider/config.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'providerName': 'cliproxy', 'baseUrl': 'http://127.0.0.1:18317/v1',
                                    'modelAliases': {'claude-fixture': 'anthropic/claude-fixture'}}))

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
        ssh = ROOT / 'env/files/isolated-ssh.md'
        for _ in range(2):
            self.prepare()
            with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'http://127.0.0.1:18317'}), \
                    patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"data": []}')):
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

    skills_skipped = 'work skills: runtime.json has no skillsUrl; ai-skills clone and update skipped\n'

    def run_skills_startup(self):
        self.validate()
        lines = (ROOT / 'env/files/isolated-entrypoint.sh').read_text().splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith('ai_skills='))
        block = '\n'.join(lines[start:lines.index('fi', start) + 1])
        fake = self.root / 'bin'
        fake.mkdir(exist_ok=True)
        (fake / 'git').write_text('#!/bin/sh\n[ "$1" != -C ] || [ -d "$2/.git" ] || exit 1\necho "$@" >>"$GIT_LOG"\n')
        (fake / 'git').chmod(0o755)
        log = self.root / 'git.log'
        log.write_text('')
        result = subprocess.run(['bash', '-eu', '-c', block], capture_output=True, text=True, env={
            'PATH': f'{fake}:{os.environ["PATH"]}', 'HOME': str(self.home), 'GIT_LOG': str(log),
            'MCP_HUB_INSTANCE': 'work', 'inputs': str(self.inputs)})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stderr, log.read_text()

    def test_skills_clones_from_the_private_url(self):
        clone = self.home / 'work/ai-skills'
        self.assertEqual(self.run_skills_startup(), ('', f'clone -q https://github.com/work/skills-fixture.git {clone}\n'))

    def test_skills_skips_git_without_a_clone_or_url(self):
        for transform in (lambda obj: obj.pop('skillsUrl'), lambda obj: obj.update(skillsUrl='')):
            self.change('runtime.json', transform)
            self.assertEqual(self.run_skills_startup(), (self.skills_skipped, ''))

    def test_skills_pulls_an_existing_clone_without_a_url(self):
        clone = self.home / 'work/ai-skills'
        (clone / '.git').mkdir(parents=True)
        self.assertEqual(self.run_skills_startup(), ('', f'-C {clone} pull -q --ff-only\n'))
        self.change('runtime.json', lambda obj: obj.pop('skillsUrl'))
        self.assertEqual(self.run_skills_startup(), ('', f'-C {clone} pull -q --ff-only\n'))

    def test_skills_moves_one_candidate_before_pulling(self):
        self.prepare()
        old = self.home / 'old checkout/ai-skills'
        (old / '.git').mkdir(parents=True)
        (old / 'notes').write_text('local changes')
        clone = self.home / 'work/ai-skills'
        self.change('runtime.json', lambda obj: obj.pop('skillsUrl'))
        self.assertEqual(self.run_skills_startup(), (
            'work skills: moved ai-skills to ~/work/ai-skills\n', f'-C {clone} pull -q --ff-only\n'))
        self.assertEqual((clone / 'notes').read_text(), 'local changes')
        self.assertFalse(old.exists())

    def test_skills_leaves_two_candidates_in_place(self):
        candidates = [self.home / name / 'ai-skills/.git' for name in ('previous', 'other')]
        for candidate in candidates:
            candidate.mkdir(parents=True)
        self.change('runtime.json', lambda obj: obj.pop('skillsUrl'))
        self.assertEqual(self.run_skills_startup(), (self.skills_skipped, ''))
        self.assertTrue(all(candidate.is_dir() for candidate in candidates))
        self.assertFalse((self.home / 'work/ai-skills').exists())

    def assert_skills_skips_symlink(self, component):
        self.prepare()
        git_dir = self.home / 'previous/ai-skills/.git'
        link = self.home / component
        outside = self.root / 'outside'
        outside_git = outside / git_dir.relative_to(link)
        outside_git.mkdir(parents=True)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside, target_is_directory=True)
        self.change('runtime.json', lambda obj: obj.pop('skillsUrl'))
        self.assertEqual(self.run_skills_startup(), (self.skills_skipped, ''))
        self.assertEqual(link.readlink(), outside)
        self.assertTrue(outside_git.is_dir())
        self.assertFalse((self.home / 'work').exists())

    def test_skills_skips_symlinked_parent(self):
        self.assert_skills_skips_symlink('previous')

    def test_skills_skips_symlinked_clone(self):
        self.assert_skills_skips_symlink('previous/ai-skills')

    def test_skills_skips_symlinked_git_dir(self):
        self.assert_skills_skips_symlink('previous/ai-skills/.git')

    def test_skills_does_not_move_into_a_symlinked_work_dir(self):
        self.prepare()
        old = self.home / 'previous/ai-skills/.git'
        old.mkdir(parents=True)
        outside = self.root / 'outside'
        outside.mkdir()
        (self.home / 'work').symlink_to(outside, target_is_directory=True)
        self.change('runtime.json', lambda obj: obj.pop('skillsUrl'))
        self.assertEqual(self.run_skills_startup(), (
            'work skills: old ai-skills clone was not moved\n' + self.skills_skipped, ''))
        self.assertTrue(old.is_dir())
        self.assertEqual(list(outside.iterdir()), [])

    def test_skills_move_failure_does_not_block_startup(self):
        self.prepare()
        old = self.home / 'previous/ai-skills/.git'
        old.mkdir(parents=True)
        clone = self.home / 'work/ai-skills'
        clone.mkdir(parents=True)
        (clone / 'keep').touch()
        self.change('runtime.json', lambda obj: obj.pop('skillsUrl'))
        self.assertEqual(self.run_skills_startup(), (
            'work skills: old ai-skills clone was not moved\n' + self.skills_skipped, ''))
        self.assertTrue(old.is_dir())
        self.assertEqual(list(clone.iterdir()), [clone / 'keep'])

    def test_home_kubeconfig_persists_and_no_kubeconfig_input_is_accepted(self):
        self.prepare()
        config = self.home / '.kube/config'
        config.parent.mkdir()
        config.write_text('synthetic-any-cluster')
        for cache in ('.aws', '.config/gcloud'):
            (self.home / cache).mkdir()
        for _ in range(2):
            runtime.apply(self.inputs, self.home, 'work')
            runtime.prepare(self.inputs, self.home, 'work')
        self.assertEqual(config.read_text(), 'synthetic-any-cluster')
        self.assertEqual(os.listdir(config.parent), ['config'])
        entrypoint = (ROOT / 'env/files/isolated-entrypoint.sh').read_text()
        self.assertIn('if [[ $MCP_HUB_INSTANCE == homelab ]]; then\n  export KUBECONFIG=', entrypoint)
        self.assertEqual(entrypoint.count('KUBECONFIG='), 1)
        (self.inputs / 'kubeconfig').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'only the reviewed files'):
            self.validate()

    def test_work_only_hub_policies_settings_history_and_azure_persist(self):
        os.environ['T3_VAULT_CLIENT'] = '1'
        self.prepare()
        tool = self.home / '.local/bin/installed-tool'
        tool.parent.mkdir(parents=True)
        tool.write_text('#!/bin/sh\nexit 0\n')
        tool.chmod(0o755)
        planted_python = tool.with_name('python3')
        planted_python.write_text('#!/bin/sh\nexit 97\n')
        planted_python.chmod(0o755)
        os.environ['PATH'] = '/tools/node_modules/.bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:' + str(tool.parent)
        state = self.home / '.t3/userdata'
        state.mkdir(parents=True)
        (state / 'settings.json').write_text(json.dumps({'theme': 'fixture', 'providerInstances': {
            'pi': {'driver': 'pi', 'displayName': 'Work fixture', 'config': {'customModels': ['cliproxy/fixture']}}}}))
        (state / 'history-fixture').write_text('work-only')
        cache = self.home / '.azure'
        cache.mkdir()
        (cache / 'session-fixture').write_text('synthetic-work-session')
        for _ in range(2):
            runtime.apply(self.inputs, self.home, 'work')
            runtime.prepare(self.inputs, self.home, 'work')
        settings = json.loads((state / 'settings.json').read_text())
        self.assertEqual(settings['theme'], 'fixture')
        self.assertEqual(settings['providerInstances']['pi']['displayName'], 'Work fixture')
        self.assertFalse(settings['providerInstances']['codex']['enabled'])
        self.assertEqual(subprocess.check_output(['bash', '-c', 'command -v installed-tool'], text=True).strip(), str(tool))
        self.assertEqual(subprocess.run(['installed-tool']).returncode, 0)
        self.assertEqual(Path(subprocess.check_output(['bash', '-c', 'command -v python3'], text=True).strip()).resolve(), Path('/usr/bin/python3').resolve())
        self.assertEqual(Path(subprocess.check_output(['python3', '-c', 'import sys; print(sys.executable)'], text=True).strip()).resolve(), Path('/usr/bin/python3').resolve())
        self.assertEqual((state / 'history-fixture').read_text(), 'work-only')
        self.assertEqual((cache / 'session-fixture').read_text(), 'synthetic-work-session')
        for name in ('.pi/agent/mcp.json', '.claude.json', '.claude/.claude.json'):
            servers = json.loads((self.home / name).read_text())['mcpServers']
            self.assertEqual(set(servers), {'searxng'})
            self.assertEqual(servers['searxng']['url'], 'http://mcp-hub.mcp-hub-work.svc.cluster.local:8000/searxng')
            self.assertNotIn(os.environ['MCP_HUB_TOKEN'], (self.home / name).read_text())
        requirements = tomllib.loads(self.requirements.read_text())
        self.assertEqual(requirements['mcp_servers'], {'searxng': {'identity': {
            'url': 'http://mcp-hub.mcp-hub-work.svc.cluster.local:8000/searxng'}}})
        safety = json.loads((self.home / '.cc-safety-net/policy.json').read_text())
        self.assertIn(str(cache), safety['secret_protection']['deny_paths'])
        self.assertIn('/run/t3-work-ssh', safety['secret_protection']['deny_paths'])
        self.assertEqual(subprocess.run(['python3', str(self.home / '.config/t3-policy/guard.py'), 'git push']).returncode, 2)
        self.assertFalse((self.home / runtime.LEGACY_PERMISSION_PATH).exists())

    def test_work_notion_allowlist_rotation_preserves_boundaries(self):
        self.prepare()
        runtime.apply(self.inputs, self.home, 'work')
        retained = [self.inputs / name for name in ('policies.json', 'bundle.json')]
        policies = json.loads((self.inputs / 'policies.json').read_text())
        retained.extend(self.home / name for name in policies['files'] if name != runtime.LEGACY_PERMISSION_PATH)
        for name, value in (('.t3/userdata/history-fixture', 'synthetic-work-history'),
                            ('.azure/session-fixture', 'synthetic-enrolled-session')):
            path = self.home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
            retained.append(path)
        retained_state_before = {path: path.read_bytes() for path in retained}
        grant_digests = json.loads((self.inputs / 'runtime.json').read_text())['grantSha256']
        services = self.inputs / 'services.json'
        services.write_text(json.dumps({'environment': 'work', 'services': ['searxng', 'notion']}))
        with self.assertRaises(ValueError):
            self.validate()
        refresh(self.inputs)
        registrations = [self.home / name for name in ('.pi/agent/mcp.json', '.claude.json', '.claude/.claude.json')]
        for path in registrations:
            path.write_text(json.dumps({'mcpServers': {'drive': {'url': 'https://stale.example.test/mcp'}}}))
        codex_path = self.home / '.codex/config.toml'
        codex_path.write_text('[mcp_servers.drive]\nurl = "https://stale.example.test/mcp"\n')
        work_hub_url = 'http://mcp-hub.mcp-hub-work.svc.cluster.local:8000'
        first_apply_snapshot = None
        for _ in range(2):
            self.prepare()
            runtime.apply(self.inputs, self.home, 'work')
            self.assertFalse((self.home / '.config/t3-runtime/services.json').exists())
            for path in registrations:
                servers = json.loads(path.read_text())['mcpServers']
                self.assertEqual(set(servers), {'searxng', 'notion'})
                for name, server in servers.items():
                    self.assertEqual(server['url'], work_hub_url + '/' + name)
                    self.assertEqual(server['headers'], {'Authorization': 'Bearer ${MCP_HUB_TOKEN}'})
            codex = tomllib.loads(codex_path.read_text())['mcp_servers']
            requirements = tomllib.loads(self.requirements.read_text())['mcp_servers']
            self.assertEqual(set(codex), {'searxng', 'notion'})
            self.assertEqual(set(requirements), {'searxng', 'notion'})
            for name in ('searxng', 'notion'):
                self.assertEqual(codex[name], {'url': work_hub_url + '/' + name,
                                             'bearer_token_env_var': 'MCP_HUB_TOKEN'})
                self.assertEqual(requirements[name], {'identity': {'url': work_hub_url + '/' + name}})
            for path in [*registrations, codex_path, self.requirements]:
                for variable in ('MCP_HUB_TOKEN', 'ANTHROPIC_AUTH_TOKEN'):
                    self.assertNotIn(os.environ[variable], path.read_text())
            self.assertEqual({path: path.read_bytes() for path in retained}, retained_state_before)
            self.assertEqual(json.loads((self.inputs / 'runtime.json').read_text())['grantSha256'], grant_digests)
            snapshot = {path.relative_to(self.home): path.read_bytes()
                        for path in self.home.rglob('*') if path.is_file()}
            snapshot['requirements'] = self.requirements.read_bytes()
            if first_apply_snapshot is None:
                first_apply_snapshot = snapshot
            else:
                self.assertEqual(snapshot, first_apply_snapshot)

    def test_pi_text_generation_selections_and_options_survive_restart(self):
        self.prepare()
        path = self.home / '.t3/userdata/settings.json'
        path.parent.mkdir(parents=True)
        canonical = {'instanceId': 'pi', 'model': 'cliproxy-textgen/claude-haiku-5-5'}
        options = [{'id': 'thinking', 'value': 'high'}]
        cases = [
            ({'instanceId': 'pi', 'model': 'cliproxy/fixture', 'options': options},
             {**canonical, 'options': options}),
            ({'instanceId': 'pi', 'model': 'cliproxy-textgen/'}, canonical),
            ({'instanceId': 'pi', 'model': 'cliproxy-textgen/fixture', 'options': options}, None),
            ({'instanceId': 'codex', 'model': 'fixture', 'options': options}, canonical)]
        default = {'instanceId': 'codex', 'model': 'fixture', 'options': options}
        for selection, expected in cases:
            with self.subTest(selection=selection):
                path.write_text(json.dumps({'defaultModelSelection': default,
                                            'textGenerationModelSelection': selection}))
                for restart in range(2):
                    with self.subTest(restart=restart):
                        runtime.apply(self.inputs, self.home, 'work')
                        settings = json.loads(path.read_text())
                        self.assertEqual(settings['textGenerationModelSelection'],
                                         selection if expected is None else expected)
                        self.assertEqual(settings['defaultModelSelection'],
                                         {'instanceId': 'pi', 'model': 'cliproxy/claude-opus-5-5'})
                        if restart == 0:
                            first = path.read_bytes()
                        else:
                            self.assertEqual(path.read_bytes(), first)

    def test_legacy_permission_input_is_reviewed_but_not_installed(self):
        self.change('policies.json', lambda obj: obj['files'].update({
            runtime.LEGACY_PERMISSION_PATH: common.encoded({'permission': 'ask'})}))
        self.validate()
        self.prepare()
        runtime.apply(self.inputs, self.home, 'work')
        policy = self.home / runtime.LEGACY_PERMISSION_PATH
        self.assertFalse(policy.exists())
        policy.parent.mkdir(parents=True)
        policy.write_text('stale rollback policy')
        runtime.apply(self.inputs, self.home, 'work')
        self.assertEqual(policy.read_text(), 'stale rollback policy')
        path = self.inputs / 'policies.json'
        path.write_text(path.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            self.validate()

    def test_legacy_permission_acceptance_checks_are_not_silently_skipped(self):
        self.change('policies.json', lambda obj: obj['files'].update({
            runtime.LEGACY_PERMISSION_PATH: common.encoded({'permission': 'ask'})}))
        self.change('policies.json', lambda obj: obj['checks'].append({
            'argv': ['python3', '-c', 'from pathlib import Path; import sys; assert Path(sys.argv[1]).exists()',
                     '${HOME}/' + runtime.LEGACY_PERMISSION_PATH], 'exit': 0}))
        self.prepare()
        with self.assertRaisesRegex(ValueError, 'policy acceptance check failed'):
            runtime.apply(self.inputs, self.home, 'work')

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

    def test_required_safety_and_hooks_cannot_be_disabled(self):
        for path, replacement in [('.claude/settings.json', {'disableAllHooks': True}),
                                 ('.cc-safety-net/policy.json', {'version': 1, 'secret_protection': {'overrides': {'x': 'off'}}})]:
            fixture(self.inputs)
            self.change('policies.json', lambda obj: obj['files'].update({path: common.encoded(replacement)}))
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate()

    def test_work_project_trust_and_compliant_checkout_settings_survive(self):
        self.prepare()
        project = self.home / 'projects/work'
        config = project / '.claude/settings.json'
        config.parent.mkdir(parents=True)
        content = '{ "mcpServers": {}, "disableAllHooks": false, "theme": "fixture" }\n'
        config.write_text(content)
        before = config.read_bytes(), config.stat().st_mtime_ns, config.stat().st_ino
        codex = self.home / '.codex/config.toml'
        runtime.write_toml(codex, {'model_reasoning_effort': 'high',
            'projects': {str(project): {'trust_level': 'trusted'}, '/foreign/project': {'trust_level': 'trusted'}}})
        runtime.apply(self.inputs, self.home, 'work')
        self.assertEqual((config.read_bytes(), config.stat().st_mtime_ns, config.stat().st_ino), before)
        actual = tomllib.loads(codex.read_text())
        self.assertEqual(actual['projects'], {str(project): {'trust_level': 'trusted'}})
        self.assertEqual(actual['model_reasoning_effort'], 'high')
        self.assertEqual(actual['model_provider'], 'cliproxy')

    def test_inventory_grants_and_reviews_fail_closed(self):
        for name, transform in [('runtime.json', lambda obj: obj['approvals'].update(kubeconfig='stale-review')),
            ('runtime.json', lambda obj: obj['approvals'].pop('hub')),
            ('runtime.json', lambda obj: obj.update(skillsUrl='https://example.test/skills.git')),
            ('services.json', lambda obj: obj.update(environment='homelab')),
            ('services.json', lambda obj: obj.update(services=['vaultwarden'])),
            ('runtime.json', lambda obj: obj.update(hubUrl='http://mcp-hub.mcp-hub-homelab.svc.cluster.local:8000'))]:
            fixture(self.inputs)
            self.change(name, transform)
            with self.subTest(name=name), self.assertRaises((ValueError, KeyError)): self.validate()
        fixture(self.inputs)
        for name, value in [('MCP_HUB_TOKEN', 'homelab.' + 'h' * 43), ('GITHUB_TOKEN', 'unreviewed'),
                            ('SSH_AUTH_SOCK', '/foreign/agent'), ('AZURE_CONFIG_DIR', '/foreign/cache'), ('BW_SESSION', 'foreign')]:
            with patch.dict(os.environ, {name: value}), self.assertRaises(ValueError): self.validate()
        (self.inputs / 'work-auth.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'only the reviewed files'): self.validate()

    def test_work_startup_rejects_every_vault_client_variable_including_empty_values(self):
        with patch.dict(os.environ, {'T3_VAULT_CLIENT': '1'}):
            self.validate()
            for name in ('BW_EMAIL', 'BW_PASSWORD', 'BW_SERVER', 'BW_ORGANIZATION_ID', 'BW_COLLECTION_ID',
                         'BW_SESSION', 'BW_CLIENTID', 'BW_CLIENTSECRET', 'BW_UNKNOWN', 'BITWARDENCLI_APPDATA_DIR'):
                for value in ('synthetic-unapproved', ''):
                    with self.subTest(name=name, present=bool(value)), patch.dict(os.environ, {name: value}):
                        with self.assertRaises(ValueError):
                            self.validate()

    def test_imported_home_or_azure_cache_refused_and_homelab_gate_unchanged(self):
        cache = self.home / '.azure'
        cache.mkdir()
        (cache / 'cache-fixture').write_text('foreign')
        with self.assertRaises(ValueError): self.validate()
        shutil.rmtree(cache)
        (self.home / '.config/t3-runtime').mkdir(parents=True)
        (self.home / '.config/t3-runtime/environment').write_text('homelab\n')
        with self.assertRaises(ValueError): self.validate()
        kube = {'current-context': 'any', 'clusters': [{'name': 'any', 'cluster': {
                    'server': 'https://any.example.test', 'certificate-authority-data': 'Y2E='}}],
                'users': [{'name': 'any', 'user': {'exec': {'command': 'kubelogin'}}}],
                'contexts': [{'name': 'any', 'context': {'cluster': 'any', 'user': 'any'}}]}
        with self.assertRaises(ValueError): runtime.validate_homelab_kubeconfig(kube)

    def test_laptop_environments_have_distinct_keys_configs_and_ports(self):
        for environment, port in [('homelab', 2222), ('work', 2223)]:
            directory = self.root / environment
            common.laptop.prepare(directory, f'ssh.{environment}.example.test', 'node', port,
                                  self.inputs / 'authorized_keys', environment)
            settings = subprocess.check_output(['ssh', '-G', '-F', str(directory / 'ssh_config'),
                                                f't3-{environment}'], text=True)
            for expected in ('forwardagent no', 'identityagent none', 'identitiesonly yes', f'port {port}'):
                self.assertIn(expected, settings)
        self.assertNotEqual((self.root / 'homelab/id_ed25519.pub').read_bytes(), (self.root / 'work/id_ed25519.pub').read_bytes())
        broader = self.root / 'broader'
        broader.mkdir()
        (broader / 'config').write_text('Host personal\n')
        with self.assertRaises(ValueError):
            common.laptop.prepare(broader, 'ssh.work.example.test', 'node', 2223,
                                  self.inputs / 'authorized_keys', 'work')
        self.assertEqual((broader / 'config').read_text(), 'Host personal\n')
        self.assertFalse((broader / 'environment').exists())
        before = (self.root / 'homelab/ssh_config').read_bytes()
        with self.assertRaises(ValueError):
            common.laptop.prepare(self.root / 'homelab', 'ssh.work.example.test', 'node', 2223,
                                  self.inputs / 'authorized_keys', 'work')
        self.assertEqual((self.root / 'homelab/ssh_config').read_bytes(), before)
        ssh = self.root / 'ssh'
        ssh.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        ssh.chmod(0o700)
        with patch.dict(os.environ, {'PATH': str(self.root) + ':' + os.environ['PATH']}):
            args = subprocess.check_output(['bash', str(ROOT / 'env/tools/connect-laptop.sh'),
                str(self.root / 'work/ssh_config'), '23773', 'work'], text=True)
        self.assertIn('127.0.0.1:23773:127.0.0.1:3773', args)
        self.assertIn('t3-work', args)
        self.assertIn('IdentityAgent=none', args)
        with patch.dict(os.environ, {'PATH': str(self.root) + ':' + os.environ['PATH']}):
            for environment, port in [('homelab', 13773), ('work', 23773)]:
                args = subprocess.check_output(['bash', str(ROOT / 'env/tools/connect-laptop.sh'),
                    str(self.root / environment / 'ssh_config'), '', environment], text=True)
                self.assertIn(f'127.0.0.1:{port}:127.0.0.1:3773', args)
                self.assertIn(f't3-{environment}', args)

    def test_ci_covers_work_paths_variants_and_only_public_image_inputs(self):
        import yaml
        workflow = yaml.load((ROOT.parents[1] / '.github/workflows/build-t3-runtime.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertNotIn('pull_request', workflow['on'])
        paths = workflow['on']['push']['paths']
        for name in ('13.t3code', '13.t3code-homelab', '13.t3code-work'):
            self.assertIn(f'2-k3s/{name}/**', paths)
        build = workflow['jobs']['build']
        self.assertEqual(build['strategy']['matrix']['variant'], ['mixed', 'homelab', 'work'])
        steps = {step.get('name'): step for step in build['steps'] if 'name' in step}
        verification = steps['Image references and shared files match their inputs']['run']
        self.assertEqual(steps['Build and save the layer cache PR builds read']['with']['build-args'],
                         'T3_RUNTIME_VARIANT=${{ matrix.variant }}')
        self.assertIn('smoke.sh', steps['Boot without production credentials']['run'])
        for name in ('13.t3code/one', '13.t3code-homelab', '13.t3code-work'):
            self.assertIn(f'2-k3s/{name}/statefulset.yaml', verification)
        publish = steps['Publish the tested image']['run']
        self.assertIn('ghcr.io/spyrospsarras/t3-runtime', publish)
        self.assertIn('source=$LOCAL_IMAGE', publish)
        self.assertEqual(build['env']['LOCAL_IMAGE'], "${{ matrix.variant == 'mixed' && 't3-runtime:dev' || format('t3-runtime:{0}-dev', matrix.variant) }}")
        ci = yaml.load((ROOT.parents[1] / '.github/workflows/ci.yml').read_text(), Loader=yaml.BaseLoader)
        ci_steps = {step.get('name'): step for job in ci['jobs'].values()
                    for step in job['steps'] if 'name' in step}
        self.assertIn('2-k3s/13.t3code-work', ci_steps['Kustomize build (sops-free dirs)']['run'])
        self.assertIn('work-runtime.test.py', ci_steps['T3 config and placement tests']['run'])
        smoke = ci_steps['T3 install and runtime smoke']
        self.assertEqual(ci['jobs']['smoke']['strategy']['matrix']['variant'], ['mixed', 'homelab', 'work'])
        self.assertEqual(smoke['env']['T3_RUNTIME_VARIANT'], '${{ matrix.variant }}')
        self.assertIn('smoke.sh', smoke['run'])
        self.assertIn('2-k3s/13.t3code 2-k3s/13.t3code-homelab 2-k3s/13.t3code-work', ci_steps['Pick the runtime image']['run'])
        pins = ci_steps['Runtime image pins match their inputs']['run']
        for name in ('13.t3code/one', '13.t3code-homelab', '13.t3code-work'):
            self.assertIn(f'2-k3s/{name}/statefulset.yaml', pins)
        allowed = {line[1:] for line in (ROOT / 'env/Dockerfile.dockerignore').read_text().splitlines() if line.startswith('!')}
        self.assertEqual(allowed, {'versions.env', 'env/', 'env/Dockerfile', 'env/tools/',
            'env/tools/os-packages.txt', 'env/tools/install-cluster-tools.sh',
            'env/tools/package.json', 'env/tools/package-lock.json',
            'env/files/', 'env/files/vault-client.py', 'env/files/keeper-client.py', 'env/files/isolated-guidance.md',
            'one/', 'one/files/', 'one/files/cc-safety-net-install.py',
            'one/files/cc-safety-net.patch', 'one/files/cc-safety-net-hashes.json'})

    def test_gitops_work_overlay_and_jobs_are_independent_and_revision_tracked(self):
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / '2-k3s'
            tree.mkdir()
            for name in ('13.t3code', '13.t3code-homelab', '13.t3code-work'):
                shutil.copytree(ROOT.parent / name, tree / name, ignore=shutil.ignore_patterns('__pycache__'))
            overlay = tree / '13.t3code-work'
            def render():
                return list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(overlay)])))
            docs = render()
            self.assertTrue(all(doc['metadata'].get('namespace', 't3code-work') == 't3code-work' for doc in docs))
            self.assertFalse(any(doc['kind'] == 'Secret' for doc in docs))
            sts = next(doc for doc in docs if doc['kind'] == 'StatefulSet')
            pod = sts['spec']['template']['spec']
            self.assertEqual(sts['metadata']['name'], 'work')
            rollout = next(doc for doc in docs if doc['kind'] == 'CronJob')
            self.assertEqual(rollout['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['env'],
                             [{'name': 'STATEFULSET_NAME', 'value': 'work'}])
            self.assertEqual(sts['spec']['updateStrategy']['type'], 'OnDelete')
            self.assertEqual(sts['spec']['persistentVolumeClaimRetentionPolicy'], {'whenDeleted': 'Retain', 'whenScaled': 'Retain'})
            self.assertFalse(pod['automountServiceAccountToken'])
            self.assertNotIn('hostPath', json.dumps(pod))
            self.assertEqual({v['persistentVolumeClaim']['claimName'] for v in pod['volumes'] if 'persistentVolumeClaim' in v}, {'t3code-home', 't3code-vault', 't3code-keeper'})
            secret_names = {v['secret']['secretName'] for v in pod['volumes'] if 'secret' in v}
            secret_names.update(source['secret']['name'] for v in pod['volumes']
                                for source in v.get('projected', {}).get('sources', []) if 'secret' in source)
            self.assertEqual(secret_names, {'t3-runtime-inputs', 't3-work-ssh'})
            self.assertNotIn('/run/t3-build', json.dumps(pod))
            self.assertFalse({'KUBECONFIG', 'T3_BUILD_KUBECONFIG'} & {e['name'] for e in pod['containers'][0]['env']})
            inputs = next(v for v in pod['volumes'] if v['name'] == 'inputs')
            self.assertEqual({item['key'] for item in inputs['secret']['items']}, runtime.WORK_INPUTS | {'runtime.json'})
            image = pod['containers'][0]['image']
            self.assertIn(':work-inputs-', image)
            self.assertTrue(all(c['image'] == image for c in pod['containers'] + pod['initContainers']))
            self.assertEqual(next(e['value'] for e in pod['containers'][0]['env'] if e['name'] == 'MCP_HUB_INSTANCE'), 'work')
            for doc in docs:
                if doc['kind'] in {'Job', 'CronJob'}:
                    job = doc['spec']['jobTemplate']['spec'] if doc['kind'] == 'CronJob' else doc['spec']
                    self.assertEqual(job['template']['spec']['containers'][0]['image'], image)
                    self.assertEqual(job['template']['spec']['affinity'], pod['affinity'])
            role = next(doc for doc in docs if doc['kind'] == 'Role')
            self.assertEqual([rule['resourceNames'] for rule in role['rules']], [['work'], ['work-0'], ['work-0']])
            binding = next(doc for doc in docs if doc['kind'] == 'RoleBinding')
            self.assertEqual(binding['subjects'][0]['namespace'], 't3code-work')
            scripts = next(doc for doc in docs if doc['kind'] == 'ConfigMap' and 'entrypoint.sh' in doc['data'])
            self.assertNotIn('work-aks-auth.py', scripts['data'])
            self.assertNotIn('vault-run.py', scripts['data'])
            self.assertNotIn('aks-auth-central.sh', scripts['data'])
            manifest = overlay / 'statefulset.yaml'
            annotation = 't3code.epaflix.com/inputs-grants-revision'
            before = sts['spec']['template']
            source = next(doc for doc in yaml.safe_load_all(manifest.read_text()) if doc['kind'] == 'StatefulSet')
            revision = source['spec']['template']['metadata']['annotations'][annotation]
            self.assertEqual(before['metadata']['annotations'][annotation], revision)
            next_revision = revision + '-next'
            manifest.write_text(manifest.read_text().replace('"' + revision + '"', '"' + next_revision + '"'))
            after = next(doc for doc in render() if doc['kind'] == 'StatefulSet')['spec']['template']
            self.assertNotEqual(before, after)
            self.assertEqual(after['metadata']['annotations'][annotation], next_revision)
            self.assertEqual(before['spec'], after['spec'])
            app = yaml.safe_load((ROOT.parent / '11.argocd/apps/app-t3code-work.yaml').read_text())
            self.assertTrue(app['spec']['syncPolicy']['automated']['prune'])
            self.assertFalse(app['spec']['syncPolicy']['automated'].get('selfHeal', False))
            self.assertEqual(app['spec']['source']['path'], '2-k3s/13.t3code-work')
            parent = yaml.safe_load((ROOT.parent / '11.argocd/apps/app-of-apps.yaml').read_text())
            self.assertTrue(parent['spec']['syncPolicy']['automated']['selfHeal'])


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'fixture':
        fixture(Path(sys.argv[2]))
    else:
        unittest.main()
