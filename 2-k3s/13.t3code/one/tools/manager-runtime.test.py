#!/usr/bin/env python3
import base64
import importlib
import importlib.util
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

import yaml

ROOT = Path(__file__).resolve().parents[2]
FILES = ROOT / 'one/files'
sys.path.insert(0, str(FILES))
spec = importlib.util.spec_from_file_location('private_fixture', ROOT / 'env/tools/private-config.test.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.project = self.home / 'project'
        (self.project / '.git').mkdir(parents=True)
        self.bundle = self.root / 'bundle.json'
        self.bundle.write_text(json.dumps(fixture.fixture()))
        self.guidance = FILES / 'manager-guidance.md'
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        codex = self.bin / 'codex'
        codex.write_text('''#!/usr/bin/env python3
import os
from pathlib import Path
import re
import sys
if len(sys.argv) != 4 or sys.argv[1:3] != ['mcp', 'remove']:
    sys.exit(2)
path = Path(os.environ['CODEX_HOME']) / 'config.toml'
pattern = r'(?ms)^\\[mcp_servers\\.' + re.escape(sys.argv[3]) + r'\\]\\n.*?(?=^\\[|\\Z)'
text, count = re.subn(pattern, '', path.read_text())
if count != 1:
    sys.exit(3)
path.write_text(text)
''')
        codex.chmod(0o700)
        env = patch.dict(os.environ, {'PATH': str(self.bin) + os.pathsep + os.environ['PATH']})
        env.start()
        self.addCleanup(env.stop)

    def manager(self):
        self.assertTrue((FILES / 'manager-setup.py').is_file(), 'manager setup is required')
        return importlib.import_module('manager-setup')

    def configure(self):
        self.manager().configure(self.home, self.project, self.guidance, self.bundle)

    def put(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def render(self):
        overlay = self.root / 'overlay'
        shutil.copytree(ROOT / 'one', overlay)
        fixture.overlay(overlay)
        return list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(overlay)])))

    def test_rendered_manager_has_only_model_and_recovery_grants(self):
        docs = self.render()
        pod = next(d for d in docs if d['kind'] == 'StatefulSet')['spec']['template']['spec']
        self.assertEqual(pod['serviceAccountName'], 'manager-agent')
        self.assertFalse(pod['automountServiceAccountToken'])
        volumes = {v['name']: v for v in pod['volumes']}
        grants = {v['secret']['secretName'] for v in volumes.values() if 'secret' in v}
        self.assertEqual(grants, {'t3code-private-config', 'manager-kubeconfig'})
        self.assertEqual(volumes['manager-kubeconfig']['secret'], {
            'secretName': 'manager-kubeconfig', 'defaultMode': 0o440,
            'items': [{'key': 'kubeconfig', 'path': 'kubeconfig'}]})
        forbidden = {'MCP_HUB_URL', 'MCP_HUB_TOKEN', 'GITHUB_TOKEN', 'GH_TOKEN', 'AKS_TENANT_ID', 'SSH_AUTH_SOCK'}
        for container in pod['containers'] + pod.get('initContainers', []):
            self.assertFalse(container.get('envFrom'))
            self.assertFalse(forbidden.intersection(e['name'] for e in container.get('env', [])))
            for mount in container.get('volumeMounts', []):
                self.assertNotIn(mount['name'], {'github-ssh', 'vaultwarden', 'bw-scratch'})
            if container['name'] in {'t3', 'sshd'}:
                mount = next(m for m in container['volumeMounts'] if m['name'] == 'manager-kubeconfig')
                self.assertEqual(mount['mountPath'], '/run/t3-manager')
                self.assertTrue(mount['readOnly'])
        refs = {(ref['name'], ref['key'])
                for container in pod['containers'] + pod.get('initContainers', [])
                for env in container.get('env', [])
                if (ref := env.get('valueFrom', {}).get('secretKeyRef'))}
        self.assertEqual(refs, {('cliproxy-secrets', 'omp-api-key')})
        t3 = next(c for c in pod['containers'] if c['name'] == 't3')
        self.assertIn({'name': 'KUBECONFIG', 'value': '/run/t3-manager/kubeconfig'}, t3['env'])
        self.assertFalse(any(d['kind'] == 'ServiceAccount' and d['metadata']['name'] == 't3code' for d in docs))
        scripts = next(d for d in docs if d['kind'] == 'ConfigMap' and d['metadata']['name'].startswith('t3code-scripts-'))['data']
        self.assertTrue({'manager-setup.py', 'manager-guidance.md', 'ssh-policy.py'}.issubset(scripts))
        self.assertFalse({'git-credential-github.sh', 'vault-ssh-agent.py', 'vault-run.py', 'hub_clients.py',
                          'hub_services.py', 'homelab-ssh.conf', 'homelab-ssh.md'}.intersection(scripts))

    def test_model_key_inventory_and_manager_client_removal(self):
        generator = yaml.safe_load((ROOT / 'one/ksops-generator.yaml').read_text())
        self.assertEqual(set(generator['files']), {'private-agent-config.enc.yaml', 'cliproxy-secrets.enc.yaml'})
        for name in ('github-ssh', 'mcp-hub-client', 'vaultwarden'):
            self.assertFalse((ROOT / f'one/{name}.enc.yaml').exists())
        config = yaml.safe_load((ROOT / 'one/kustomization.yaml').read_text())
        sources = [r['source'] for r in config['replacements']]
        self.assertFalse(any(s.get('name') == 'mcp-hub-client' for s in sources))
        self.assertFalse(any(s.get('name') == 'cliproxy-secrets' for s in sources))
        clients = json.loads((ROOT.parent / '23.mcp-hub/files/clients.json').read_text())
        self.assertNotIn('t3code', clients)
        self.assertTrue(clients)
        pod = yaml.safe_load_all((ROOT / 'one/statefulset.yaml').read_text())
        t3 = next(pod)['spec']['template']['spec']['containers'][0]
        env = {e['name']: e for e in t3['env']}
        self.assertTrue(env['ANTHROPIC_BASE_URL']['value'])
        self.assertEqual(env['ANTHROPIC_AUTH_TOKEN']['valueFrom']['secretKeyRef'],
                         {'name': 'cliproxy-secrets', 'key': 'omp-api-key'})
        secret = yaml.safe_load((ROOT / 'one/cliproxy-secrets.enc.yaml').read_text())
        self.assertEqual(set(secret.get('stringData', secret.get('data', {}))), {'omp-api-key'})
        ciphertext = secret.get('stringData', secret.get('data', {}))['omp-api-key']
        self.assertTrue(ciphertext.startswith('ENC['))

    def test_stale_agent_and_project_mcp_maps_are_empty_after_two_boots(self):
        manager = self.manager()
        servers = {'legacy': {'command': 'old'}, 'custom': {'url': 'https://mcp.example.test'}}
        paths = [self.home / p for p in ('.pi/agent/mcp.json', '.claude.json', '.claude/.claude.json',
                 '.claude/settings.json', '.claude/settings.local.json')]
        for project in (self.project, self.home / '.t3/worktrees/fixture', self.project / 'nested'):
            paths += [project / '.mcp.json', project / '.pi/mcp.json',
                      project / '.claude/settings.json', project / '.claude/settings.local.json']
        for path in paths:
            self.put(path, {'mcpServers': servers, 'enableAllProjectMcpServers': True,
                           'enabledMcpjsonServers': ['legacy'], 'disabledMcpjsonServers': ['custom'],
                           'hooks': {'safe': 'unchanged'}, 'theme': 'dark',
                           'profiles': [{'mcpServers': servers}],
                           'projects': {'nested': {'mcpServers': servers, 'enableAllProjectMcpServers': True,
                                                   'enabledMcpjsonServers': ['custom'], 'trust': True}}})
        opencode = self.home / '.config/opencode/opencode.json'
        self.put(opencode, {'mcp': servers, 'theme': 'dark'})
        project_opencode = [project / name for project in (self.project, self.home / '.t3/worktrees/jbod-fixture/t3-x')
                            for name in ('opencode.json', '.opencode/opencode.json')]
        for path in project_opencode:
            self.put(path, {'$schema': 'x', 'mcp': servers, 'theme': 'dark'})
        codex = self.home / '.codex/config.toml'
        codex.parent.mkdir()
        codex.write_text('model = "fixture"\n[mcp_servers.legacy]\ncommand = "old"\n[mcp_servers.custom]\nurl = "https://mcp.example.test"\n')
        preserved = {self.project / '.git/config': b'checkout fixture',
                     self.home / '.t3/userdata/state.sqlite': b'state fixture',
                     self.home / '.pi/agent/pi-cliproxyapi-provider/config.json': b'{"aliases":"fixture"}'}
        for path, data in preserved.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for _ in range(2):
            self.configure()
            for path in paths:
                obj = json.loads(path.read_text())
                for state in (obj, obj['projects']['nested']):
                    self.assertEqual(state['mcpServers'], {})
                    self.assertFalse(state['enableAllProjectMcpServers'])
                    self.assertEqual(state['enabledMcpjsonServers'], [])
                self.assertEqual(obj['profiles'][0]['mcpServers'], {})
                self.assertEqual(obj['disabledMcpjsonServers'], [])
                self.assertEqual(obj['hooks'], {'safe': 'unchanged'})
                self.assertEqual(obj['theme'], 'dark')
                self.assertTrue(obj['projects']['nested']['trust'])
            self.assertEqual(json.loads(opencode.read_text()), {'mcp': {}, 'theme': 'dark'})
            for path in project_opencode:
                self.assertEqual(json.loads(path.read_text()), {'$schema': 'x', 'mcp': {}, 'theme': 'dark'})
            self.assertFalse(tomllib.loads(codex.read_text()).get('mcp_servers'))
            self.assertEqual(tomllib.loads(codex.read_text())['model'], 'fixture')
            for path, data in preserved.items():
                self.assertEqual(path.read_bytes(), data)
        with patch.object(manager.subprocess, 'run', side_effect=AssertionError('no CLI calls needed')):
            self.configure()

    def test_invalid_or_escaping_agent_config_stops_without_disclosure(self):
        self.manager()
        marker = 'private-fixture-not-for-output'
        cases = [('.pi/agent/mcp.json', '{' + marker),
                 ('.codex/config.toml', '[bad ' + marker),
                 ('.codex/config.toml', '[profiles.alt.mcp_servers.custom]\ncommand = "' + marker + '"\n'),
                 ('.config/opencode/opencode.json', '{"mcp": []}'),
                 ('project/.opencode/opencode.jsonc', '{}'),
                 ('project/opencode.json', '{"mcp": []}')]
        for name, data in cases:
            with self.subTest(name=name, data_type=data[:1]):
                path = self.home / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(data)
                result = self.run_setup()
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(marker, result.stdout + result.stderr)
                self.assertIn(name, result.stderr)
                self.assertNotIn(str(self.home), result.stderr)
                self.assertEqual(path.read_text(), data)
                path.unlink()
        outside = self.root / 'outside.json'
        outside.write_text('{"mcpServers":{"private-fixture-not-for-output":{}}}')
        path = self.home / '.claude.json'
        path.symlink_to(outside)
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertTrue(path.is_symlink())
        self.assertIn(marker, outside.read_text())

    def test_in_home_directory_link_is_allowed(self):
        target = self.home / 'shared'
        target.mkdir()
        (self.project / 'shared').symlink_to(target, target_is_directory=True)
        for _ in range(2):
            self.configure()
        self.assertTrue((self.project / 'shared').is_symlink())

    def test_config_named_and_escaping_directory_links_are_refused(self):
        inside = self.home / 'shared'
        outside = self.root / 'outside'
        inside.mkdir()
        outside.mkdir()
        for name, target in [('.claude', inside), ('.codex', inside), ('.pi', inside),
                             ('.mcp.json', inside), ('opencode.json', inside),
                             ('opencode.jsonc', inside), ('shared', outside)]:
            with self.subTest(name=name):
                link = self.project / name
                link.symlink_to(target, target_is_directory=True)
                result = self.run_setup()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f'project/{name}', result.stderr)
                self.assertNotIn(str(self.home), result.stderr)
                self.assertTrue(link.is_symlink())
                link.unlink()

    def run_setup(self):
        return subprocess.run([sys.executable, str(FILES / 'manager-setup.py'), str(self.home), str(self.project),
                               str(self.guidance), str(self.bundle)], capture_output=True, text=True)

    def test_manager_guidance_replaces_old_symlinks_and_is_required(self):
        self.manager()
        private = importlib.import_module('private-config')
        setup = importlib.import_module('pi-setup')
        retired = self.home / '.claude/CLAUDE.md'
        retired.parent.mkdir()
        (self.home / '.claude/AGENTS.md').write_text('Old permissive instructions.\n')
        retired.symlink_to(self.home / '.local/share/t3-private-config/old/instructions.md')
        for revision in range(3):
            payload = fixture.fixture()
            payload['files']['instructions.md']['data'] = base64.b64encode(f'Personal preferences {revision}.\n'.encode()).decode()
            self.bundle.write_text(json.dumps(payload))
            private.install(self.bundle.read_bytes(), self.home)
            self.configure()
            with patch.object(setup, 'fetch_models', return_value=[{'id': 'codex/fixture'}]):
                setup.write(self.home, '/fixture/safety', Path('/dev/null'))
            self.assertFalse(retired.is_symlink())
            for name in ('.claude/AGENTS.md', '.codex/AGENTS.md', '.codex/AGENTS.override.md', '.pi/agent/AGENTS.md'):
                text = (self.home / name).read_text()
                self.assertEqual(text.count('## Manager pod'), 1)
                self.assertNotIn('## Homelab SSH', text)
                self.assertIn(f'Personal preferences {revision}.', text)
                for rule in ('explicitly disabled, or unreadable', 'Recheck immediately before deletion',
                             'Never exec, read pod logs, port-forward or read Secrets',
                             'Never use, copy or export credentials', 'KUBECTL_REMOTE_COMMAND_WEBSOCKETS=false',
                             'automated: {}', 'enabled: false'):
                    self.assertIn(rule, text)
            self.assertTrue((self.home / '.claude/AGENTS.md').is_symlink())
            self.assertTrue((self.home / '.codex/AGENTS.md').is_symlink())
            self.assertEqual((self.home / '.claude/AGENTS.md.before-git').read_text(), 'Old permissive instructions.\n')
        with self.assertRaises(Exception):
            self.manager().configure(self.home, self.project, self.root / 'missing', self.bundle)
        blank = self.root / 'blank'
        blank.write_text(' \n')
        with self.assertRaises(Exception):
            self.manager().configure(self.home, self.project, blank, self.bundle)
        self.bundle.unlink()
        self.assertIn(self.bundle.name, self.run_setup().stderr)

    def test_startup_and_ssh_do_not_restore_credentials(self):
        self.manager()
        scripts = self.root / 'scripts'
        scripts.mkdir()
        for source in FILES.iterdir():
            if source.is_file():
                shutil.copy(source, scripts / source.name)
        startup = (FILES / 'entrypoint.sh').read_text().replace('/scripts/', str(scripts) + '/')
        startup = startup.replace("'/scripts'", repr(str(scripts)))
        startup = startup.replace('/private-agent-config/bundle.json', str(self.bundle))
        startup = startup.replace('$(getent passwd "$(id -u)" | cut -d: -f6)', str(self.root / 'ssh-home'))
        startup = startup[:startup.index(': >"$T3_HOME/userdata/server-runtime.json"')] + 'exit 0\n'
        script = self.root / 'entrypoint.sh'
        script.write_text(startup)
        (scripts / 'cc-safety-net-install.py').write_text('print("/fixture/safety")\n')
        (scripts / 'pi.sh').write_text('#!/bin/sh\nexit 0\n')
        (scripts / 'pi.sh').chmod(0o700)
        real_run = subprocess.run
        shim = self.bin
        log = self.root / 'calls'
        for name in ('git', 'claude', 'codex', 't3'):
            binary = shim / name
            binary.write_text('#!/bin/sh\nprintf "%s\\n" "' + name + ' $*" >>"$CALL_LOG"\n')
            binary.chmod(0o700)
        python = shim / 'python3'
        python.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >>"$CALL_LOG"\ncase "$*" in *check-packages*) exit 0;; *manager-setup.py*) [ "$5" = "$MANAGER_TEST_BUNDLE" ] || exit 99;; esac\nexec ' + sys.executable + ' "$@"\n')
        python.chmod(0o700)
        (scripts / 'ssh-config.sh').write_text('#!/bin/sh\nexit 0\n')
        env = {**os.environ, 'HOME': str(self.home), 'T3_PROJECT_DIR': str(self.project),
               'PATH': str(shim) + ':/usr/bin:/bin', 'CALL_LOG': str(log), 'ANTHROPIC_BASE_URL': '', 'MANAGER_TEST_BUNDLE': str(self.bundle)}
        startup = script.read_text().replace('export PATH=/tools/node_modules/.bin:$PATH', 'export PATH="$PATH"')
        script.write_text(startup)
        result = real_run(['bash', str(script)], env=env, capture_output=True, text=True)
        calls = log.read_text().splitlines()
        manager_calls = [call for call in calls if 'manager-setup.py' in call]
        self.assertEqual(manager_calls, [f'{scripts / "manager-setup.py"} {self.home} {self.project} {scripts / "manager-guidance.md"} {self.bundle}'])
        self.assertFalse(any('/private-agent-config' in call for call in manager_calls))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any('ssh-policy.py' in call for call in calls))
        for forbidden in ('hub_clients.py', 'git-credential-github', 'vault-ssh-agent', 'vault-run'):
            self.assertFalse(any(forbidden in call for call in calls))
        policy = json.loads((self.home / '.cc-safety-net/policy.json').read_text())
        for home in (self.home, self.root / 'ssh-home'):
            self.assertIn(str(home / '.ssh'), policy['secret_protection']['deny_paths'])
        log.unlink()
        (scripts / 'manager-guidance.md').unlink()
        failed = real_run(['bash', str(script)], env=env, capture_output=True, text=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(any('t3 ' in call for call in log.read_text().splitlines()))
        (scripts / 'ssh-config.sh').write_text((FILES / 'ssh-config.sh').read_text())
        getent = shim / 'getent'
        getent.write_text('#!/bin/sh\nprintf "fixture:x:1000:1000::%s:/bin/bash\\n" "$SSH_HOME"\n')
        getent.chmod(0o700)
        ssh_home = self.root / 'ssh-home'
        ssh_home.mkdir()
        env['SSH_HOME'] = str(ssh_home)
        (scripts / 'sshd-authorized-keys').write_text('synthetic-public-login-key\n')
        for name in ('sshd', 'ssh-keygen'):
            binary = shim / name
            binary.write_text('#!/bin/sh\nprintf "%s\\n" "' + name + ' $*" >>"$CALL_LOG"\n')
            binary.chmod(0o700)
        ssh = (FILES / 'sshd.sh').read_text().replace('/scripts/', str(scripts) + '/')
        ssh = ssh.replace('/usr/sbin/sshd', str(shim / 'sshd'))
        ssh = ssh[:ssh.index('while :; do')] + 'exit 0\n'
        ssh_script = self.root / 'sshd.sh'
        ssh_script.write_text(ssh)
        result = real_run(['bash', str(ssh_script)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = (self.home / '.ssh/sshd/sshd_config').read_text()
        self.assertIn('KUBECONFIG=/run/t3-manager/kubeconfig', config)
        self.assertNotIn('SSH_AUTH_SOCK=', config)
        self.assertIn('PermitOpen 127.0.0.1:3773 localhost:3773', config)
        self.assertEqual((ssh_home / '.ssh/authorized_keys').read_text(), 'synthetic-public-login-key\n')
        self.assertTrue(any('ssh-keygen -q -t ed25519' in call for call in log.read_text().splitlines()))
        self.assertFalse((ssh_home / '.ssh/config').exists())
        self.assertFalse((self.home / '.ssh/homelab-identity.conf').exists())
        outbound = (FILES / 'ssh-config.sh').read_text()
        for forbidden in ('homelab-ssh.conf', 'homelab-identity.conf', '/run/t3-github-ssh', 'IdentityFile'):
            self.assertNotIn(forbidden, outbound)
        self.assertIn('exec t3 start', (FILES / 'entrypoint.sh').read_text())
        self.assertIn('pi-setup', (FILES / 'entrypoint.sh').read_text())

    def test_manager_requests_and_self_recovery_identity_stay_separate(self):
        docs = self.render()
        sts = next(d for d in docs if d['kind'] == 'StatefulSet')
        self.assertEqual(sts['spec']['replicas'], 1)
        self.assertEqual(sts['spec']['updateStrategy'], {'type': 'OnDelete'})
        t3 = sts['spec']['template']['spec']['containers'][0]
        self.assertEqual(t3['resources']['requests'], {'cpu': '500m', 'memory': '1536Mi'})
        self.assertEqual(t3['resources']['limits'], {'memory': '12Gi'})
        pvc = next(d for d in docs if d['kind'] == 'PersistentVolumeClaim')
        self.assertEqual(pvc['spec']['resources']['requests']['storage'], '40Gi')
        binding = next(d for d in docs if d['kind'] == 'RoleBinding')
        self.assertEqual(binding['subjects'][0]['name'], 't3code-idle-rollout')
        self.assertNotEqual(binding['subjects'][0]['name'], 'manager-agent')
        app = yaml.safe_load((ROOT.parent / '11.argocd/apps/app-t3code.yaml').read_text())
        self.assertTrue(app['spec']['syncPolicy']['automated']['selfHeal'])


if __name__ == '__main__':
    unittest.main()
