import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HUB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUB / 'files'))
import mcp_hub
import vaultwarden_reader_http
BUILDER = HUB / 'reader/provision_inputs.py'
IDS = ['00000000-0000-4000-8000-' + str(n).zfill(12) for n in range(1, 9)]
VALUES = [*IDS[:7], 'user.' + IDS[7], 'synthetic-api-key', 'synthetic-master-password with spaces\n']


class ProvisionTests(unittest.TestCase):
    def run_wizard(self, existing=False, approve=True, changed=False, email_match=True, kdf=0,
                   changed_field='kdfIterations'):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            commands = root / 'bin'
            commands.mkdir()
            log = root / 'calls'
            kubectl = '''import base64,json,os,pathlib,sys
args=sys.argv[1:]
assert args[:2]==['--context','epaflix']
with open(os.environ['STUB_LOG'],'a') as stream: stream.write(' '.join(args)+'\\n')
if args[2]=='config': print('epaflix')
elif args[2:4]==['get','namespace']: print('namespace/'+args[4])
elif 'secret' in args:
    if os.environ['STUB_EXISTING']=='1': print('secret/'+args[6])
elif args[2]=='apply':
    items=json.load(sys.stdin)['items']
    assert len(items)==3
    assert base64.b64decode(items[0]['data']['password'])==b'synthetic-master-password with spaces'
    for item in items:
        assert item['metadata']['namespace'] in ('vault-reader-homelab','mcp-hub-homelab')
    print('synthetic apply output must be suppressed')
else: sys.exit(1)
'''
            curl = '''import json,os,pathlib,sys
assert sys.argv[-1]=='https://vaultwarden.epaflix.com/identity/accounts/prelogin'
assert json.load(sys.stdin)=={'email':'synthetic-reader@example.test'}
path=pathlib.Path(os.environ['STUB_LOG'])
count=path.read_text().count('prelogin')
with path.open('a') as stream: stream.write('prelogin\\n')
value={'kdf':int(os.environ['STUB_KDF']),'kdfIterations':600000,'kdfMemory':64,'kdfParallelism':4}
if count and os.environ['STUB_CHANGED']=='1': value[os.environ['STUB_CHANGED_FIELD']]+=1
print(json.dumps(value))
'''
            for name, content in [('kubectl', kubectl), ('curl', curl), ('xdg-open', 'pass\n')]:
                file = commands / name
                file.write_text('#!' + sys.executable + '\n' + content)
                file.chmod(0o700)
            env = dict(os.environ, PATH=str(commands) + ':' + os.environ['PATH'], STUB_LOG=str(log),
                       STUB_EXISTING=str(int(existing)), STUB_CHANGED=str(int(changed)), STUB_KDF=str(kdf),
                       STUB_CHANGED_FIELD=changed_field)
            replies = ['\n', '\n', 'y\n', '\n', 'y\n', '\n', *(['y\n'] * 7),
                       'APPROVE CANARY GRANTS\n', *[v + '\n' for v in IDS[:7]], '\n',
                       'user.' + IDS[7] + '\n', 'synthetic-api-key\n',
                       'synthetic-master-password with spaces\n', 'synthetic-master-password with spaces\n',
                       'synthetic-reader@example.test\n',
                       ('synthetic-reader@example.test' if email_match else 'synthetic-other@example.test') + '\n',
                       '\n', 'y\n', '\n']
            if existing:
                replies = (['y\n'] * 3 + replies) if approve else ['n\n']
            result = subprocess.run(['bash', str(HUB / 'reader/provision-homelab.sh')],
                                    input=''.join(replies), capture_output=True, text=True, env=env, cwd=root, timeout=20)
            calls = log.read_text() if log.exists() else ''
            for value in ['synthetic-api-key', 'synthetic-master-password with spaces', 'synthetic-reader@example.test',
                          'synthetic-other@example.test', 'synthetic apply output must be suppressed']:
                self.assertNotIn(value, result.stdout + result.stderr + calls)
            self.assertFalse((root / '.env').exists())
            return result, calls

    def test_desktop_wizard_stdin_apply_and_kdf_comparison(self):
        result, calls = self.run_wizard()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('apply --server-side --field-manager=vault-reader-provisioner -f -', calls)
        self.assertEqual(calls.count('prelogin'), 2)
        self.assertIn('KDF type: SAME', result.stdout)
        self.assertIn('KDF iterations: SAME', result.stdout)

    def test_reader_email_mismatch_stops_before_prelogin_or_apply(self):
        result, calls = self.run_wizard(email_match=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Reader emails do not match.', result.stdout)
        self.assertNotIn('prelogin', calls)
        self.assertNotIn('apply', calls)

    def test_argon2_memory_and_parallelism_comparison(self):
        result, calls = self.run_wizard(kdf=1)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls.count('prelogin'), 2)
        self.assertIn('KDF settings: 1 600000 64 4', result.stdout)
        self.assertIn('KDF memory: SAME', result.stdout)
        self.assertIn('KDF parallelism: SAME', result.stdout)
        for field, label in [('kdfMemory', 'memory'), ('kdfParallelism', 'parallelism')]:
            with self.subTest(field=field):
                result, calls = self.run_wizard(kdf=1, changed=True, changed_field=field)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('KDF ' + label + ': CHANGED', result.stdout)

    def test_argon2_snapshot_requires_positive_integer_memory_and_parallelism(self):
        for field in ('kdfMemory', 'kdfParallelism'):
            for invalid in (None, 0, -1, True, '64'):
                with self.subTest(field=field, invalid=invalid):
                    value = {'kdf': 1, 'kdfIterations': 3, 'kdfMemory': 64, 'kdfParallelism': 4}
                    value[field] = invalid
                    result = subprocess.run([sys.executable, str(BUILDER), '--kdf'],
                                            input=json.dumps(value), capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, '')
                    self.assertEqual(result.stderr, 'provisioning inputs refused\n')

    def test_desktop_wizard_requires_replacement_approval_and_detects_kdf_changes(self):
        result, calls = self.run_wizard(existing=True, approve=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('apply', calls)
        result, calls = self.run_wizard(existing=True, changed=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('apply', calls)
        self.assertIn('KDF iterations: CHANGED', result.stdout)

    def build(self, values=VALUES):
        self.assertTrue(BUILDER.exists(), 'stdin Secret builder is missing')
        return subprocess.run([sys.executable, str(BUILDER)], input=b'\0'.join(v.encode() for v in values) + b'\0',
                              capture_output=True)

    def test_secret_bytes_and_real_reader_hub_validation(self):
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b'')
        secrets = json.loads(result.stdout)['items']
        self.assertEqual([s['metadata']['name'] for s in secrets],
                         ['vault-reader-inputs', 'mcp-hub-instance', 'vault-canary-client'])
        data = [{k: base64.b64decode(v) for k, v in s['data'].items()} for s in secrets]
        self.assertEqual(set(data[0]), {'config.json', 'client-id', 'client-secret', 'password',
                                      'metadata-token', 'broker-token'})
        self.assertEqual(data[0]['password'], VALUES[-1].encode())
        self.assertEqual(data[0]['client-id'], VALUES[-3].encode())
        self.assertEqual(set(data[1]), {'services.json', 'credentials.json'})
        self.assertEqual(set(data[2]), {'token', 'services.json', 'canary.json'})
        self.assertEqual(data[1]['services.json'], data[2]['services.json'])
        credentials = json.loads(data[1]['credentials.json'])
        self.assertEqual(credentials['clients']['canary'], hashlib.sha256(data[2]['token']).hexdigest())
        self.assertEqual(json.loads(data[0]['config.json'])['reader']['server_url'], 'https://vaultwarden.epaflix.com')
        fixture = json.loads(data[2]['canary.json'])
        self.assertEqual(fixture['duplicate_item_ids'], IDS[4:6])
        self.assertEqual(fixture['collection_id'], IDS[1])
        self.assertEqual(fixture['denied_collection_id'], IDS[2])
        for key in ('metadata-token', 'broker-token'):
            vaultwarden_reader_http.token_value(data[0][key].decode())
        self.assertNotEqual(data[0]['metadata-token'], data[0]['broker-token'])
        data[0]['metadata-token'] = b'synthetic-metadata-bearer-0000000000000000'
        data[0]['broker-token'] = b'synthetic-broker-bearer-000000000000000000'
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            config = json.loads(data[0]['config.json'])
            for key in ('metadata_token_file', 'broker_token_file'):
                config[key] = str(home / key.replace('_file', '').replace('_', '-'))
            for name, value in data[0].items():
                file = home / name
                file.write_bytes(json.dumps(config).encode() if name == 'config.json' else value)
                file.chmod(0o600)
            vaultwarden_reader_http.load_config(home / 'config.json')
            def private_stream(path, *_):
                return io.StringIO(data[1][Path(path).name].decode())

            with patch('builtins.open', side_effect=private_stream):
                app = mcp_hub.instance_app('homelab', home / 'services.json', home / 'credentials.json')
            self.assertTrue(app.state.restricted_reader)

    def test_bad_ids_and_login_bytes_refuse_without_payload_or_secret_diagnostics(self):
        cases = []
        for index, value in [(0, 'not-a-uuid'), (2, IDS[1]), (8, ''), (9, ''),
                              (9, 'synthetic-too-long-' + 'x' * 16384), (7, 'user.NOT-A-UUID')]:
            values = list(VALUES)
            values[index] = value
            cases.append(values)
        for values in cases:
            with self.subTest(index=cases.index(values)):
                result = self.build(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b'')
                self.assertEqual(result.stderr, b'provisioning inputs refused\n')


if __name__ == '__main__':
    unittest.main()
