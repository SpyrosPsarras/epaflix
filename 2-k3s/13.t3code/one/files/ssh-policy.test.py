import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class SshPolicy(unittest.TestCase):
    def test_vault_attachment_allowed_but_ssh_files_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            policy = home / '.cc-safety-net/policy.json'
            policy.parent.mkdir()
            (home / '.ssh').mkdir()
            key_type = b'ssh-ed25519'
            key = base64.b64encode(len(key_type).to_bytes(4, 'big') + key_type + (32).to_bytes(4, 'big') + bytes(32)).decode()
            (home / '.ssh/known_hosts').write_text(f'192.168.10.30 ssh-ed25519 {key} comment\nPRIVATE DATA\n')
            policy.write_text(json.dumps({'version': 1, 'secret_protection': {
                'overrides': {'secret.cli.opencode': 'off'},
                'deny_paths': ['/existing/protected', str(home / '.ssh')],
            }}))
            subprocess.run(['python3', str(Path(__file__).with_name('ssh-policy.py')),
                            str(policy), str(home), '/home/node', '/home/spyros'], check=True)
            first = policy.read_bytes()
            subprocess.run(['python3', str(Path(__file__).with_name('ssh-policy.py')),
                            str(policy), str(home), '/home/node', '/home/spyros'], check=True)
            self.assertEqual(policy.read_bytes(), first)
            data = json.loads(first)
            self.assertEqual(data['secret_protection']['overrides']['secret.cli.opencode'], 'off')
            self.assertIn('/existing/protected', data['secret_protection']['deny_paths'])
            self.assertEqual((home / '.config/ssh/known_hosts').read_text(), f'192.168.10.30 ssh-ed25519 {key}\n')
            cli = os.environ.get('SAFETY_NET_CLI', 'cc-safety-net')
            cases = [
                ('''python3 -c 'import subprocess,json; subprocess.check_output(["python3","scripts/vault.py","vault_attachment",json.dumps({"path":"/SSH/spyros","filename":"id_ed25519"})])' ''', 'allowed'),
                ('ssh -i /tmp/opencode/identity root@192.168.10.30 "systemctl is-active pihole-FTL unbound"', 'allowed'),
                ('cat /home/spyros/.ssh/id_ed25519', 'blocked'),
                ('cat /home/node/.ssh/id_ed25519', 'blocked'),
                ('cat /home/spyros/.ssh/id_rsa', 'blocked'),
                ('cat /home/spyros/.ssh/id_custom', 'blocked'),
                ('cat /run/t3-github-ssh/identity', 'blocked'),
                ('cat /existing/protected', 'blocked'),
                (f'cat {home}/.config/ssh/known_hosts', 'allowed'),
                (f'cat {home}/.ssh/known_hosts', 'blocked'),
                (f'cat {home}/.ssh/id_ed25519', 'blocked'),
                (f'cat {home}/.ssh/custom-identity', 'blocked'),
                ('git push --force origin main', 'blocked'),
            ]
            for command, expected in cases:
                with self.subTest(command=command):
                    result = subprocess.run([cli, 'explain', '--json', command],
                                            env={**os.environ, 'HOME': directory, 'CC_SAFETY_NET_HOME': str(policy.parent)},
                                            capture_output=True, text=True, check=True)
                    self.assertEqual(json.loads(result.stdout)['result'], expected)
            (home / '.ssh/known_hosts').unlink()
            (home / '.ssh/custom-identity').write_text('PRIVATE DATA')
            (home / '.ssh/known_hosts').symlink_to(home / '.ssh/custom-identity')
            subprocess.run(['python3', str(Path(__file__).with_name('ssh-policy.py')),
                            str(policy), str(home), '/home/node', '/home/spyros'], check=True)
            self.assertEqual((home / '.config/ssh/known_hosts').read_text(), '')
            (home / '.ssh/known_hosts').unlink()
            bad_key = base64.b64encode(len(key_type).to_bytes(4, 'big') + key_type + b'SYNTHETIC_PRIVATE_PAYLOAD').decode()
            (home / '.ssh/known_hosts').write_text(f'host ssh-ed25519 {bad_key}\n  @revoked host ssh-ed25519 {key}\n')
            subprocess.run(['python3', str(Path(__file__).with_name('ssh-policy.py')),
                            str(policy), str(home)], check=True)
            self.assertEqual((home / '.config/ssh/known_hosts').read_text(), f'@revoked host ssh-ed25519 {key}\n')
            (home / '.ssh/known_hosts').write_bytes(b'\xff\n')
            subprocess.run(['python3', str(Path(__file__).with_name('ssh-policy.py')),
                            str(policy), str(home)], check=True)
            self.assertEqual((home / '.config/ssh/known_hosts').read_text(), '')


if __name__ == '__main__':
    unittest.main()
