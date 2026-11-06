#!/usr/bin/env python3
"""Offline checks for keeper-client.py: sidecar reads and writes against fake Commander objects,
and the t3-side client against a real local HTTP server. No Keeper account or network."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from http.server import HTTPServer
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'env/files/keeper-client.py'
SECRET = 'synthetic-password-value'
TOKEN = 'synthetic-field-value'
CONTENT = b'synthetic-attachment\x00\xff'

spec = importlib.util.spec_from_file_location('keeper_client', SOURCE)
keeper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keeper)


class TypedField:
    def __init__(self, entry=None):
        entry = entry or {}
        self.type = entry.get('type', '')
        self.label = entry.get('label', '')
        self.value = list(entry.get('value', []))


class TypedRecord:
    def __init__(self, uid='', title='', fields=(), custom=()):
        self.record_uid, self.title, self.type_name, self.notes = uid, title, 'login', ''
        self.fields, self.custom = list(fields), list(custom)

    @property
    def record_type(self):
        return self.type_name

    def load_record_data(self, data):
        self.type_name, self.title = data['type'], data['title']
        self.fields = [TypedField(x) for x in data.get('fields', [])]
        self.custom = [TypedField(x) for x in data.get('custom', [])]

    def get_typed_field(self, field_type):
        return next((field for field in self.fields + self.custom if field.type == field_type), None)


class FileRecord:
    def __init__(self, name):
        self.name = name


class FakeKeeper:
    """Vault with three typed records, one file record and one legacy (v2) record."""
    def __init__(self):
        self.calls = []
        self.fail_write = False
        login = TypedRecord('in1', 'synthetic-item', [
            TypedField({'type': 'login', 'value': ['synthetic-user']}),
            TypedField({'type': 'password', 'value': [SECRET]}),
            TypedField({'type': 'fileRef', 'value': ['file1']})],
            [TypedField({'type': 'text', 'label': 'api', 'value': [TOKEN]})])
        self.objects = {'in1': login, 'out1': TypedRecord('out1', 'synthetic-other'),
                        'nested1': TypedRecord('nested1', 'synthetic-nested'), 'file1': FileRecord('fixture.bin'),
                        'legacy1': SimpleNamespace(record_uid='legacy1', title='synthetic-legacy')}
        self.params = SimpleNamespace(record_cache=dict.fromkeys(self.objects))
        fake = self

        class Vault:
            class KeeperRecord:
                @staticmethod
                def load(_, uid):
                    return fake.objects.get(uid)

        Vault.TypedRecord, Vault.FileRecord, Vault.TypedField = TypedRecord, FileRecord, TypedField

        def write(name, action):
            def call(*args, **kwargs):
                fake.calls.append((name, args, kwargs))
                if fake.fail_write:
                    raise RuntimeError('synthetic transport failure')
                return action(*args, **kwargs)
            return call

        def add(_, record, folder_uid):
            fake.objects[record.record_uid] = record
            fake.params.record_cache[record.record_uid] = None

        def upload(_, record, tasks):
            fake.objects['file2'] = FileRecord(tasks[0].name)
            record.get_typed_field('fileRef').value.append('file2')

        def remove(params, records, force):
            del fake.params.record_cache[records[0]]

        class Download:
            def download_to_stream(self, _, stream):
                stream.write(CONTENT)

        self.kc = SimpleNamespace(
            vault=Vault, utils=SimpleNamespace(generate_uid=lambda: 'new1'),
            api=SimpleNamespace(sync_down=lambda _: fake.calls.append(('sync', (), {}))),
            remove_record=SimpleNamespace(execute=lambda params, **kwargs: write('remove', remove)(params, **kwargs)),
            record_management=SimpleNamespace(add_record_to_folder=write('add', add),
                                              update_record=write('update', lambda *_: None)),
            attachment=SimpleNamespace(
                BytesUploadTask=lambda data: SimpleNamespace(data=data, name='', title='', mime_type=''),
                upload_attachments=write('upload', upload),
                prepare_attachment_download=lambda _, uid: [Download()] if uid == 'file1' else []))


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeKeeper()
        patcher = patch.multiple(keeper, kc=self.fake.kc)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.session = keeper.Session(self.fake.params)


class SidecarTests(Base):
    def handle(self, method, path, query=None, body=b''):
        return self.session.handle(method, path, query or {}, body)

    def test_list_and_reads_cover_every_typed_record(self):
        listed = self.handle('GET', '/list')
        self.assertEqual([record['uid'] for record in listed], ['in1', 'nested1', 'out1'])
        self.assertEqual(listed[0], {'uid': 'in1', 'title': 'synthetic-item', 'type': 'login', 'password': True,
                                     'fields': ['api', 'login', 'password'], 'attachments': ['fixture.bin']})
        self.assertNotIn(SECRET, json.dumps(listed))
        self.assertEqual(self.handle('GET', '/record', {'title': 'synthetic-item'})['uid'], 'in1')
        self.assertEqual(self.handle('GET', '/record', {'uid': 'nested1'})['title'], 'synthetic-nested')
        self.assertEqual(self.handle('GET', '/attachment', {'uid': 'in1', 'name': 'fixture.bin'}), CONTENT)
        for query in ({'uid': 'file1'}, {'uid': 'legacy1'}, {'uid': 'missing'}, {'uid': 'in1', 'title': 'synthetic-item'}, {}):
            with self.subTest(query=query), self.assertRaises(keeper.Refused):
                self.handle('GET', '/record', query)

    def test_duplicate_titles_must_be_selected_by_uid(self):
        self.fake.objects['out1'].title = 'synthetic-item'
        with self.assertRaises(keeper.Refused):
            self.handle('GET', '/record', {'title': 'synthetic-item'})
        self.assertEqual(self.handle('GET', '/record', {'uid': 'out1'})['uid'], 'out1')

    def test_create_goes_into_the_vault_root_and_validates_documents(self):
        result = self.handle('POST', '/create', body=json.dumps({'type': 'login', 'title': 'synthetic-new',
            'fields': [{'type': 'password', 'value': ['synthetic']}]}).encode())
        self.assertEqual(result, {'status': 'created', 'uid': 'new1'})
        self.assertIsNone(next(call[1] for call in self.fake.calls if call[0] == 'add')[2])
        for document in ({'title': 'x'}, {'type': 'login', 'title': 'x', 'folder': 'F2'},
                         *({'type': 'login', 'title': 'x', key: [{'type': link, 'value': ['out1']}]}
                           for key in ('fields', 'custom') for link in keeper.LINK_TYPES),
                         {'type': 'login', 'title': 'x', 'fields': [{'type': 'login', 'value': 'not-a-list'}]},
                         *({'type': 'login', 'title': 'x', 'fields': [{'type': spoof, 'value': ['out1']}]}
                           for spoof in (' fileRef', 'recordRef\t', 'FILEREF', 'cardRef\n', '', 'a b')),
                         {'type': 'login', 'title': ' '}, []):
            with self.subTest(document=document), self.assertRaises(keeper.Refused):
                self.handle('POST', '/create', body=json.dumps(document).encode())

    def test_update_replaces_fields_but_keeps_attachments_and_links(self):
        record = self.fake.objects['in1']
        record.custom.append(TypedField({'type': 'recordRef', 'value': ['in1']}))
        self.handle('POST', '/update', {'uid': 'in1'}, json.dumps({'fields': [{'type': 'login', 'value': ['u2']}]}).encode())
        self.assertEqual([(f.type, f.value) for f in record.fields], [('login', ['u2']), ('fileRef', ['file1'])])
        self.assertEqual(record.custom[0].value, [TOKEN])
        self.handle('POST', '/update', {'uid': 'in1'}, json.dumps({'custom': []}).encode())
        self.assertEqual([(f.type, f.value) for f in record.custom], [('recordRef', ['in1'])])

    def test_attach_replaces_same_name_in_one_update(self):
        self.handle('POST', '/attach', {'uid': 'in1', 'name': 'fixture.bin'}, b'new')
        self.assertEqual(self.fake.objects['in1'].get_typed_field('fileRef').value, ['file2'])
        self.assertEqual([call[0] for call in self.fake.calls if call[0] != 'sync'], ['upload', 'update'])
        for name in ('', '../x', 'a"b', 'x' * 256):
            with self.subTest(name=name), self.assertRaises(keeper.Refused):
                self.handle('POST', '/attach', {'uid': 'in1', 'name': name}, b'x')

    def test_attach_replaces_a_file_linked_from_custom(self):
        record = self.fake.objects['in1']
        record.fields = [field for field in record.fields if field.type != 'fileRef']
        record.custom.append(TypedField({'type': 'fileRef', 'value': ['file1']}))
        self.handle('POST', '/attach', {'uid': 'in1', 'name': 'fixture.bin'}, b'new')
        self.assertEqual(keeper.record_files(self.fake.params, record), [('file2', 'fixture.bin')])

    def test_trash_uses_commanders_rm_and_confirms_removal(self):
        self.assertEqual(self.handle('POST', '/trash', {'uid': 'out1'}), {'status': 'trashed', 'uid': 'out1'})
        self.assertEqual(next(call[2] for call in self.fake.calls if call[0] == 'remove'), {'records': ['out1'], 'force': True})
        self.assertNotIn('out1', self.fake.params.record_cache)

    def test_failure_after_a_write_starts_is_reported_uncertain(self):
        self.fake.fail_write = True
        with self.assertRaises(keeper.Uncertain) as caught:
            self.handle('POST', '/update', {'uid': 'in1'}, b'{"notes": "x"}')
        self.assertEqual(caught.exception.uid, 'in1')

    def test_failed_sync_marks_session_dead(self):
        def broken(_):
            raise RuntimeError('session expired')
        self.fake.kc.api.sync_down = broken
        with self.assertRaises(RuntimeError):
            self.handle('GET', '/list')
        self.assertTrue(self.session.dead)

    def test_login_waits_for_the_bootstrap_lock_and_rereads_config(self):
        with tempfile.TemporaryDirectory() as directory:
            loads = []

            def load(path):
                loads.append(json.loads(Path(path).read_text())['clone_code'])
                return SimpleNamespace(user='synthetic@example.invalid', clone_code=loads[-1], session_token=None,
                                       rest_context=SimpleNamespace(fail_on_throttle=False))

            def login(params, login_ui):
                params.session_token = 'synthetic-session'
            self.fake.kc.get_params_from_config = load
            self.fake.kc.api.login = login
            config = Path(directory) / 'config.json'
            with patch.multiple(keeper, CONFIG=config, LOGIN_LOCK=Path(directory) / 'login.lock'):
                self.assertIsNone(keeper.login())
                config.write_text('{"clone_code": "first"}')
                with keeper.login_lock(blocking=True):
                    self.assertIsNone(keeper.login())
                self.assertEqual(loads, [])
                params = keeper.login()
                self.assertTrue(params.batch_mode and params.rest_context.fail_on_throttle)
                config.write_text('{"clone_code": "rotated"}')
                keeper.login()
                self.assertEqual(loads, ['first', 'rotated'])

    def test_cancelled_sso_login_waits_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text('{}')
            self.fake.kc.get_params_from_config = lambda _: SimpleNamespace(user='u', clone_code='c', session_token=None)

            def sso(*_, **__):
                raise KeyboardInterrupt  # what Commander raises when NoPrompt cancels an SSO step
            self.fake.kc.api.login = sso
            with patch.multiple(keeper, CONFIG=config, LOGIN_LOCK=Path(directory) / 'login.lock'):
                self.assertIsNone(keeper.login())

    def test_persistent_login_never_prompts(self):
        counter = []
        step = SimpleNamespace(cancel=lambda: counter.append(1))
        prompt = keeper.NoPrompt()
        for method in ('on_device_approval', 'on_two_factor', 'on_password', 'on_sso_redirect', 'on_sso_data_key'):
            getattr(prompt, method)(step)
        self.assertEqual(len(counter), 5)


class ClientTests(Base):
    """Runs the real client file as a subprocess against the sidecar handler on a free port."""
    def setUp(self):
        super().setUp()
        self.server = HTTPServer(('127.0.0.1', 0), keeper.Handler)
        self.server.session = self.session
        Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        client = Path(self.tmp.name) / 'keeper'
        port = self.server.server_address[1]
        client.write_text(SOURCE.read_text().replace("ADDRESS = ('127.0.0.1', 8088)", f"ADDRESS = ('127.0.0.1', {port})"))
        self.client = [sys.executable, str(client)]

    def keeper(self, *args, env=None):
        return subprocess.run([*self.client, *args], capture_output=True, text=True,
                              env={**os.environ, 'KEEPER_LEAK': 'parent', **(env or {})}, timeout=30)

    def test_run_injects_values_only_into_the_child(self):
        fixture = Path(self.tmp.name) / 'expected.bin'
        fixture.write_bytes(CONTENT)
        checked = Path(self.tmp.name) / 'checked'
        script = (f'test "$KEEPER_PW" = "{SECRET}" && test "$KEEPER_USER" = synthetic-user && test "$KEEPER_API" = "{TOKEN}" '
                  f'&& test -z "${{KEEPER_LEAK:-}}" && cmp -s "$KEEPER_FILE" "{fixture}" && echo "$KEEPER_PW" && touch "{checked}"')
        result = self.keeper('run', '--title', 'synthetic-item', '--password-env', 'KEEPER_PW', '--login-env', 'KEEPER_USER',
                             '--field', 'api=KEEPER_API', '--attachment', 'fixture.bin', '--attachment-env', 'KEEPER_FILE',
                             '--', 'sh', '-c', script)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, 'keeper: command finished; exit code 0\n', ''))
        self.assertTrue(checked.exists())
        self.assertEqual(self.keeper('run', '--uid', 'in1', '--password-env', 'KEEPER_PW', '--', 'sh', '-c', 'exit 7').returncode, 7)

    def test_requests_are_refused_at_once_while_not_logged_in(self):
        self.server.session = None
        started = time.monotonic()
        result = self.keeper('list')
        self.assertEqual(result.returncode, 1)
        self.assertIn('the keeper sidecar is not logged in', result.stderr)
        self.assertLess(time.monotonic() - started, 5)

    def test_an_idle_connection_does_not_block_other_requests(self):
        with patch.object(keeper.Handler, 'timeout', 1), socket.create_connection(self.server.server_address):
            started = time.monotonic()
            self.assertEqual(self.keeper('list').returncode, 0)
            self.assertLess(time.monotonic() - started, 5)

    def test_run_refuses_bad_targets_and_missing_values(self):
        for args in (['--uid', 'missing', '--password-env', 'KEEPER_PW'], ['--uid', 'out1', '--password-env', 'KEEPER_PW'], ['--uid', 'in1', '--password-env', 'PW'],
                     ['--uid', 'in1', '--field', 'missing=KEEPER_X'], ['--uid', 'in1']):
            with self.subTest(args=args):
                result = self.keeper('run', *args, '--', 'true')
                self.assertEqual(result.returncode, 1)
                self.assertNotIn(SECRET, result.stdout + result.stderr)

    def test_list_and_writes_print_no_values(self):
        listed = self.keeper('list', 'ITEM')
        self.assertEqual([r['uid'] for r in json.loads(listed.stdout)], ['in1'])
        self.assertNotIn(SECRET, listed.stdout)
        created = self.keeper('create', '--input-env', 'KEEPER_DOC',
                              env={'KEEPER_DOC': json.dumps({'type': 'login', 'title': 'synthetic-new'})})
        self.assertEqual(json.loads(created.stdout), {'status': 'created', 'uid': 'new1'})
        attachment = Path(self.tmp.name) / 'cred.bin'
        attachment.write_bytes(b'x')
        self.assertEqual(json.loads(self.keeper('attach', '--uid', 'in1', '--file', str(attachment)).stdout)['status'], 'attached')
        self.fake.fail_write = True
        result = self.keeper('trash', '--uid', 'in1')
        self.assertEqual(result.returncode, 2)
        self.assertIn('record in1 may have changed', result.stderr)


class ShutdownTests(unittest.TestCase):
    DRIVER = f'''
import importlib.util, json, sys, time
from types import SimpleNamespace
spec = importlib.util.spec_from_file_location('k', {str(SOURCE)!r}); k = importlib.util.module_from_spec(spec); spec.loader.exec_module(k)
k.ADDRESS = ('127.0.0.1', int(sys.argv[1]))
k.STOP_DEADLINE_SECONDS = float(sys.argv[2])
k.commander = lambda: SimpleNamespace(api=SimpleNamespace(sync_down=lambda _: None))
k.login = lambda: SimpleNamespace()
class Slow(k.Session):
    def handle(self, *_):
        print('handling', flush=True); time.sleep(float(sys.argv[3])); return {{}}
k.Session = Slow
sys.exit(k.main(['serve']))
'''

    def serve(self, deadline, request_seconds):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        server = subprocess.Popen([sys.executable, '-c', self.DRIVER, str(port), str(deadline), str(request_seconds)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  env=os.environ)
        self.addCleanup(server.kill)
        self.assertEqual(server.stdout.readline(), 'keeper: ready\n')
        request = subprocess.Popen([sys.executable, '-c', f'import urllib.request; urllib.request.urlopen("http://127.0.0.1:{port}/list")'],
                                   stderr=subprocess.DEVNULL)
        self.addCleanup(request.kill)
        self.assertEqual(server.stdout.readline(), 'handling\n')
        server.send_signal(signal.SIGTERM)
        return server, time.monotonic()

    def test_sigterm_during_a_slow_request_stops_serve_promptly(self):
        server, started = self.serve(deadline=45, request_seconds=3)
        self.assertEqual(server.wait(timeout=10), 0)
        self.assertLess(time.monotonic() - started, 6)  # finishes the in-flight request, then stops
        self.assertEqual(server.stdout.read(), 'keeper: stopped\n')

    def test_a_blocked_commander_call_is_cut_off_at_the_stop_deadline(self):
        server, started = self.serve(deadline=1, request_seconds=60)
        self.assertEqual(server.wait(timeout=10), 143)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(server.stderr.read(), 'keeper: stop deadline reached\n')


if __name__ == '__main__':
    unittest.main()
