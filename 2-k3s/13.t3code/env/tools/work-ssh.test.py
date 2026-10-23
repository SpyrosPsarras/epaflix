#!/usr/bin/env python3
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shared = load('shared_ssh_tests', Path(__file__).with_name('homelab-ssh.test.py'))
work = load('work_ssh', ROOT / '13.t3code-work/files/work-ssh.py')


class WorkAgentTests(shared.AgentTests):
    def setUp(self):
        super().setUp()
        self.scope = {**self.scope, 'environment': 'work',
                      'organizationId': '55555555-5555-4555-8555-555555555555'}
        self.item['organizationId'] = self.scope['organizationId']
        self.work_inputs = self.root / 'work-inputs'
        self.work_inputs.mkdir()
        (self.work_inputs / 'work-ssh.py').write_bytes(Path(work.__file__).read_bytes())
        self.host_input()
        inputs_patch = patch.object(shared.vault, 'WORK_SSH_INPUTS', self.work_inputs)
        inputs_patch.start()
        self.addCleanup(inputs_patch.stop)

    def test_work_agent_never_loads_homelab_keys(self):
        homelab = self.root / 'homelab'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(homelab)], check=True)
        foreign = {**self.item, 'organizationId': shared.SCOPE['organizationId'],
                   'sshKey': {'privateKey': homelab.read_text()}}
        with patch.object(shared.vault, 'fetch', return_value={'data': [foreign, self.item]}), \
                shared.vault.ssh_agent(self.scope, self.server):
            self.assertEqual(self.add('-T', str(self.key.with_suffix('.pub'))).returncode, 0)
            self.assertNotEqual(self.add('-T', str(homelab.with_suffix('.pub'))).returncode, 0)
            self.assertEqual(len(self.add('-L').stdout.splitlines()), 1)

    def host_input(self):
        entry = {'alias': 'work-dev', 'address': '192.0.2.10', 'user': 'node', 'port': 2222,
                 'hostKey': ' '.join(self.key.with_suffix('.pub').read_text().split()[:2])}
        path = self.work_inputs / 'hosts.json'
        path.write_text(json.dumps([entry]))
        return entry, path

    def test_missing_or_empty_host_input_keeps_config_and_agent_empty(self):
        entry, path = self.host_input()
        for content in (None, '', '  \n', '[]'):
            with self.subTest(content=content):
                work.install([entry], [self.root, self.root / 'login'])
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(content)
                output = io.StringIO()
                with patch.object(work, 'INPUTS', self.work_inputs), \
                        patch.object(work.Path, 'home', return_value=self.root), \
                        patch.object(work.pwd, 'getpwuid', return_value=SimpleNamespace(pw_dir=str(self.root / 'login'))), \
                        redirect_stdout(output):
                    self.assertEqual(work.main(['install']), 0)
                self.assertEqual(output.getvalue(), 'work SSH: no approved hosts; SSH disabled\n')
                self.assertEqual(work.hosts(path), [])
                self.assertEqual(work.policy(work.hosts(path))['spec']['egress'], [])
                for home in (self.root, self.root / 'login'):
                    config = home / '.ssh/config'
                    self.assertNotIn('Host work-dev', config.read_text())
                    self.assertEqual((home / '.ssh/work_known_hosts').read_text(), '')
                    result = subprocess.run(['ssh', '-G', '-F', str(config), 'work-dev'],
                                            capture_output=True, text=True, check=True)
                    settings = dict(line.split(' ', 1) for line in result.stdout.splitlines())
                    self.assertEqual(settings['identityagent'], 'none')
                    self.assertEqual(settings['stricthostkeychecking'], 'true')
                    self.assertEqual(settings['userknownhostsfile'], '/dev/null')
                    self.assertEqual(settings['proxycommand'], 'false')
                with patch.object(shared.vault, 'fetch', side_effect=AssertionError('No hosts must not fetch keys')), \
                        redirect_stdout(io.StringIO()) as agent_output, \
                        shared.vault.ssh_agent(self.scope, self.server) as agent:
                    self.assertIsNone(agent.poll())
                    self.assertEqual(self.add('-L').returncode, 1)
                    self.assertNotEqual(self.add('-T', str(self.key.with_suffix('.pub'))).returncode, 0)
                    self.assertEqual(agent_output.getvalue(), '')
                self.assertFalse(self.socket.exists())

    def test_present_invalid_hosts_refuse_config_and_agent_before_key_fetch(self):
        path = self.work_inputs / 'hosts.json'
        for content in ('not-json', '{}', '[{}]', 'null'):
            with self.subTest(content=content):
                path.write_text(content)
                output, errors = io.StringIO(), io.StringIO()
                with patch.object(work, 'INPUTS', self.work_inputs), redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(work.main(['install']), 1)
                self.assertEqual(output.getvalue(), '')
                self.assertEqual(errors.getvalue(), 'work SSH: invalid or unavailable private input\n')
                with patch.object(shared.vault, 'fetch', side_effect=AssertionError('Invalid hosts must not fetch keys')):
                    with self.assertRaises(ValueError), shared.vault.ssh_agent(self.scope, self.server):
                        self.fail('Invalid host input was accepted')
                self.assertFalse(self.socket.exists())

    def test_only_private_approved_aliases_resolve_and_host_keys_are_pinned(self):
        entry, path = self.host_input()
        login = self.root / 'login'
        work.install(work.hosts(path), [self.root, login])
        for home in (self.root, login):
            config = home / '.ssh/config'
            result = subprocess.run(['ssh', '-G', '-F', str(config), entry['alias']],
                                    capture_output=True, text=True, check=True)
            settings = dict(line.split(' ', 1) for line in result.stdout.splitlines())
            for name, value in {'hostname': entry['address'], 'port': '2222', 'user': 'node',
                                'identityagent': '/run/t3-vault/ssh-agent.sock', 'identityfile': 'none',
                                'stricthostkeychecking': 'true', 'passwordauthentication': 'no',
                                'kbdinteractiveauthentication': 'no', 'forwardagent': 'no',
                                'preferredauthentications': 'publickey', 'batchmode': 'yes'}.items():
                self.assertEqual(settings[name], value)
            self.assertNotIn('proxycommand', settings)
            self.assertEqual(settings['userknownhostsfile'], str(home / '.ssh/work_known_hosts'))
            self.assertEqual(settings['globalknownhostsfile'], '/dev/null')
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            known = home / '.ssh/work_known_hosts'
            self.assertEqual(known.stat().st_mode & 0o777, 0o600)
            self.assertEqual(known.read_text(), '[192.0.2.10]:2222 ' + entry['hostKey'] + '\n')
            denied = [(host, []) for host in ('homepc', 'laptop', 'unapproved', entry['address'], 'work-dev.example.test')]
            denied.append((entry['alias'], ['-o', 'HostName=203.0.113.9']))
            for unknown, options in denied:
                result = subprocess.run(['ssh', '-G', '-F', str(config), *options, unknown],
                                        capture_output=True, text=True, check=True)
                settings = dict(line.split(' ', 1) for line in result.stdout.splitlines())
                self.assertEqual(settings['proxycommand'], 'false')
                self.assertEqual(settings['identityagent'], 'none')
                self.assertEqual(settings['stricthostkeychecking'], 'true')
                self.assertEqual(settings['userknownhostsfile'], '/dev/null')
                self.assertEqual(settings['globalknownhostsfile'], '/dev/null')
                self.assertNotIn('homelab', config.read_text())
        entry['alias'], entry['address'] = 'work-replacement', '192.0.2.11'
        work.install([entry], [self.root, login])
        self.assertNotIn('work-dev', (self.root / '.ssh/config').read_text())
        self.assertNotIn('192.0.2.10', (self.root / '.ssh/work_known_hosts').read_text())

    def test_github_is_the_only_hostname_and_host_keys_are_pinned(self):
        login = self.root / 'login'
        for entries in ([], work.hosts(self.host_input()[1])):
            work.install(entries, [self.root, login])
            for home in (self.root, login):
                with self.subTest(hosts=len(entries), home=home):
                    config = home / '.ssh/config'
                    shared.assert_github_pinned(self, config, home / '.ssh/github_known_hosts')
                    blocks = [line for line in config.read_text().splitlines() if line.startswith(('Host ', 'Match '))]
                    self.assertEqual(blocks[-1], 'Host *')

    def test_policy_uses_the_same_private_exact_addresses_and_ports(self):
        entry, path = self.host_input()
        entries = work.hosts(path)
        document = work.policy(entries)
        self.assertEqual(document['metadata'], {'name': 't3code-work-ssh', 'namespace': 't3code-work'})
        self.assertEqual(document['spec']['podSelector'], {'matchLabels': {'app.kubernetes.io/name': 't3code'}})
        self.assertEqual(document['spec']['policyTypes'], ['Egress'])
        self.assertEqual(document['spec']['egress'], [
            {'to': [{'ipBlock': {'cidr': '192.0.2.10/32'}}], 'ports': [{'protocol': 'TCP', 'port': 2222}]}])
        output = self.root / 'policy.json'
        result = subprocess.run(['python3', str(ROOT / '13.t3code-work/files/work-ssh.py'),
                                 'policy', str(path), str(output)], capture_output=True, text=True, check=True)
        self.assertNotIn(entry['address'], result.stdout + result.stderr)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(output.read_text()), document)
        entries[0]['address'] = '2001:db8::1'
        self.assertEqual(work.policy(entries)['spec']['egress'][0]['to'][0]['ipBlock']['cidr'], '2001:db8::1/128')

    def test_private_input_rejects_injection_wildcards_duplicates_and_unpinned_hosts(self):
        entry, path = self.host_input()
        invalid = [{}, [entry, entry], [{**entry, 'alias': '*'}],
                   [{**entry, 'address': 'work.example.test'}], [{**entry, 'address': 'github.com'}],
                   [{**entry, 'alias': 'github.com'}], [{**entry, 'address': '127.0.0.1'}],
                   [{**entry, 'address': '169.254.1.1'}], [{**entry, 'address': '0.0.0.0'}],
                   [{**entry, 'address': '192.0.2.0/24'}], [{**entry, 'address': '2001:db8::1%eth0'}],
                   [{**entry, 'port': True}],
                   [{**entry, 'port': 0}], [{**entry, 'port': 65536}],
                   [{**entry, 'user': 'node\nProxyCommand bad'}], [{**entry, 'ProxyCommand': 'bad'}],
                   [{**entry, 'hostKey': 'ssh-ed25519 aW52YWxpZA=='}],
                   [{**entry, 'hostKey': entry['hostKey'] + '\nHost *'}]]
        for entries in invalid:
            with self.subTest(index=invalid.index(entries)):
                path.write_text(json.dumps(entries))
                with self.assertRaises((ValueError, TypeError)):
                    work.hosts(path)
        path.write_text('private invalid input')
        result = subprocess.run(['python3', str(ROOT / '13.t3code-work/files/work-ssh.py'),
                                 'policy', str(path), str(self.root / 'invalid.json')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('private invalid input', result.stdout + result.stderr)
        self.assertFalse((self.root / 'invalid.json').exists())

    def test_work_render_mounts_only_work_config_and_shared_read_only_agent(self):
        import yaml
        docs = list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(ROOT / '13.t3code-work')])))
        state = next(doc for doc in docs if doc and doc['kind'] == 'StatefulSet')['spec']['template']['spec']
        containers = {container['name']: container for container in state['containers']}
        volumes = {volume['name']: volume for volume in state['volumes']}
        sources = volumes['work-ssh']['projected']['sources']
        self.assertEqual(sources[0]['secret'], {'name': 't3-work-ssh', 'optional': True,
                                               'items': [{'key': 'hosts.json', 'path': 'hosts.json'}]})
        self.assertEqual(sources[1]['configMap']['items'], [{'key': 'work-ssh.py', 'path': 'work-ssh.py'}])
        self.assertTrue(any(doc and doc['kind'] == 'ConfigMap' and doc['metadata']['name'] == sources[1]['configMap']['name']
                            and 'work-ssh.py' in doc['data'] for doc in docs))
        self.assertFalse(any(doc and doc['kind'] == 'NetworkPolicy' and doc['metadata']['name'] == 't3code-work-ssh' for doc in docs))
        self.assertIn({'name': 'T3_VAULT_SSH_AGENT', 'value': '1'}, containers['vault']['env'])
        for name in ('t3', 'sshd'):
            mounts = {mount['name']: mount for mount in containers[name]['volumeMounts']}
            self.assertTrue(mounts['vault-scope']['readOnly'])
            if name == 't3':
                self.assertTrue(mounts['work-ssh']['readOnly'])
            else:
                self.assertNotIn('work-ssh', mounts)
        self.assertTrue(next(mount for mount in containers['vault']['volumeMounts']
                             if mount['name'] == 'work-ssh')['readOnly'])
        scripts = {}
        for source in volumes['scripts']['projected']['sources']:
            scripts.update(next(doc['data'] for doc in docs if doc and doc['kind'] == 'ConfigMap'
                                and doc['metadata']['name'] == source['configMap']['name']))
        self.assertIn('work-ssh.py', scripts)
        self.assertIn('work-ssh.md', scripts)
        self.assertNotIn('homelab-ssh.conf', scripts)
        self.assertNotIn('ssh-config.sh', scripts)
        self.assertIn('python3 /scripts/work-ssh.py install', scripts['entrypoint.sh'])
        self.assertIn('bash /scripts/ssh-agent-ready.sh', scripts['entrypoint.sh'])
        self.assertIn('ssh-agent-ready.sh', scripts)
        self.assertNotIn('SSH_AUTH_SOCK', scripts['entrypoint.sh'])
        self.assertNotIn('SSH_AUTH_SOCK', scripts['sshd.sh'])
        self.assertNotIn('ssh-keyscan', scripts['work-ssh.py'])


if __name__ == '__main__':
    unittest.main()
