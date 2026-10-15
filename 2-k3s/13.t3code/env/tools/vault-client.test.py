#!/usr/bin/env python3
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from threading import Thread
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'env/files/vault-client.py'
ORG = '11111111-1111-4111-8111-111111111111'
COLLECTION = '22222222-2222-4222-8222-222222222222'
ITEM = '33333333-3333-4333-8333-333333333333'
FOREIGN = '44444444-4444-4444-8444-444444444444'
VALUE = 'synthetic-password-value'
FIELD = 'synthetic-field-value'
CONTENT = b'synthetic-attachment-value\x00\xff'


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        marker = self.home / '.config/t3-runtime/environment'
        marker.parent.mkdir(parents=True)
        marker.write_text('homelab\n')
        marker.chmod(0o600)
        self.scope = self.home / 'scope.json'
        self.scope.write_text(json.dumps({'environment': 'homelab', 'organizationId': ORG, 'collectionId': COLLECTION}))
        self.scope.chmod(0o444)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(('BW_', 'BITWARDENCLI_'))}
        self.env.update(HOME=str(self.home), T3_RUNTIME_VARIANT='homelab', T3_VAULT_CLIENT='1')
        self.item = {'object': 'item', 'id': ITEM, 'organizationId': ORG, 'collectionIds': [COLLECTION],
                     'name': 'synthetic-item', 'login': {'username': 'synthetic-user', 'password': VALUE},
                     'fields': [{'name': 'api', 'value': FIELD, 'type': 1}],
                     'attachments': [{'id': 'syntheticattachment', 'fileName': 'fixture.bin', 'size': str(len(CONTENT))}]}
        self.extra_items = []
        self.paths = []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                test.paths.append(self.path)
                parsed = urlsplit(self.path)
                if parsed.path == '/object/attachment/syntheticattachment':
                    if parse_qs(parsed.query) != {'itemid': [ITEM]}:
                        self.send_error(400)
                        return
                    data = CONTENT
                    content_type = 'application/octet-stream'
                else:
                    content_type = 'application/json'
                    if parsed.path == '/list/object/items':
                        data = {'object': 'list', 'data': [test.item, *test.extra_items]}
                    elif parsed.path == '/object/item/' + ITEM:
                        data = test.item
                    elif parsed.path == '/object/item/' + FOREIGN:
                        data = {**test.item, 'id': FOREIGN, 'organizationId': FOREIGN}
                    else:
                        self.send_error(400)
                        return
                    data = json.dumps({'success': True, 'data': data}).encode()
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.prefix = [sys.executable, '-c',
            'import importlib.util,sys; s=importlib.util.spec_from_file_location("vault",sys.argv[1]); '
            'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
            'm.ADDRESS=sys.argv[2]; m.SCOPE_FILE=m.Path(sys.argv[3]); sys.exit(m.main(sys.argv[4:]))', str(SOURCE),
            f'http://127.0.0.1:{self.server.server_port}', str(self.scope)]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def command(self, options, code, **kwargs):
        return subprocess.run(self.prefix + ['run', *options, '--', sys.executable, '-c', code],
                              env=self.env, capture_output=True, **kwargs)

    def assert_quiet(self, result):
        for secret in (VALUE.encode(), FIELD.encode(), CONTENT, b'synthetic-login-password', b'synthetic-session'):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_password_field_private_attachment_and_output_suppression(self):
        record = self.home / 'path'
        result = self.command(['--item', ITEM, '--password-env', 'VAULT_PASSWORD', '--field', 'api=VAULT_API',
                               '--attachment', 'fixture.bin', '--attachment-env', 'VAULT_FILE'],
            f'import os,pathlib,stat; p=pathlib.Path(os.environ["VAULT_FILE"]); '
            f'assert p.read_bytes()=={CONTENT!r}; assert stat.S_IMODE(p.stat().st_mode)==0o600; '
            'assert stat.S_IMODE(p.parent.stat().st_mode)==0o700; '
            f'assert os.environ["VAULT_PASSWORD"]=={VALUE!r}; assert os.environ["VAULT_API"]=={FIELD!r}; '
            'assert not any(k.startswith("BW_") for k in os.environ); '
            f'pathlib.Path({str(record)!r}).write_text(str(p)); '
            'print(os.environ["VAULT_PASSWORD"]); print(os.environ["VAULT_API"],file=__import__("sys").stderr)')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_quiet(result)
        self.assertFalse(Path(record.read_text()).parent.exists())
        self.assertIn('/object/attachment/syntheticattachment?itemid=' + ITEM, self.paths)

    def test_readonly_scope_file_supports_work_and_refuses_wrong_or_missing_scope(self):
        marker = self.home / '.config/t3-runtime/environment'
        self.env['T3_RUNTIME_VARIANT'] = 'work'
        marker.write_text('work\n')
        self.assertEqual(self.command(['--item', ITEM, '--password-env', 'VAULT_PASSWORD'], 'pass').returncode, 1)
        self.scope.chmod(0o600)
        self.scope.write_text(json.dumps({'environment': 'work', 'organizationId': ORG, 'collectionId': COLLECTION}))
        self.scope.chmod(0o444)
        self.assertEqual(self.command(['--item', ITEM, '--password-env', 'VAULT_PASSWORD'], 'pass').returncode, 0)
        self.scope.unlink()
        self.assertEqual(self.command(['--item', ITEM, '--password-env', 'VAULT_PASSWORD'], 'pass').returncode, 1)

    def test_work_scope_refuses_homelab_item_before_child_and_runs_work_values_quietly(self):
        (self.home / '.config/t3-runtime/environment').write_text('work\n')
        self.env['T3_RUNTIME_VARIANT'] = 'work'
        self.scope.chmod(0o600)
        self.scope.write_text(json.dumps({'environment': 'work', 'organizationId': FOREIGN, 'collectionId': FOREIGN}))
        self.scope.chmod(0o444)
        record = self.home / 'ran'
        result = self.command(['--item', ITEM, '--password-env', 'VAULT_PASSWORD'],
                              f'open({str(record)!r},"w").close()')
        self.assertEqual(result.returncode, 1)
        self.assertFalse(record.exists())
        self.assert_quiet(result)
        self.item.update(organizationId=FOREIGN, collectionIds=[FOREIGN])
        result = self.command(['--item', ITEM, '--password-env', 'VAULT_PASSWORD', '--field', 'api=VAULT_API',
                               '--attachment', 'fixture.bin', '--attachment-env', 'VAULT_FILE'],
            f'import os,pathlib; assert os.environ["VAULT_PASSWORD"]=={VALUE!r}; '
            f'assert os.environ["VAULT_API"]=={FIELD!r}; '
            'p=pathlib.Path(os.environ["VAULT_FILE"]); '
            f'assert p.read_bytes()=={CONTENT!r}; pathlib.Path({str(record)!r}).write_text(str(p)); '
            'print(os.environ["VAULT_PASSWORD"]); print(os.environ["VAULT_API"],file=__import__("sys").stderr)')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(Path(record.read_text()).parent.exists())
        self.assert_quiet(result)

    def test_exact_name_and_ambiguous_missing_or_foreign_names(self):
        result = self.command(['--name', 'synthetic-item', '--password-env', 'VAULT_PASSWORD'], 'pass')
        self.assertEqual(result.returncode, 0, result.stderr)
        original = copy.deepcopy(self.item)
        self.extra_items.append(copy.deepcopy(self.item))
        self.assertEqual(self.command(['--name', 'synthetic-item', '--password-env', 'VAULT_PASSWORD'], 'pass').returncode, 1)
        self.extra_items.clear()
        for change in ({'name': 'synthetic-item-extra'}, {'organizationId': FOREIGN}, {'collectionIds': []}):
            self.item = {**original, **change}
            result = self.command(['--name', 'synthetic-item', '--password-env', 'VAULT_PASSWORD'], 'pass')
            self.assertEqual(result.returncode, 1)
            self.assert_quiet(result)

    def test_foreign_nonexistent_and_wrong_collection_refused_before_child(self):
        record = self.home / 'ran'
        for identity in (FOREIGN, '55555555-5555-4555-8555-555555555555'):
            result = self.command(['--item', identity, '--password-env', 'VAULT_PASSWORD'],
                                  f'open({str(record)!r},"w").close()')
            self.assertEqual(result.returncode, 1)
            self.assertFalse(record.exists())
            self.assert_quiet(result)
        self.item['collectionIds'] = []
        self.assertEqual(self.command(['--item', ITEM, '--password-env', 'VAULT_PASSWORD'], 'pass').returncode, 1)

    def test_timeout_and_sigterm_remove_file_and_kill_descendants(self):
        for terminate in (False, True):
            record = self.home / ('signal' if terminate else 'timeout')
            options = ['run', '--item', ITEM, '--attachment', 'fixture.bin', '--attachment-env', 'VAULT_FILE',
                       '--timeout', '10' if terminate else '0.5', '--', sys.executable, '-c',
                       'import os,pathlib,subprocess,time; '
                       'p=subprocess.Popen(["sleep","30"]); '
                       f'pathlib.Path({str(record)!r}).write_text(os.environ["VAULT_FILE"]+"\\n"+str(p.pid)); '
                       'time.sleep(30)']
            with subprocess.Popen(self.prefix + options, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                deadline = time.monotonic() + 5
                while not record.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(record.exists())
                path, pid = record.read_text().splitlines()
                if terminate:
                    process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 130 if terminate else 124, stderr)
                self.assertFalse(Path(path).parent.exists())
                state = Path(f'/proc/{pid}/stat')
                self.assertTrue(not state.exists() or state.read_text().split()[2] == 'Z')
                self.assertNotIn(CONTENT, stdout + stderr)

    def test_unsafe_targets_missing_values_and_duplicate_fields_refused(self):
        options = [
            ['--password-env', 'PATH'], ['--password-env', 'BW_PASSWORD'],
            ['--field', 'missing=VAULT_API'], ['--password-env', 'VAULT_X', '--field', 'api=VAULT_X'],
            ['--attachment', 'fixture.bin', '--attachment-env', 'VAULT_X', '--password-env', 'VAULT_X']]
        for args in options:
            result = self.command(['--item', ITEM, *args], 'pass')
            self.assertEqual(result.returncode, 1)
            self.assert_quiet(result)
        self.item['fields'].append(copy.deepcopy(self.item['fields'][0]))
        self.assertEqual(self.command(['--item', ITEM, '--field', 'api=VAULT_API'], 'pass').returncode, 1)


if __name__ == '__main__':
    unittest.main()
