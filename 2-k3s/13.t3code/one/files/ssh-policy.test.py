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
            policy = home / 'policy.json'
            policy.write_text(json.dumps({'version': 1, 'secret_protection': {
                'overrides': {'secret.cli.opencode': 'off'},
                'deny_paths': ['/existing/protected'],
            }}))
            subprocess.run(['python3', str(Path(__file__).with_name('ssh-policy.py')),
                            str(policy), '/home/spyros', '/home/node'], check=True)
            first = policy.read_bytes()
            subprocess.run(['python3', str(Path(__file__).with_name('ssh-policy.py')),
                            str(policy), '/home/spyros', '/home/node'], check=True)
            self.assertEqual(policy.read_bytes(), first)
            data = json.loads(first)
            self.assertEqual(data['secret_protection']['overrides']['secret.cli.opencode'], 'off')
            self.assertIn('/existing/protected', data['secret_protection']['deny_paths'])
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
                ('git push --force origin main', 'blocked'),
            ]
            for command, expected in cases:
                with self.subTest(command=command):
                    result = subprocess.run([cli, 'explain', '--json', command],
                                            env={**os.environ, 'CC_SAFETY_NET_HOME': directory},
                                            capture_output=True, text=True, check=True)
                    self.assertEqual(json.loads(result.stdout)['result'], expected)


if __name__ == '__main__':
    unittest.main()
