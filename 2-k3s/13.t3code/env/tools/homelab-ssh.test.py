#!/usr/bin/env python3
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import pwd
import socket
import struct
import subprocess
import tempfile
import time
import unittest
import contextlib
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'files/vault-client.py'
spec = importlib.util.spec_from_file_location('vault', SOURCE)
vault = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vault)
SCOPE = {'environment': 'homelab', 'organizationId': '11111111-1111-4111-8111-111111111111',
         'collectionId': '22222222-2222-4222-8222-222222222222'}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.scope = SCOPE.copy()
        self.tmp = tempfile.TemporaryDirectory(prefix='vault-ssh-test-', dir='/dev/shm')
        self.root = Path(self.tmp.name)
        self.key = self.root / 'key'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'synthetic', '-f', str(self.key)], check=True)
        self.item = {'id': '33333333-3333-4333-8333-333333333333', 'type': 5,
                     'organizationId': SCOPE['organizationId'], 'collectionIds': [SCOPE['collectionId']],
                     'sshKey': {'privateKey': self.key.read_text()}}
        self.socket = self.root / 'ssh-agent.sock'
        self.env = {**os.environ, 'SSH_AUTH_SOCK': str(self.socket), 'SSH_ASKPASS_REQUIRE': 'never'}
        self.server = object()
        owner_patch = patch.object(vault, 'owns_listener', return_value=True)
        owner_patch.start()
        self.addCleanup(owner_patch.stop)
        self.uid_patch = patch.object(vault, 'SSH_CLIENT_UID', os.getuid())
        self.uid_patch.start()
        self.addCleanup(self.uid_patch.stop)
        self.scope_patch = patch.object(vault, 'SCOPE_FILE', self.root / 'scope.json')
        self.scope_patch.start()
        self.addCleanup(self.scope_patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def add(self, *args):
        return subprocess.run(['ssh-add', *args], env=self.env, capture_output=True, timeout=5)

    def test_scoped_keys_sign_but_clients_cannot_mutate_the_agent(self):
        foreign = copy.deepcopy(self.item)
        foreign['organizationId'] = '44444444-4444-4444-8444-444444444444'
        wrong_collection = copy.deepcopy(self.item)
        wrong_collection['collectionIds'] = []
        with patch.object(vault, 'fetch', return_value={'data': [self.item, foreign, wrong_collection, {'type': 1}]}), \
                vault.ssh_agent(self.scope, self.server):
            listed = self.add('-L')
            self.assertEqual(listed.returncode, 0)
            self.assertEqual(len(listed.stdout.splitlines()), 1)
            self.assertEqual(listed.stdout.split()[:2], self.key.with_suffix('.pub').read_bytes().split()[:2])
            self.assertEqual(self.add('-T', str(self.key.with_suffix('.pub'))).returncode, 0)
            self.assertNotEqual(self.add('-D').returncode, 0)
            self.assertEqual(self.add('-T', str(self.key.with_suffix('.pub'))).returncode, 0)
            self.assertEqual(self.socket.stat().st_mode & 0o777, 0o666)
        self.assertFalse(self.socket.exists())
        self.assertNotEqual(self.add('-L').returncode, 0)

    def test_empty_scope_runs_an_empty_agent_without_signing_or_fallback(self):
        output = io.StringIO()
        with patch.object(vault, 'fetch', return_value={'data': []}), redirect_stdout(output), \
                vault.ssh_agent(self.scope, self.server) as agent:
            self.assertIsNone(agent.poll())
            self.assertTrue(self.socket.is_socket())
            self.assertEqual(self.add('-L').returncode, 1)
            self.assertNotEqual(self.add('-T', str(self.key.with_suffix('.pub'))).returncode, 0)
        self.assertEqual(output.getvalue(), 'vault: no scoped SSH keys; agent is empty\n')
        self.assertFalse(self.socket.exists())

    def test_readiness_accepts_an_empty_agent_but_requires_scope_and_socket(self):
        source = SOURCE.parent / 'ssh-agent-ready.sh'
        helper = self.root / 'ssh-agent-ready.sh'
        helper.write_text(source.read_text().replace('/run/t3-vault', str(self.root)))
        command = ['bash', '-c', 'sleep() { return 1; }; source "$1"', 'fixture', str(helper)]
        environment = {name: value for name, value in self.env.items() if name != 'SSH_AUTH_SOCK'}
        (self.root / 'scope.json').touch()
        with patch.object(vault, 'fetch', return_value={'data': []}), redirect_stdout(io.StringIO()), \
                vault.ssh_agent(self.scope, self.server):
            result = subprocess.run(command, env=environment, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            (self.root / 'scope.json').unlink()
            result = subprocess.run(command, env=environment, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            (self.root / 'scope.json').touch()
        result = subprocess.run(command, env=environment, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)

    def test_foreign_variant_is_rejected_before_listener_or_item_access(self):
        selected = {**self.scope, 'environment': 'mixed'}
        self.socket.write_text('existing socket marker')
        with patch.object(vault, 'owns_listener', side_effect=AssertionError('Listener accessed')), \
                patch.object(vault, 'fetch', side_effect=AssertionError('Items fetched')):
            with self.assertRaises(vault.Refused), vault.ssh_agent(selected, self.server):
                self.fail('Foreign variant was accepted')
        self.assertEqual(self.socket.read_text(), 'existing socket marker')

    def test_proxy_denies_mutation_and_extension_types_with_one_fixed_log_each(self):
        denied = (17, 25, 18, 19, 22, 23, 27, 255)
        output = io.StringIO()
        with patch.object(vault, 'fetch', return_value={'data': [self.item]}), redirect_stderr(output), \
                vault.ssh_agent(self.scope, self.server):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(self.socket))
                with client.makefile('rb') as stream:
                    for number, message_type in enumerate(denied, 1):
                        with self.subTest(message_type=message_type):
                            payload = bytes([message_type]) + b'synthetic request payload must not be logged'
                            client.sendall(struct.pack('!I', len(payload)) + payload)
                            self.assertEqual(stream.read(5), b'\x00\x00\x00\x01\x05')
                            deadline = time.monotonic() + 1
                            while len(output.getvalue().splitlines()) < number and time.monotonic() < deadline:
                                time.sleep(0.001)
                            self.assertEqual(output.getvalue().splitlines(),
                                ['vault: SSH agent proxy request refused'] * number)
            self.assertEqual(self.add('-T', str(self.key.with_suffix('.pub'))).returncode, 0)
        self.assertEqual(output.getvalue().splitlines(),
                         ['vault: SSH agent proxy request refused'] * len(denied))

    def test_key_fetch_refuses_an_unowned_listener(self):
        for result in (False, vault.Refused):
            with self.subTest(result=result), patch.object(vault, 'owns_listener',
                    **({'return_value': False} if result is False else {'side_effect': result})), \
                    patch.object(vault, 'fetch', side_effect=AssertionError('Untrusted listener received a request')):
                with self.assertRaises(vault.Refused), vault.ssh_agent(self.scope, self.server):
                    self.fail('Untrusted listener was accepted')
                self.assertFalse(self.socket.exists())

    def test_missing_invalid_or_unreachable_keys_leave_no_socket(self):
        invalid = copy.deepcopy(self.item)
        invalid['sshKey']['privateKey'] = 'invalid synthetic key'
        missing = copy.deepcopy(self.item)
        missing['sshKey'] = {}
        for items in ([missing], [self.item, invalid]):
            with self.subTest(count=len(items)), patch.object(vault, 'fetch', return_value={'data': items}):
                with self.assertRaises(vault.Refused), vault.ssh_agent(self.scope, self.server):
                    self.fail('Invalid key set was accepted')
                self.assertFalse(self.socket.exists())
        with patch.object(vault, 'fetch', side_effect=OSError):
            with self.assertRaises(OSError), vault.ssh_agent(self.scope, self.server):
                self.fail('Unavailable vault was accepted')
        self.assertFalse(self.socket.exists())

    def test_real_ssh_authenticates_with_proxy_and_fails_without_agent(self):
        host = self.root / 'host'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(host)], check=True)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        user = pwd.getpwuid(os.getuid()).pw_name
        config = self.root / 'sshd_config'
        config.write_text(f'ListenAddress 127.0.0.1\nPort {port}\nHostKey {host}\n'
                          f'AuthorizedKeysFile {self.key}.pub\nAllowUsers {user}\n'
                          'PidFile none\nUsePAM no\nStrictModes no\nPasswordAuthentication no\n'
                          'KbdInteractiveAuthentication no\nPermitRootLogin yes\n')
        known = self.root / 'known_hosts'
        known.write_text(f'[127.0.0.1]:{port} ' + host.with_suffix('.pub').read_text())
        command = ['ssh', '-F', '/dev/null', '-o', 'BatchMode=yes', '-o', 'IdentityFile=none',
                   '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(known),
                   '-o', 'PreferredAuthentications=publickey', '-o', 'ConnectTimeout=2',
                   '-o', 'IdentityAgent=' + str(self.socket), '-p', str(port), user + '@127.0.0.1', 'true']
        environment = {name: value for name, value in self.env.items() if name != 'SSH_AUTH_SOCK'}
        with subprocess.Popen(['/usr/sbin/sshd', '-D', '-e', '-f', str(config)], stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE) as server:
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and server.poll() is None:
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=0.1):
                            break
                    except OSError:
                        time.sleep(0.02)
                self.assertIsNone(server.poll(), 'Synthetic SSH server failed to start')
                with patch.object(vault, 'fetch', return_value={'data': [self.item]}), vault.ssh_agent(self.scope, self.server):
                    result = subprocess.run(command, env=environment, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                result = subprocess.run(command, env=environment, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 255)
            finally:
                server.terminate()
                server.wait(timeout=5)

    def test_restart_replaces_keys_instead_of_keeping_old_agent(self):
        with patch.object(vault, 'fetch', return_value={'data': [self.item]}), vault.ssh_agent(self.scope, self.server):
            self.assertEqual(self.add('-T', str(self.key.with_suffix('.pub'))).returncode, 0)
        other = self.root / 'other'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(other)], check=True)
        self.item['sshKey']['privateKey'] = other.read_text()
        with patch.object(vault, 'fetch', return_value={'data': [self.item]}), vault.ssh_agent(self.scope, self.server):
            self.assertNotEqual(self.add('-T', str(self.key.with_suffix('.pub'))).returncode, 0)
            self.assertEqual(self.add('-T', str(other.with_suffix('.pub'))).returncode, 0)

    def test_homelab_config_resolves_aliases_without_identity_files_or_password_fallback(self):
        overlay = SOURCE.parents[3] / '13.t3code-homelab'
        scripts = self.root / 'scripts'
        scripts.mkdir()
        config = (overlay / 'files/homelab-ssh.conf').read_text().replace('~/.ssh/', str(self.root / '.ssh') + '/')
        (scripts / 'homelab-ssh.conf').write_text(config)
        login = self.root / 'login'
        binaries = tempfile.TemporaryDirectory(prefix='ssh-config-test-')
        self.addCleanup(binaries.cleanup)
        executable = Path(binaries.name) / 'getent'
        executable.write_text('#!/bin/sh\nprintf "fixture:x:1000:1000::%s:/bin/bash\\n" "$LOGIN_HOME"\n')
        executable.chmod(0o700)
        script = (overlay / 'files/ssh-config.sh').read_text().replace('/scripts/', str(scripts) + '/')
        env = {**os.environ, 'HOME': str(self.root), 'LOGIN_HOME': str(login),
               'PATH': binaries.name + ':' + os.environ['PATH']}
        subprocess.run(['bash', '-c', script], env=env, check=True, capture_output=True)
        env['SSH_AUTH_SOCK'] = '/run/t3-vault/ssh-agent.sock'
        inventory = ('homepc', 'laptop', 'laptop-ts', 'k3s-master-51')
        inventory_ip = next(line.split()[1] for line in config.splitlines() if line.strip().startswith('HostName '))
        cases = [(host, [], True) for host in inventory] + [
            (inventory_ip, [], True), ('work-host.example.com', [], False), ('203.0.113.9', [], False),
            ('homepc', ['-o', 'HostName=203.0.113.9'], False)]
        for directory in (login, self.root):
            for host, options, uses_agent in cases:
                with self.subTest(host=host, options=options, directory=directory):
                    result = subprocess.run(['ssh', '-G', '-F', str(directory / '.ssh/config'), *options, host],
                                            env=env, capture_output=True, text=True, check=True)
                    settings = dict(line.split(' ', 1) for line in result.stdout.splitlines())
                    if host == 'laptop-ts':
                        direct = subprocess.check_output(
                            ['ssh', '-G', '-F', str(directory / '.ssh/config'), 'laptop'], env=env, text=True)
                        direct_settings = dict(line.split(' ', 1) for line in direct.splitlines())
                        self.assertEqual(settings['hostname'], direct_settings['hostname'])
                        self.assertEqual(settings['user'], direct_settings['user'])
                        self.assertEqual(settings['proxycommand'],
                                         'kubectl -n tailscale exec -i deploy/subnet-router -- tailscale nc spylinux 22')
                    else:
                        self.assertNotIn('proxycommand', settings)
                    if options:
                        self.assertEqual(settings['hostname'], '203.0.113.9')
                    elif host not in inventory:
                        self.assertEqual(settings['hostname'], host)
                    if uses_agent:
                        if host in inventory:
                            self.assertNotEqual(settings['hostname'], host)
                        self.assertEqual(settings['identityagent'], '/run/t3-vault/ssh-agent.sock')
                        self.assertEqual(settings['stricthostkeychecking'], 'accept-new')
                        self.assertEqual(settings['userknownhostsfile'], str(self.root / '.ssh/known_hosts'))
                    else:
                        self.assertEqual(settings['identityagent'], 'none')
                        self.assertEqual(settings['stricthostkeychecking'], 'true')
                    for name, value in {'identityfile': 'none',
                                        'preferredauthentications': 'publickey', 'passwordauthentication': 'no',
                                        'kbdinteractiveauthentication': 'no', 'forwardagent': 'no', 'batchmode': 'yes'}.items():
                        self.assertEqual(settings[name], value)

    def test_rendered_homelab_only_exposes_an_agent_socket(self):
        import yaml
        for variant in ('homelab', 'work'):
            overlay = SOURCE.parents[3] / ('13.t3code-' + variant)
            docs = list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(overlay)])))
            statefulset = next(doc for doc in docs if doc and doc['kind'] == 'StatefulSet')
            containers = {container['name']: container for container in statefulset['spec']['template']['spec']['containers']}
            volumes = {volume['name']: volume for volume in statefulset['spec']['template']['spec']['volumes']}
            volume = volumes['scripts']
            names = [entry['configMap']['name'] for entry in volume['projected']['sources']] if 'projected' in volume else [volume['configMap']['name']]
            scripts = {}
            for name in names:
                data = next(doc['data'] for doc in docs if doc and doc['kind'] == 'ConfigMap' and doc['metadata']['name'] == name)
                self.assertFalse(set(scripts) & set(data), 'Script projection contains duplicate paths')
                scripts.update(data)
            if variant == 'work':
                self.assertNotIn('homelab-ssh.conf', scripts)
                self.assertNotIn('ssh-config.sh', scripts)
                continue
            self.assertIn('homelab-ssh.conf', scripts)
            self.assertIn('ssh-config.sh', scripts)
            self.assertIn('ssh-agent-ready.sh', scripts)
            self.assertIn('SSH_AUTH_SOCK', scripts['homelab-ssh.md'])
            self.assertEqual(scripts['homelab-ssh.md'], (overlay / 'files/homelab-ssh.md').read_text())
            env = {entry['name']: entry for entry in containers['vault']['env']}
            self.assertEqual(env['T3_VAULT_SSH_AGENT']['value'], '1')
            for name in ('t3', 'sshd'):
                mounts = {mount['name']: mount for mount in containers[name]['volumeMounts']}
                self.assertTrue(mounts['vault-scope']['readOnly'])
            for probe in ('startupProbe', 'readinessProbe'):
                self.assertEqual(containers['vault'][probe]['exec']['command'],
                                 ['test', '-f', '/run/t3-vault/scope.json'])

    def test_malformed_messages_are_closed_without_breaking_other_clients(self):
        output = io.StringIO()
        with patch.object(vault, 'fetch', return_value={'data': [self.item]}), redirect_stderr(output), \
                vault.ssh_agent(self.scope, self.server):
            for length in (0, 262145):
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(2)
                    client.connect(str(self.socket))
                    client.sendall(struct.pack('!I', length))
                    self.assertEqual(client.recv(4), b'')
            self.assertEqual(self.add('-T', str(self.key.with_suffix('.pub'))).returncode, 0)
        self.assertEqual(output.getvalue().splitlines(), ['vault: SSH agent proxy request refused'] * 2)

    def test_unapproved_peer_uid_cannot_list_keys(self):
        output = io.StringIO()
        with patch.object(vault, 'SSH_CLIENT_UID', os.getuid() + 1), redirect_stderr(output), \
                patch.object(vault, 'fetch', return_value={'data': [self.item]}), vault.ssh_agent(self.scope, self.server):
            self.assertNotEqual(self.add('-L').returncode, 0)
        self.assertEqual(output.getvalue().splitlines(), ['vault: SSH agent proxy request refused'])

    def test_refresh_stops_on_an_unowned_listener(self):
        for result in (False, vault.Refused):
            output = io.StringIO()
            with self.subTest(result=result), redirect_stderr(output), \
                    patch.object(vault, 'fetch', return_value={'data': [self.item]}), \
                    vault.ssh_agent(self.scope, self.server) as agent, patch.object(vault, 'owns_listener',
                    **({'return_value': False} if result is False else {'side_effect': result})):
                with self.assertRaises(vault.UnsafeListener):
                    vault.refresh_keys(agent)
            self.assertEqual(output.getvalue(), '')

    def test_reordered_identical_keys_do_not_reload_the_agent(self):
        other = self.root / 'other'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(other)], check=True)
        second = {**self.item, 'sshKey': {'privateKey': other.read_text()}}
        with patch.object(vault, 'fetch', return_value={'data': [self.item, second]}) as fetch, \
                vault.ssh_agent(self.scope, self.server) as agent:
            fetch.return_value = {'data': [second, self.item]}
            with patch.object(vault, 'loaded_agent', side_effect=AssertionError('Agent reloaded')):
                agent.refresh()

    def test_reload_breaks_proxied_connections_opened_before_the_swap(self):
        other = self.root / 'other'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(other)], check=True)
        request = struct.pack('!I', 1) + bytes([11])  # SSH2_AGENTC_REQUEST_IDENTITIES
        with patch.object(vault, 'fetch', return_value={'data': [self.item]}) as fetch, redirect_stdout(io.StringIO()), \
                redirect_stderr(io.StringIO()), vault.ssh_agent(self.scope, self.server) as agent, \
                socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(self.socket))
            client.sendall(request)
            self.assertEqual(client.recv(5)[4], 12)
            client.recv(65536)
            fetch.return_value = {'data': [{**self.item, 'sshKey': {'privateKey': other.read_text()}}]}
            agent.refresh()
            with contextlib.suppress(OSError):
                client.sendall(request)
            with contextlib.suppress(ConnectionResetError):
                self.assertEqual(client.recv(5), b'')
            self.assertEqual(self.add('-T', str(other.with_suffix('.pub'))).returncode, 0)

    @unittest.skipUnless(os.geteuid() == 0, 'Two-UID signing check needs root')
    def test_uid_1000_signs_through_uid_1001_proxy_but_not_direct_agent(self):
        self.root.chmod(0o755)
        os.chown(self.root, 1001, 1001)
        fixture = self.root / 'fixture.json'
        fixture.write_text(json.dumps(self.item))
        os.chown(fixture, 1001, 1001)
        fixture.chmod(0o600)
        code = '''import importlib.util,json,os,signal,sys,time
from pathlib import Path
s=importlib.util.spec_from_file_location('vault',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
m.SCOPE_FILE=Path(sys.argv[2])/'scope.json'
m.WORK_SSH_INPUTS=Path(sys.argv[2])/'work-inputs'
item=json.loads((Path(sys.argv[2])/'fixture.json').read_text())
m.fetch=lambda _: {'data':[item]}
m.owns_listener=lambda _: True
sockets=[]
loaded=m.loaded_agent
m.loaded_agent=lambda env,path,keys: sockets.append(path) or loaded(env,path,keys)
def stop(*_): raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
with m.ssh_agent(json.loads(sys.argv[3]), object()) as agent:
    private=Path(sockets[-1])
    private.parent.chmod(0o755)
    private.chmod(0o666)
    print('ready',flush=True)
    print(str(private),flush=True)
    time.sleep(30)
'''
        with subprocess.Popen(['python3', '-c', code, str(SOURCE), str(self.root), json.dumps(self.scope)],
                              user=1001, group=1001, extra_groups=[], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE) as process:
            try:
                self.assertEqual(process.stdout.readline(), b'ready\n')
                private_socket = process.stdout.readline().decode().strip()
                direct = subprocess.run(['ssh-add', '-L'], env={**self.env, 'SSH_AUTH_SOCK': private_socket},
                                        user=1000, group=1000, extra_groups=[], capture_output=True, timeout=5)
                self.assertNotEqual(direct.returncode, 0)
                result = subprocess.run(['ssh-add', '-T', str(self.key.with_suffix('.pub'))], env=self.env,
                                        user=1000, group=1000, extra_groups=[], capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0)
                result = subprocess.run(['python3', '-c',
                    'import pathlib,sys; pathlib.Path(sys.argv[1]).read_bytes()', str(fixture)],
                    user=1000, group=1000, extra_groups=[], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
            finally:
                process.terminate()
                process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
