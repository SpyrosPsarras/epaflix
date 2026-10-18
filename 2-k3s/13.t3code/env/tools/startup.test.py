#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

import yaml


class StartupTest(unittest.TestCase):
    def test_isolated_server_binds_loopback_and_probes_use_local_health(self):
        for variant in ('homelab', 'work'):
            with self.subTest(variant=variant):
                overlay = Path(__file__).resolve().parents[3] / f'13.t3code-{variant}'
                docs = list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(overlay)])))
                config = next(doc for doc in docs if doc and doc['kind'] == 'ConfigMap' and 'entrypoint.sh' in doc['data'])
                startup = config['data']['entrypoint.sh']
                args = shlex.split(startup.split('exec t3 ', 1)[1])
                self.assertEqual(args[args.index('--host') + 1], '127.0.0.1')
                self.assertIn('"host":"127.0.0.1","port":3773,"origin":"http://127.0.0.1:3773"', startup)
                sts = next(doc for doc in docs if doc and doc['kind'] == 'StatefulSet')
                server = next(container for container in sts['spec']['template']['spec']['containers'] if container['name'] == 't3')
                self.assertNotIn('ports', server)
                for probe, period, failures in (('startupProbe', 5, 1080), ('readinessProbe', 10, 3), ('livenessProbe', 30, 3)):
                    self.assertEqual(server[probe], {
                        'exec': {'command': ['curl', '-fsS', '-m', '1', 'http://127.0.0.1:3773/health']},
                        'periodSeconds': period,
                        **({'failureThreshold': failures} if probe != 'readinessProbe' else {})})
                service = next(doc for doc in docs if doc and doc['kind'] == 'Service'
                               and doc['metadata']['name'] == sts['spec']['serviceName'])
                self.assertEqual(service['spec']['ports'], [{'name': 'ssh', 'port': 2222, 'targetPort': 'ssh'}])
                self.assertFalse(any(port['port'] == 3773 for doc in docs if doc and doc['kind'] == 'Service'
                                     for port in doc['spec']['ports']))

    def test_isolated_server_and_ssh_shells_resolve_user_installed_tools(self):
        for variant in ('homelab', 'work'):
            overlay = Path(__file__).resolve().parents[3] / f'13.t3code-{variant}'
            docs = list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(overlay)])))
            config = next(doc for doc in docs if doc and doc['kind'] == 'ConfigMap' and 'entrypoint.sh' in doc['data'])
            sts = next(doc for doc in docs if doc and doc['kind'] == 'StatefulSet')
            server = next(container for container in sts['spec']['template']['spec']['containers'] if container['name'] == 't3')
            server_env = {entry['name']: entry.get('value') for entry in server['env']}
            self.assertTrue(server_env.get('PATH', '').endswith('/bin:/home/t3/.local/bin'))
            self.assertTrue(server_env['PATH'].startswith('/tools/node_modules/.bin:'))
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as home:
                binary = Path(home) / '.local/bin/isolated-path-probe'
                binary.parent.mkdir(parents=True)
                binary.write_text('#!/bin/sh\nprintf user-tool')
                binary.chmod(0o755)
                planted_python = binary.with_name('python3')
                planted_python.write_text('#!/bin/sh\nexit 97\n')
                planted_python.chmod(0o755)
                env = {'HOME': home, 'PATH': server_env['PATH'].replace('/home/t3', home)}
                path_exports = [line for line in config['data']['entrypoint.sh'].splitlines() if line.startswith('export PATH=')]
                startup = '\n'.join(path_exports) + '\n'
                pre_agent = path_exports[0] + '\n'
                safe_path = subprocess.check_output(['bash', '-c', pre_agent + 'printf %s "$PATH"'], env=env, text=True)
                self.assertNotIn(str(binary.parent), safe_path.split(':'))
                self.assertEqual(subprocess.check_output(['bash', '-c', pre_agent + 'python3 -c "import sys; print(sys.executable)"'],
                                                        env=env, text=True).strip(), '/usr/bin/python3')
                ssh_config = config['data']['sshd.sh'].split('<<EOF\n', 1)[1].split('\nEOF', 1)[0]
                rendered = subprocess.check_output(['bash', '-c', 'cat <<EOF\n' + ssh_config + '\nEOF'], env=env, text=True)
                ssh_path = next(line.split(' PATH=', 1)[1] for line in rendered.splitlines() if line.startswith('SetEnv '))
                for source, command, path in (('server', 'isolated-path-probe', env['PATH']),
                                               ('startup', startup + 'isolated-path-probe', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'),
                                               ('ssh', 'isolated-path-probe', ssh_path)):
                    with self.subTest(source=source):
                        result = subprocess.check_output(['bash', '-c', command], env={**env, 'PATH': path}, text=True)
                        self.assertEqual(result, 'user-tool')
                        resolved = subprocess.check_output(['bash', '-c', startup + 'printf %s "$PATH"'] if source == 'startup'
                                                           else ['bash', '-c', 'printf %s "$PATH"'],
                                                           env={**env, 'PATH': path}, text=True).split(':')
                        self.assertEqual(resolved[0], '/tools/node_modules/.bin')
                        self.assertEqual(resolved[-2:], ['/bin', str(binary.parent)])
                        self.assertEqual(subprocess.check_output(['bash', '-c', 'command -v python3'],
                                                                env={**env, 'PATH': ':'.join(resolved)}, text=True).strip(), '/usr/bin/python3')

    def test_rendered_pi_settings_enable_direct_tools_without_duplicate_restart_args(self):
        for variant in ('homelab', 'work'):
            overlay = Path(__file__).resolve().parents[3] / f'13.t3code-{variant}'
            docs = list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(overlay)])))
            config = next(doc for doc in docs if doc and doc['kind'] == 'ConfigMap' and 'entrypoint.sh' in doc['data'])
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                script = home / 't3-pi-settings.py'
                script.write_text(config['data']['t3-pi-settings.py'])
                path = home / 'userdata/settings.json'
                path.parent.mkdir()
                path.write_text(json.dumps({'providerInstances': {'pi': {'driver': 'pi', 'config': {
                    'launchArgs': '--thinking high'}}}}))
                for _ in range(2):
                    subprocess.run([sys.executable, str(script), str(home), 'http://proxy.test'], check=True)
                    settings = json.loads(path.read_text())
                    args = settings['providerInstances']['pi']['config']['launchArgs']
                    self.assertEqual(shlex.split(args), ['--thinking', 'high',
                        '--tools=+mcp__t3-code__orchestrator_capabilities,'
                        '+mcp__t3-code__delegate_task,+mcp__t3-code__task_status'])

    def test_isolated_startup_sets_git_identity_for_both_auth_modes(self):
        for variant in ('homelab', 'work'):
            overlay = Path(__file__).resolve().parents[3] / f'13.t3code-{variant}'
            docs = list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(overlay)])))
            config = next(doc for doc in docs if doc and doc['kind'] == 'ConfigMap' and 'entrypoint.sh' in doc['data'])
            startup = config['data']['entrypoint.sh']
            git_setup = startup.split('export GIT_TERMINAL_PROMPT=0\n', 1)[1].split('mkdir -p "$HOME/projects"', 1)[0]
            for auth in ('public', 'github-token'):
                with self.subTest(variant=variant, auth=auth), tempfile.TemporaryDirectory() as home:
                    env = {'HOME': home, 'PATH': os.environ['PATH'], 'GIT_CONFIG_NOSYSTEM': '1'}
                    command = f'set -euo pipefail\nread_config() {{ echo {auth}; }}\n' + git_setup
                    for _ in range(2):
                        subprocess.run(['bash', '-c', command], env=env, check=True)
                        for key, expected in (
                                ('user.name', 'Spyros Psarras'),
                                ('user.email', '13405649+SpyrosPsarras@users.noreply.github.com'),
                                ('credential.helper', '/scripts/git-credential-github.sh' if auth == 'github-token' else '')):
                            actual = subprocess.check_output(['git', 'config', '--global', '--get', key], env=env, text=True)
                            self.assertEqual(actual.rstrip('\n'), expected)

    def test_vault_credentials_and_storage_are_isolated_from_t3_and_sshd(self):
        for variant in ('homelab', 'work'):
            overlay = Path(__file__).resolve().parents[3] / f'13.t3code-{variant}'
            docs = list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(overlay)])))
            sts = next(doc for doc in docs if doc and doc['kind'] == 'StatefulSet')
            spec = sts['spec']['template']['spec']
            containers = {container['name']: container for container in spec['containers']}
            self.assertEqual(containers['t3']['command'], ['bash', '/scripts/entrypoint.sh'])
            for name, container in containers.items():
                env = {entry['name']: entry for entry in container.get('env', [])}
                bw = {key: entry for key, entry in env.items() if key.startswith(('BW_', 'BITWARDENCLI_'))}
                uid = container.get('securityContext', {}).get('runAsUser', spec['securityContext']['runAsUser'])
                if name == 'vault':
                    self.assertEqual(uid, 1001)
                    self.assertEqual(container['securityContext']['runAsGroup'], 1001)
                    self.assertTrue(container['securityContext']['runAsNonRoot'])
                    self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
                    self.assertFalse(container['securityContext']['allowPrivilegeEscalation'])
                    self.assertEqual(container['securityContext']['capabilities']['drop'], ['ALL'])
                    self.assertEqual(container['command'], ['vault', 'serve'])
                    self.assertEqual(container['workingDir'], '/vault')
                    self.assertEqual(set(bw), {'BW_EMAIL', 'BW_PASSWORD', 'BW_SERVER', 'BW_ORGANIZATION_ID', 'BW_COLLECTION_ID'})
                    for key, entry in bw.items():
                        self.assertEqual(entry['valueFrom']['secretKeyRef'], {'name': 'vaultwarden-agent', 'key': key})
                    mounts = {mount['name']: mount for mount in container['volumeMounts']}
                    self.assertEqual(set(mounts), {'vault-data', 'vault-scope', 'vault-tmp'} |
                                     ({'work-ssh'} if variant == 'work' else set()))
                    if variant == 'work':
                        self.assertTrue(mounts['work-ssh']['readOnly'])
                    self.assertEqual(mounts['vault-data']['mountPath'], '/vault')
                    self.assertNotIn('ports', container)
                else:
                    self.assertEqual(uid, 1000)
                    self.assertFalse(container['securityContext']['allowPrivilegeEscalation'])
                    self.assertEqual(container['securityContext']['capabilities']['drop'], ['ALL'])
                    self.assertNotIn('add', container['securityContext']['capabilities'])
                    self.assertFalse(bw)
                    self.assertNotIn('envFrom', container)
                    self.assertFalse(any(entry.get('valueFrom', {}).get('secretKeyRef', {}).get('name') == 'vaultwarden-agent'
                                         for entry in env.values()))
                    self.assertFalse(any(mount['name'] in ('vault-data', 'vault-tmp') for mount in container['volumeMounts']))
            for init in spec['initContainers']:
                self.assertNotIn('envFrom', init)
                for entry in init.get('env', []):
                    self.assertFalse(entry['name'].startswith(('BW_', 'BITWARDENCLI_')))
                    self.assertNotEqual(entry.get('valueFrom', {}).get('secretKeyRef', {}).get('name'), 'vaultwarden-agent')
            self.assertIn('vault', containers)
            self.assertIn({'name': 'T3_VAULT_CLIENT', 'value': '1'}, containers['t3']['env'])
            scope = next(mount for mount in containers['t3']['volumeMounts'] if mount['name'] == 'vault-scope')
            self.assertTrue(scope['readOnly'])
            self.assertTrue(next(mount for mount in containers['sshd']['volumeMounts']
                                 if mount['name'] == 'vault-scope')['readOnly'])
            vault_volume = next(volume for volume in spec['volumes'] if volume['name'] == 'vault-data')
            self.assertEqual(vault_volume['persistentVolumeClaim']['claimName'], 't3code-vault')
            pvc = next(doc for doc in docs if doc and doc['kind'] == 'PersistentVolumeClaim' and doc['metadata']['name'] == 't3code-vault')
            self.assertEqual(pvc['metadata']['namespace'], f't3code-{variant}')
            self.assertEqual(pvc['spec']['resources']['requests']['storage'], '1Gi')
            self.assertEqual(pvc['metadata']['annotations']['argocd.argoproj.io/sync-options'], 'Prune=false,Delete=false')
            init = next(container for container in spec['initContainers'] if container['name'] == 'vault-storage')
            self.assertEqual(init['securityContext']['runAsUser'], 0)
            self.assertEqual(set(init['securityContext']['capabilities']['add']), {'CHOWN'})
            self.assertEqual(init['image'], containers['vault']['image'])
            self.assertEqual(containers['vault']['image'], containers['t3']['image'])
            self.assertFalse(any(volume.get('secret', {}).get('secretName') == 'vaultwarden-agent' for volume in spec['volumes']))

    @unittest.skipUnless(os.geteuid() == 0, 'Two-UID check needs root; non-root runs still verify the rendered isolation')
    def test_uid_1000_cannot_read_uid_1001_environ_or_persistent_data(self):
        overlay = Path(__file__).resolve().parents[3] / '13.t3code-homelab'
        docs = list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(overlay)])))
        sts = next(doc for doc in docs if doc and doc['kind'] == 'StatefulSet')
        init = next(container for container in sts['spec']['template']['spec']['initContainers'] if container['name'] == 'vault-storage')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            data, scope = root / 'vault', root / 'scope'
            data.mkdir()
            scope.mkdir()
            for path in (data, scope):
                os.chown(path, 0, 1000)
                path.chmod(0o2770)
            command = [arg.replace('/vault', str(data)).replace('/run/t3-vault', str(scope)) for arg in init['command']]
            subprocess.run(command, check=True)
            client = data / 'client'
            for path in (data, client, scope):
                os.chown(path, 1001, 1000)
                path.chmod(0o2770)
            subprocess.run(command, check=True)
            for path, mode in ((data, 0o700), (client, 0o700), (scope, 0o755)):
                self.assertEqual(path.stat().st_uid, 1001)
                self.assertEqual(path.stat().st_gid, 1001)
                self.assertEqual(path.stat().st_mode & 0o7777, mode)
            code = 'import os,pathlib,sys,time; p=pathlib.Path(sys.argv[1]); (p/"cache").write_text("synthetic-data"); assert os.environ["BW_PASSWORD"]=="synthetic-password"; print("ready",flush=True); time.sleep(30)'
            with subprocess.Popen([sys.executable, '-c', code, str(client)], user=1001, group=1001, extra_groups=[],
                                  env={'BW_PASSWORD': 'synthetic-password'}, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as sidecar:
                try:
                    self.assertEqual(sidecar.stdout.readline(), b'ready\n')
                    probe = '''import pathlib,sys
for path in sys.argv[1:]:
    try:
        pathlib.Path(path).read_bytes()
    except PermissionError:
        continue
    raise SystemExit(1)
'''
                    result = subprocess.run([sys.executable, '-c', probe, f'/proc/{sidecar.pid}/environ', str(client / 'cache')],
                                            user=1000, group=1000, extra_groups=[], capture_output=True)
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(result.stdout + result.stderr, b'')
                finally:
                    sidecar.kill()
                    sidecar.wait()

    def test_init_creates_owned_userdata_before_kubelet_mounts_runtime_file(self):
        for variant in ('homelab', 'work'):
            with self.subTest(variant=variant):
                overlay = Path(__file__).resolve().parents[3] / f'13.t3code-{variant}'
                docs = list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(overlay)])))
                sts = next(doc for doc in docs if doc and doc['kind'] == 'StatefulSet')
                init = next(container for container in sts['spec']['template']['spec']['initContainers'] if container['name'] == 'runtime-state')
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    home, state = root / 'home', root / 'runtime-state'
                    home.mkdir()
                    state.mkdir()
                    command = [arg.replace('/home/t3', str(home)).replace('/runtime-state', str(state)) for arg in init['command']]
                    subprocess.run(command, check=True)
                    directory = home / '.t3/userdata'
                    self.assertTrue(directory.is_dir(), 'Init must create userdata before the main-container file mount')
                    self.assertEqual(directory.stat().st_uid, os.getuid())
                    self.assertEqual((state / 'server-runtime.json').read_bytes(), b'')
                    mounts = {mount['mountPath']: mount['name'] for mount in init['volumeMounts']}
                    self.assertEqual(mounts.get('/home/t3'), 'home')
                    self.assertEqual(mounts.get('/runtime-state'), 'runtime-state')


if __name__ == '__main__':
    unittest.main()
