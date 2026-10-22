#!/usr/bin/env python3
import contextlib
import copy
from email.parser import BytesParser
from email.policy import default
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
from threading import Thread
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch
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
        self.writes = []
        self.upload_error = False
        self.corrupt_upload = False
        self.bad_upload_response = False
        self.omit_upload_size = False
        self.confirmation_failure = None
        self.create_changes = {}
        self.create_delay = 0
        self.omit_read_data = False
        self.read_error = None
        self.read_error_html = False
        self.get_item = lambda item: item
        self.uploads = {'syntheticattachment': CONTENT}
        self.next_attachment = 0
        self.cache_lag_reads = 0
        self.attachment_delete_lag_reads = 0
        self.cache_reads = 0
        self.cached_item = None
        self.list_override = None
        self.list_includes_trashed = False
        self.stale_list_after_delete = False
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def cached(self):
                if test.cache_reads:
                    test.cache_reads -= 1
                    return copy.deepcopy(test.cached_item)
                return copy.deepcopy(test.item)

            def defer_cache(self):
                test.cached_item = copy.deepcopy(test.item)
                test.cache_reads = test.cache_lag_reads

            def do_GET(self):
                test.paths.append(self.path)
                if test.writes:
                    if test.confirmation_failure == 'http500':
                        self.reply(status=500, message=VALUE)
                        return
                    if test.confirmation_failure == 'connection':
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    if test.confirmation_failure == 'timeout':
                        time.sleep(0.3)
                        self.close_connection = True
                        return
                    if test.confirmation_failure == 'unsuccessful':
                        self.reply(success=False, message=VALUE)
                        return
                if test.read_error is not None:
                    if test.read_error_html:
                        self.send_error(test.read_error[0])
                        return
                    self.reply(status=test.read_error[0], message=test.read_error[1])
                    return
                parsed = urlsplit(self.path)
                item = self.cached()
                if parsed.path.startswith('/object/attachment/'):
                    if test.cache_lag_reads and not any(entry['id'] == parsed.path.rsplit('/', 1)[1]
                            for entry in (item or {}).get('attachments', [])):
                        self.reply(status=400, message='No attachments available for this item.')
                        return
                    if parse_qs(parsed.query) != {'itemid': [ITEM]}:
                        self.send_error(400)
                        return
                    data = test.uploads[parsed.path.rsplit('/', 1)[1]]
                    content_type = 'application/octet-stream'
                else:
                    content_type = 'application/json'
                    if parsed.path == '/list/object/items':
                        query = parse_qs(parsed.query)
                        if test.list_override is not None:
                            item = copy.deepcopy(test.list_override)
                        items = ([item] if item is not None else []) + test.extra_items
                        trashed = query.get('trash') == ['true']
                        if not test.list_includes_trashed:
                            items = [item for item in items if (item.get('deletedDate') is not None) == trashed]
                        search = query.get('search', [''])[0]
                        data = {'object': 'list', 'data': [item for item in items if search in item.get('name', '')]}
                    elif parsed.path == '/object/item/' + ITEM:
                        if item is None:
                            self.reply(status=400, message='Not found.')
                            return
                        data = test.get_item(item)
                    elif parsed.path == '/object/item/' + FOREIGN:
                        data = {**test.item, 'id': FOREIGN, 'organizationId': FOREIGN}
                    else:
                        self.send_error(400)
                        return
                    data = json.dumps({'success': True, **({} if test.omit_read_data else {'data': data})}).encode()
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def reply(self, data=None, status=200, message=None, success=None):
                body = json.dumps({'success': status == 200 if success is None else success, **({} if data is None else {'data': data}),
                    **({} if message is None else {'message': message})}).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                test.writes.append(('POST', self.path, body))
                if self.path == '/sync?force=true':
                    test.list_override = None
                    self.reply()
                elif self.path == '/object/item':
                    self.defer_cache()
                    test.item = {**json.loads(body), 'id': ITEM, **test.create_changes}
                    time.sleep(test.create_delay)
                    self.reply(test.item)
                elif self.path == '/attachment?itemid=' + ITEM:
                    if test.upload_error:
                        self.reply({'message': VALUE}, 400)
                        return
                    message = BytesParser(policy=default).parsebytes(
                        b'Content-Type: ' + self.headers['Content-Type'].encode() + b'\r\n\r\n' + body)
                    part, = message.iter_parts()
                    test.next_attachment += 1
                    attachment_id = 'uploaded' + str(test.next_attachment)
                    test.uploads[attachment_id] = b'corrupt' if test.corrupt_upload else part.get_payload(decode=True)
                    self.defer_cache()
                    test.item.setdefault('attachments', []).append({'id': attachment_id, 'fileName': part.get_filename(),
                        'size': str(len(part.get_payload(decode=True)))})
                    response = copy.deepcopy(test.item)
                    if test.omit_upload_size:
                        del response['attachments'][-1]['size']
                    self.reply({**response, 'id': FOREIGN} if test.bad_upload_response else response)
                else:
                    self.reply(status=400)

            def do_PUT(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                test.writes.append(('PUT', self.path, body))
                if self.path != '/object/item/' + ITEM:
                    self.reply(status=400)
                    return
                self.defer_cache()
                test.item = json.loads(body)
                self.reply(test.item)

            def do_DELETE(self):
                test.writes.append(('DELETE', self.path, None))
                parsed = urlsplit(self.path)
                if test.cache_lag_reads and self.cached() is None:
                    self.reply(status=400, message='Not found.')
                    return
                self.defer_cache()
                if parsed.path == '/object/item/' + ITEM:
                    if parse_qs(parsed.query) == {'permanent': ['true']}:
                        if test.stale_list_after_delete:
                            test.list_override = copy.deepcopy(test.item)
                        test.item = None
                    else:
                        test.item['deletedDate'] = '2026-01-01T00:00:00Z'
                elif parsed.path.startswith('/object/attachment/') and parse_qs(parsed.query) == {'itemid': [ITEM]}:
                    test.cache_reads = max(test.cache_reads, test.attachment_delete_lag_reads)
                    attachment_id = parsed.path.rsplit('/', 1)[1]
                    test.item['attachments'] = [entry for entry in test.item['attachments'] if entry['id'] != attachment_id]
                    del test.uploads[attachment_id]
                else:
                    self.reply(status=400)
                    return
                self.reply()

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

    def test_list_returns_only_scoped_active_metadata_for_both_runtimes(self):
        self.item.update(type=1, notes='synthetic-notes')
        self.item['login'].update(uris=[{'uri': 'https://synthetic-uri.invalid'}], totp='synthetic-totp')
        self.item['fields'].append({'name': 'account', 'value': 'synthetic-account', 'type': 0})
        self.extra_items = [
            {**self.item, 'name': 'foreign-org', 'organizationId': FOREIGN},
            {**self.item, 'name': 'foreign-collection', 'collectionIds': [FOREIGN]},
            {**self.item, 'name': 'personal', 'organizationId': None},
            {**self.item, 'name': 'trashed', 'deletedDate': '2026-01-01T00:00:00Z'},
            {**self.item, 'name': 'Alpha', 'type': 2, 'login': None, 'fields': None, 'attachments': None},
        ]
        self.list_includes_trashed = True
        for variant in ('homelab', 'work'):
            with self.subTest(variant=variant):
                self.env['T3_RUNTIME_VARIANT'] = variant
                (self.home / '.config/t3-runtime/environment').write_text(variant + '\n')
                self.scope.chmod(0o600)
                self.scope.write_text(json.dumps({'environment': variant, 'organizationId': ORG,
                                                 'collectionId': COLLECTION}))
                self.scope.chmod(0o444)
                result = subprocess.run(self.prefix + ['list'], env=self.env, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), [
                    {'name': 'Alpha', 'type': 2, 'password': False, 'fields': [], 'attachments': []},
                    {'name': 'synthetic-item', 'type': 1, 'password': True,
                     'fields': ['account', 'api'], 'attachments': ['fixture.bin']},
                ])
                self.assertEqual(result.stderr, b'')
                self.assert_quiet(result)
                for private in ('synthetic-user', 'synthetic-notes', 'synthetic-uri', 'synthetic-totp',
                                'synthetic-account'):
                    self.assertNotIn(private.encode(), result.stdout + result.stderr)
                query = parse_qs(urlsplit(self.paths[-1]).query)
                self.assertEqual(query, {'organizationid': [ORG], 'collectionid': [COLLECTION]})

    def test_list_filter_is_case_insensitive_and_matches_only_item_names(self):
        self.item['type'] = 1
        self.extra_items = [{**self.item, 'name': 'zeta'}, {**self.item, 'name': 'alpha'}]
        for term, names in (('ThEtIc', ['synthetic-item']), ('api', []), ('missing', []),
                            ('', ['alpha', 'synthetic-item', 'zeta'])):
            with self.subTest(term=term):
                result = subprocess.run(self.prefix + ['list', term], env=self.env, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([item['name'] for item in json.loads(result.stdout)], names)
                self.assert_quiet(result)

    def test_list_refuses_invalid_arguments_scope_and_api_errors_without_metadata(self):
        for arguments in (['list', 'one', 'two'], ['list', '--unknown']):
            result = subprocess.run(self.prefix + arguments, env=self.env, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, b'')
            self.assertRegex(result.stderr, rb'^vault: request or startup refused \(\w+ at main:\d+ > list_items:\d+( > [\w<>]+:\d+)*\)\n$')
        self.assertEqual(self.paths, [])
        self.read_error = (500, VALUE)
        result = subprocess.run(self.prefix + ['list'], env=self.env, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertRegex(result.stderr, rb'^vault: request or startup refused \(HTTPError at main:\d+ > list_items:\d+ > fetch:\d+\)\n$')
        result = subprocess.run(self.prefix + ['bogus'], env=self.env, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stderr, rb'^vault: request or startup refused \(Refused at main:\d+\)\n$')
        self.assert_quiet(result)
        self.paths.clear()
        self.scope.unlink()
        result = subprocess.run(self.prefix + ['list'], env=self.env, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(self.paths, [])
        self.assert_quiet(result)

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

    def test_name_selection_rechecks_item_instead_of_using_stale_decrypted_list(self):
        self.list_override = copy.deepcopy(self.item)
        self.item['login']['password'] = FIELD
        result = self.command(['--name', 'synthetic-item', '--password-env', 'VAULT_PASSWORD'],
            f'import os; assert os.environ["VAULT_PASSWORD"] == {FIELD!r}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.item['deletedDate'] = '2026-01-01T00:00:00Z'
        result = self.command(['--name', 'synthetic-item', '--password-env', 'VAULT_PASSWORD'], 'pass')
        self.assertEqual(result.returncode, 1)
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
        for item_id in (FOREIGN, '55555555-5555-4555-8555-555555555555'):
            result = self.command(['--item', item_id, '--password-env', 'VAULT_PASSWORD'],
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


    def write_command(self, command, options=(), document=None):
        if document is not None:
            self.env['VAULT_DOCUMENT'] = json.dumps(document)
            options = [*options, '--input-env', 'VAULT_DOCUMENT']
        return subprocess.run(self.prefix + [command, *options], env=self.env, capture_output=True)

    def shorten_cache_wait(self):
        self.prefix[2] = self.prefix[2].replace('m.ADDRESS=',
            'm.CACHE_WAIT_SECONDS=0.05; m.CACHE_POLL_SECONDS=0.01; m.ADDRESS=')

    def test_write_confirmation_ignores_unrelated_get_metadata(self):
        self.shorten_cache_wait()

        def with_metadata(item):
            result = copy.deepcopy(item)
            result.update(revisionDate='2026-01-01T00:00:00.000000Z', serverMetadata='synthetic-metadata')
            result.setdefault('login', {})['serverMetadata'] = 'synthetic-login-metadata'
            for entry in result.get('attachments', []) + result.get('fields', []):
                entry['serverMetadata'] = 'synthetic-entry-metadata'
            return result

        self.get_item = with_metadata
        original = copy.deepcopy(self.item)
        path = self.home / 'fixture.bin'
        path.write_bytes(CONTENT)
        for command, options, document in (
                ('create', [], {'type': 1, 'name': 'synthetic-created', 'login': {'password': VALUE}}),
                ('update', ['--item', ITEM], {'login': {'password': FIELD},
                    'fields': [{'name': 'api', 'value': VALUE, 'type': 1}]}),
                ('attach', ['--item', ITEM, '--file', str(path)], None),
                ('trash', ['--item', ITEM], None)):
            with self.subTest(command=command):
                self.item = None if command == 'create' else copy.deepcopy(original)
                self.cache_reads = 0
                self.next_attachment = 0
                self.writes.clear()
                result = self.write_command(command, options, document)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['itemId'], ITEM)
                self.assertEqual(len(self.writes), 2 if command == 'attach' else 1)
                self.assert_quiet(result)

    def test_trash_confirmation_rejects_wrong_id_or_scope(self):
        original = copy.deepcopy(self.item)
        for change in ({'id': FOREIGN}, {'organizationId': FOREIGN}, {'collectionIds': [FOREIGN]}):
            with self.subTest(change=change):
                self.item = copy.deepcopy(original)
                self.writes.clear()
                self.get_item = lambda item: {**item, **change} if self.writes else item
                result = self.write_command('trash', ['--item', ITEM])
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(self.writes), 1)
                self.assert_quiet(result)

    def test_attachment_confirmation_requires_uploaded_identity_name_and_size(self):
        self.shorten_cache_wait()
        original = copy.deepcopy(self.item)
        path = self.home / 'fixture.bin'
        path.write_bytes(CONTENT)
        for change in ({'id': 'different-id'}, {'fileName': 'different.bin'}, {'size': '999999'}):
            with self.subTest(change=change):
                self.item = copy.deepcopy(original)
                self.next_attachment = 0
                self.writes.clear()

                def changed_attachment(item):
                    return {**item, 'attachments': [
                        {**entry, **change} if entry['id'] == 'uploaded1' else entry
                        for entry in item.get('attachments', [])]}

                self.get_item = changed_attachment
                result = self.write_command('attach', ['--item', ITEM, '--file', str(path)])
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual([method for method, _, _ in self.writes], ['POST'])
                self.assert_quiet(result)

    def test_cli_reports_all_acknowledged_confirmation_failures_without_replay(self):
        self.prefix[2] = self.prefix[2].replace('m.ADDRESS=',
            'm.fetch=__import__("functools").partial(m.fetch, timeout=0.1); m.ADDRESS=')
        original = copy.deepcopy(self.item)
        path = self.home / 'fixture.bin'
        path.write_bytes(CONTENT)
        for command, options, document in (
                ('create', [], {'type': 1, 'name': 'synthetic-created', 'login': {'password': VALUE}}),
                ('update', ['--item', ITEM], {'login': {'password': FIELD}}),
                ('attach', ['--item', ITEM, '--file', str(path)], None),
                ('trash', ['--item', ITEM], None)):
            for failure in ('http500', 'connection', 'timeout', 'unsuccessful', 'scope', 'id'):
                with self.subTest(command=command, failure=failure):
                    self.item = None if command == 'create' else copy.deepcopy(original)
                    self.next_attachment = 0
                    self.writes.clear()
                    self.confirmation_failure = failure
                    change = {'organizationId': FOREIGN} if failure == 'scope' else {'id': FOREIGN}
                    self.get_item = lambda item: {**item, **change} if self.writes and failure in ('scope', 'id') else item
                    result = self.write_command(command, options, document)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, b'')
                    self.assertIn(('item ' + ITEM + ' may exist').encode(), result.stderr)
                    self.assertIn(b'inspect before retrying', result.stderr)
                    self.assertNotIn(b'request or startup refused', result.stderr)
                    if command == 'attach':
                        self.assertIn(b'attachment uploaded1 may exist', result.stderr)
                    self.assertEqual(len(self.writes), 1)
                    self.assertIsNotNone(self.item)
                    self.assert_quiet(result)

    def test_cli_reports_known_attachment_when_upload_response_omits_size(self):
        self.omit_upload_size = True
        path = self.home / 'fixture.bin'
        path.write_bytes(CONTENT)
        result = self.write_command('attach', ['--item', ITEM, '--file', str(path)])
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, b'')
        self.assertIn(('item ' + ITEM + ' may exist').encode(), result.stderr)
        self.assertIn(b'attachment uploaded1 may exist', result.stderr)
        self.assertIn(b'inspect before retrying', result.stderr)
        self.assertEqual([method for method, _, _ in self.writes], ['POST'])
        self.assertEqual(len(self.item['attachments']), 2)
        self.assert_quiet(result)

    def test_cli_keeps_confirmation_cancellation_distinct(self):
        self.prefix[2] = self.prefix[2].replace('sys.exit(m.main(sys.argv[4:]))',
            '\ndef cancelled(*args, **kwargs):\n raise m.Cancelled\n'
            'm.wait_for_cache=cancelled\nsys.exit(m.main(sys.argv[4:]))')
        original = copy.deepcopy(self.item)
        path = self.home / 'fixture.bin'
        path.write_bytes(CONTENT)
        for command, options, document in (
                ('create', [], {'type': 1, 'name': 'synthetic-created'}),
                ('update', ['--item', ITEM], {'notes': FIELD}),
                ('attach', ['--item', ITEM, '--file', str(path)], None),
                ('trash', ['--item', ITEM], None)):
            with self.subTest(command=command):
                self.item = None if command == 'create' else copy.deepcopy(original)
                self.next_attachment = 0
                self.writes.clear()
                result = self.write_command(command, options, document)
                self.assertEqual(result.returncode, 130, result.stderr)
                self.assertNotIn(b'write sent', result.stdout + result.stderr)
                self.assertEqual(len(self.writes), 1)
                self.assert_quiet(result)

    def test_cli_reports_uncertain_cache_confirmation_for_each_write_without_replay(self):
        self.shorten_cache_wait()
        original = copy.deepcopy(self.item)
        path = self.home / 'fixture.bin'
        path.write_bytes(CONTENT)
        for command, options, document in (
                ('create', [], {'type': 1, 'name': 'synthetic-created', 'login': {'password': VALUE}}),
                ('update', ['--item', ITEM], {'login': {'password': FIELD}}),
                ('attach', ['--item', ITEM, '--file', str(path)], None),
                ('trash', ['--item', ITEM], None)):
            with self.subTest(command=command):
                self.item = None if command == 'create' else copy.deepcopy(original)
                self.cache_lag_reads = 1000000
                self.cache_reads = 0
                self.next_attachment = 0
                self.writes.clear()
                result = self.write_command(command, options, document)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, b'')
                self.assertIn(b'write sent, local cache not confirmed', result.stderr)
                self.assertIn(('item ' + ITEM + ' may exist').encode(), result.stderr)
                self.assertNotIn(b'request or startup refused', result.stderr)
                if command == 'attach':
                    self.assertIn(b'attachment uploaded1 may exist', result.stderr)
                    self.assertEqual(len(self.item['attachments']), 2)
                self.assertEqual(len(self.writes), 1)
                self.assertIsNotNone(self.item)
                self.assert_quiet(result)

    def test_cli_reports_attachment_id_when_old_delete_cache_does_not_confirm(self):
        self.shorten_cache_wait()
        self.attachment_delete_lag_reads = 1000000
        path = self.home / 'fixture.bin'
        path.write_bytes(CONTENT)
        result = self.write_command('attach', ['--item', ITEM, '--file', str(path)])
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(('item ' + ITEM + ' may exist').encode(), result.stderr)
        self.assertIn(b'attachment uploaded1 may exist', result.stderr)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(self.item['attachments'], [{'id': 'uploaded1', 'fileName': 'fixture.bin', 'size': str(len(CONTENT))}])
        self.assertEqual([method for method, _, _ in self.writes], ['POST', 'DELETE'])
        self.assert_quiet(result)

    def test_create_forces_scope_from_env_or_file_without_printing_values(self):
        document = {'type': 1, 'name': VALUE, 'login': {'password': VALUE},
                    'fields': [{'name': 'api', 'value': FIELD, 'type': 1}]}
        for variant in ('homelab', 'work'):
            self.env['T3_RUNTIME_VARIANT'] = variant
            (self.home / '.config/t3-runtime/environment').write_text(variant + '\n')
            self.scope.chmod(0o600)
            self.scope.write_text(json.dumps({'environment': variant, 'organizationId': ORG, 'collectionId': COLLECTION}))
            self.scope.chmod(0o444)
            for use_file in (False, True):
                if use_file:
                    path = self.home / 'document.json'
                    path.write_text(json.dumps(document))
                    result = self.write_command('create', ['--input-file', str(path)])
                else:
                    result = self.write_command('create', document=document)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {'status': 'created', 'itemId': ITEM})
                self.assertEqual(self.item['organizationId'], ORG)
                self.assertEqual(self.item['collectionIds'], [COLLECTION])
                self.assert_quiet(result)

    def test_create_rejects_ownership_overrides_and_invalid_input_before_request(self):
        base = {'name': 'synthetic', 'type': 1}
        for key, value in (('organizationId', FOREIGN), ('collectionIds', [FOREIGN]), ('id', FOREIGN),
                           ('deletedDate', None), ('folderId', FOREIGN)):
            result = self.write_command('create', document={**base, key: value})
            self.assertEqual(result.returncode, 1)
            self.assert_quiet(result)
        for document in ([], {}, {'name': '', 'type': 1}, {'name': 'synthetic', 'type': True}):
            self.assertEqual(self.write_command('create', document=document).returncode, 1)
        self.assertEqual(self.write_command('create', ['--input-env', 'BW_PASSWORD']).returncode, 1)
        self.assertEqual(self.write_command('create', ['--input-file', str(self.home)]).returncode, 1)
        self.assertEqual(self.writes, [])

    def test_update_preserves_scope_and_unmodified_fields(self):
        result = self.write_command('update', ['--item', ITEM], {'login': {'password': FIELD}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'status': 'updated', 'itemId': ITEM})
        self.assertEqual(self.item['name'], 'synthetic-item')
        self.assertEqual(self.item['login'], {'password': FIELD})
        self.assertEqual(self.item['organizationId'], ORG)
        self.assertEqual(self.item['collectionIds'], [COLLECTION])
        self.assertEqual(self.item['fields'][0]['value'], FIELD)
        self.assert_quiet(result)
        for key, value in (('organizationId', FOREIGN), ('collectionIds', [FOREIGN]), ('id', FOREIGN)):
            count = len(self.writes)
            self.assertEqual(self.write_command('update', ['--item', ITEM], {key: value}).returncode, 1)
            self.assertEqual(len(self.writes), count)

    def test_foreign_wrong_collection_and_trashed_items_refused_for_all_operations(self):
        path = self.home / 'fixture.bin'
        path.write_bytes(CONTENT)
        original = copy.deepcopy(self.item)
        for change in ({'organizationId': FOREIGN}, {'collectionIds': []}, {'deletedDate': '2026-01-01T00:00:00Z'}):
            self.item = {**original, **change}
            for command, options, document in (
                    ('update', [], {'notes': FIELD}), ('trash', [], None), ('attach', ['--file', str(path)], None)):
                result = self.write_command(command, ['--item', ITEM, *options], document)
                self.assertEqual(result.returncode, 1)
                self.assert_quiet(result)
            for selection in (['--item', ITEM], ['--name', 'synthetic-item']):
                result = self.command([*selection, '--password-env', 'VAULT_PASSWORD'], 'raise Exception("ran")')
                self.assertEqual(result.returncode, 1)
                self.assertNotIn(b'command finished', result.stdout)
            self.assertEqual(self.writes, [])

    def test_trash_and_then_read_refusal(self):
        result = self.write_command('trash', ['--item', ITEM])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'status': 'trashed', 'itemId': ITEM})
        self.assertIsNotNone(self.item.get('deletedDate'))
        self.assertEqual(self.command(['--item', ITEM, '--password-env', 'VAULT_PASSWORD'], 'pass').returncode, 1)
        self.assert_quiet(result)

    def test_attachment_upload_verified_before_old_delete(self):
        path = self.home / 'fixture.bin'
        replacement = b'synthetic-replacement-content'
        path.write_bytes(replacement)
        result = self.write_command('attach', ['--item', ITEM, '--file', str(path)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'status': 'attached', 'itemId': ITEM, 'attachmentId': 'uploaded1'})
        self.assertEqual(self.item['attachments'], [{'id': 'uploaded1', 'fileName': 'fixture.bin', 'size': str(len(replacement))}])
        self.assertEqual(self.uploads['uploaded1'], replacement)
        self.assertIn('/object/attachment/uploaded1?itemid=' + ITEM, self.paths)
        self.assertEqual([entry[0] for entry in self.writes], ['POST', 'DELETE'])
        self.assertNotIn(replacement, result.stdout + result.stderr)
        self.assert_quiet(result)

    def test_failed_upload_bad_response_or_corrupt_download_keeps_previous_attachment(self):
        path = self.home / 'fixture.bin'
        path.write_bytes(b'synthetic-new-content')
        for failure in ('upload_error', 'bad_upload_response', 'corrupt_upload'):
            with self.subTest(failure=failure):
                self.item['attachments'] = [{'id': 'syntheticattachment', 'fileName': 'fixture.bin'}]
                self.writes.clear()
                setattr(self, failure, True)
                try:
                    result = self.write_command('attach', ['--item', ITEM, '--file', str(path)])
                    self.assertEqual(result.returncode, 1 if failure == 'upload_error' else 2)
                    if failure != 'upload_error':
                        self.assertIn(('item ' + ITEM + ' may exist').encode(), result.stderr)
                        self.assertIn(b'inspect before retrying', result.stderr)
                    self.assertTrue(any(entry['id'] == 'syntheticattachment' for entry in self.item['attachments']))
                    self.assertFalse(any(entry[0] == 'DELETE' and 'syntheticattachment?' in entry[1] for entry in self.writes))
                    self.assertEqual(self.uploads['syntheticattachment'], CONTENT)
                    self.assert_quiet(result)
                finally:
                    setattr(self, failure, False)

    def test_write_files_reject_symlinks_fifos_oversize_and_unsafe_filenames(self):
        path = self.home / 'source.json'
        path.write_text(json.dumps({'type': 1, 'name': VALUE}))
        link = self.home / 'link.json'
        link.symlink_to(path)
        fifo = self.home / 'fifo'
        os.mkfifo(fifo)
        oversized = self.home / 'large'
        with oversized.open('wb') as output:
            output.truncate(12 * 1024 * 1024 + 1)
        for source in (link, fifo, oversized):
            result = self.write_command('create', ['--input-file', str(source)])
            self.assertEqual(result.returncode, 1)
            self.assert_quiet(result)
        for name in ('bad"name', 'bad\\name', 'bad\nname'):
            source = self.home / name
            source.write_bytes(CONTENT)
            result = self.write_command('attach', ['--item', ITEM, '--file', str(source)])
            self.assertEqual(result.returncode, 1)
            self.assert_quiet(result)
        self.assertEqual(self.writes, [])

    def test_fetch_requires_data_for_reads_but_accepts_empty_write_success(self):
        spec = importlib.util.spec_from_file_location('vault_client', SOURCE)
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        client.ADDRESS = f'http://127.0.0.1:{self.server.server_port}'
        self.omit_read_data = True
        with self.assertRaises(KeyError):
            client.fetch('/object/item/' + ITEM)
        self.assertIsNone(client.fetch('/object/item/' + ITEM, method='DELETE'))

    def test_cache_wait_is_bounded_and_only_retries_local_not_found(self):
        spec = importlib.util.spec_from_file_location('vault_client', SOURCE)
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        client.ADDRESS = f'http://127.0.0.1:{self.server.server_port}'
        client.CACHE_WAIT_SECONDS = 0.05
        client.CACHE_POLL_SECONDS = 0.01
        self.read_error = (400, 'Not found.')
        with self.assertRaises(client.Refused):
            client.wait_for_cache(lambda: client.fetch('/object/item/' + ITEM))
        self.assertGreater(len(self.paths), 1)
        for error in ((403, 'Not found.'), (400, 'Permission denied.'), (500, 'Not found.')):
            self.paths.clear()
            self.read_error = error
            with self.assertRaises(client.urllib.error.HTTPError):
                client.wait_for_cache(lambda: client.fetch('/object/item/' + ITEM))
            self.assertEqual(len(self.paths), 1)
        self.read_error = None
        self.paths.clear()
        with self.assertRaises(client.Refused):
            client.wait_for_cache(lambda: client.fetch('/object/item/' + ITEM) == {})
        self.assertGreater(len(self.paths), 1)

    def test_cache_wait_preserves_http_error_for_non_json_400(self):
        spec = importlib.util.spec_from_file_location('vault_client', SOURCE)
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        client.ADDRESS = f'http://127.0.0.1:{self.server.server_port}'
        self.read_error = (400, 'unused')
        self.read_error_html = True
        with self.assertRaises(client.urllib.error.HTTPError) as raised:
            client.wait_for_cache(lambda: client.fetch('/object/item/' + ITEM))
        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(len(self.paths), 1)

    def test_canary_preserves_original_failure_and_reports_both_errors_without_values(self):
        spec = importlib.util.spec_from_file_location('canary', ROOT / 'env/tools/vault-write-canary.py')
        canary = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(canary)
        primary = ValueError(VALUE)

        def fail_main(arguments):
            raise primary

        def fail_cleanup(check):
            raise TimeoutError(FIELD)

        client = SimpleNamespace(scope=lambda: (ORG, COLLECTION), main=fail_main, wait_for_cache=fail_cleanup)
        with patch.dict(os.environ, self.env), contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(ValueError) as raised:
                canary.exercise(client, 'homelab')
        self.assertIs(raised.exception, primary)
        messages = output.getvalue()
        self.assertLess(messages.index('lifecycle failed: ValueError'), messages.index('cleanup failed: TimeoutError'))
        self.assertNotIn(VALUE, messages)
        self.assertNotIn(FIELD, messages)

    def test_canary_recovery_searches_active_and_trashed_items_with_exact_name_only(self):
        spec = importlib.util.spec_from_file_location('canary', ROOT / 'env/tools/vault-write-canary.py')
        canary = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(canary)
        name = 'synthetic-vault-write-fixture'
        paths = []

        def fetch(path):
            paths.append(path)
            item_id = FOREIGN if parse_qs(urlsplit(path).query)['trash'] == ['true'] else ITEM
            return {'data': [{'id': item_id, 'name': name}, {'id': item_id, 'name': name + '-extra'}]}

        items = canary.canary_items(SimpleNamespace(fetch=fetch), name)
        self.assertEqual([item['id'] for item in items], [ITEM, FOREIGN])
        self.assertEqual([parse_qs(urlsplit(path).query) for path in paths], [
            {'search': [name], 'trash': ['false']}, {'search': [name], 'trash': ['true']}])

    def canary(self, variant, setup=''):
        canary = ROOT / 'env/tools/vault-write-canary.py'
        code = ('import importlib.util,sys; '
            's=importlib.util.spec_from_file_location("vault",sys.argv[1]); '
            'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
            'm.ADDRESS=sys.argv[2]; m.SCOPE_FILE=m.Path(sys.argv[3]); '
            's=importlib.util.spec_from_file_location("canary",sys.argv[4]); '
            'c=importlib.util.module_from_spec(s); s.loader.exec_module(c);\n')
        code += setup + '\ntry:\n c.exercise(m,sys.argv[5])\nexcept BaseException:\n sys.exit(1)\n'
        return subprocess.run([sys.executable, '-c', code, str(SOURCE),
            f'http://127.0.0.1:{self.server.server_port}', str(self.scope), str(canary), variant],
            env=self.env, capture_output=True, timeout=30)

    def test_canary_cleans_out_of_scope_create_without_id_including_personal_trash(self):
        for changes in ({'organizationId': FOREIGN}, {'organizationId': None, 'collectionIds': []},
                        {'organizationId': None, 'collectionIds': [], 'deletedDate': '2026-01-01T00:00:00Z'}):
            with self.subTest(changes=changes):
                self.create_changes = changes
                self.paths.clear()
                self.extra_items = [{**copy.deepcopy(self.item or {}), 'id': FOREIGN, 'name': 'unrelated-synthetic-item'}]
                result = self.canary('homelab')
                self.assertEqual(result.returncode, 1)
                self.assertTrue(self.item is None)
                self.assertEqual(len(self.extra_items), 1)
                self.assert_quiet(result)
                queries = [parse_qs(urlsplit(path).query) for path in self.paths if path.startswith('/list/object/items?')]
                self.assertTrue(any(query.get('trash') == ['false'] for query in queries))
                self.assertTrue(any(query.get('trash') == ['true'] for query in queries))
                self.assertTrue(all(set(query) == {'search', 'trash'} for query in queries))

    def test_canary_recovers_uncertain_create_before_cache_lists_item(self):
        self.item = None
        self.cache_lag_reads = 2
        self.stale_list_after_delete = True
        self.create_changes = {'organizationId': None, 'collectionIds': []}
        result = self.canary('homelab')
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(self.item)
        self.assert_quiet(result)
        self.assertEqual(sum(method == 'POST' and path == '/object/item' for method, path, _ in self.writes), 1)
        self.assertEqual(sum(method == 'DELETE' for method, _, _ in self.writes), 1)

    def test_canary_cleans_create_that_times_out_after_server_stores_item(self):
        self.create_delay = 0.3
        self.create_changes = {'organizationId': None, 'collectionIds': [], 'deletedDate': '2026-01-01T00:00:00Z'}
        setup = ('original_fetch=m.fetch\n'
            'def fetch(path, **kwargs):\n'
            ' if path=="/object/item" and kwargs.get("method")=="POST":\n'
            '  kwargs["timeout"]=0.1\n'
            ' return original_fetch(path, **kwargs)\n'
            'm.fetch=fetch\n')
        result = self.canary('homelab', setup)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(self.item is None)
        self.assert_quiet(result)
        self.assertTrue(any(method == 'POST' and path == '/object/item' for method, path, _ in self.writes))
        self.assertTrue(any(method == 'DELETE' and path.endswith('?permanent=true') for method, path, _ in self.writes))

    def test_canary_waits_for_eventually_consistent_bw_cache_without_replaying_writes(self):
        self.cache_lag_reads = 2
        self.stale_list_after_delete = True
        for variant in ('homelab', 'work'):
            with self.subTest(variant=variant):
                self.env['T3_RUNTIME_VARIANT'] = variant
                (self.home / '.config/t3-runtime/environment').write_text(variant + '\n')
                self.scope.chmod(0o600)
                self.scope.write_text(json.dumps({'environment': variant, 'organizationId': ORG, 'collectionId': COLLECTION}))
                self.scope.chmod(0o444)
                self.item = None
                self.cache_reads = 0
                self.next_attachment = 0
                self.writes.clear()
                result = self.canary(variant)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNone(self.item)
                self.assertEqual([(method, urlsplit(path).path) for method, path, _ in self.writes
                    if urlsplit(path).path != '/sync'], [
                    ('POST', '/object/item'), ('PUT', '/object/item/' + ITEM),
                    ('POST', '/attachment'), ('POST', '/attachment'),
                    ('DELETE', '/object/attachment/uploaded1'),
                    ('DELETE', '/object/item/' + ITEM), ('DELETE', '/object/item/' + ITEM)])
                self.assert_quiet(result)

    def test_canary_complete_and_cleanup_after_failed_read_for_both_orgs(self):
        for variant, fail in (('homelab', False), ('work', False), ('homelab', True)):
            self.env['T3_RUNTIME_VARIANT'] = variant
            (self.home / '.config/t3-runtime/environment').write_text(variant + '\n')
            self.scope.chmod(0o600)
            self.scope.write_text(json.dumps({'environment': variant, 'organizationId': ORG, 'collectionId': COLLECTION}))
            self.scope.chmod(0o444)
            self.item = None
            self.writes.clear()
            result = self.canary(variant, 'm.run=lambda argv: 99' if fail else '')
            self.assertEqual(result.returncode, 1 if fail else 0, result.stderr)
            self.assertTrue(self.item is None)
            self.assert_quiet(result)
            self.assertTrue(any(method == 'DELETE' and path.endswith('?permanent=true') for method, path, _ in self.writes))

    def test_attachment_without_previous_and_ambiguous_names(self):
        path = self.home / 'new.bin'
        path.write_bytes(CONTENT)
        result = self.write_command('attach', ['--item', ITEM, '--file', str(path)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([entry[0] for entry in self.writes], ['POST'])
        self.assertEqual(len(self.item['attachments']), 2)
        self.assert_quiet(result)
        self.item['attachments'].append({'id': 'duplicate', 'fileName': 'new.bin'})
        self.writes.clear()
        self.assertEqual(self.write_command('attach', ['--item', ITEM, '--file', str(path)]).returncode, 1)
        self.assertEqual(self.writes, [])


if __name__ == '__main__':
    unittest.main()
