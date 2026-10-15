#!/usr/bin/env python3
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).with_name('vault-org-denial.py')
SPEC = importlib.util.spec_from_file_location('denial', SOURCE)
DENIAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DENIAL)
STOCK_BW = DENIAL.bw
ORG = '11111111-1111-4111-8111-111111111111'
COLLECTION = '22222222-2222-4222-8222-222222222222'
USER = '33333333-3333-4333-8333-333333333333'
ITEM = '44444444-4444-4444-8444-444444444444'
SCOPE = {'environment': 'work', 'organizationId': ORG, 'collectionId': COLLECTION}
PASSWORD = 'synthetic-password'
SESSION = 'synthetic-session'
TOKEN = 'header.' + base64.urlsafe_b64encode(json.dumps({'sub': USER}).encode()).decode().rstrip('=') + '.signature'


class DenialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.calls = []
        self.requests = []
        self.cli_allows = False
        self.cli_cleanup_code = 0
        self.server_code = 400
        self.server_result = {'message': 'No rights to modify the collection'}
        self.cleanup_code = 200
        self.membership = []
        self.bad_profile = False
        environment = {'HOME': self.temporary.name, 'PATH': '/usr/bin:/bin', 'T3_RUNTIME_VARIANT': 'homelab',
            'BW_SERVER': 'https://vault.invalid', 'BW_EMAIL': 'synthetic@example.invalid', 'BW_PASSWORD': PASSWORD,
            'BW_SESSION': 'must-not-reuse-live-session', 'VAULT_DENIED_SCOPE': json.dumps(SCOPE)}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, environment, clear=True).start()
        patch.object(DENIAL, 'bw', self.bw).start()
        patch.object(DENIAL, 'request', self.request).start()

    def bw(self, arguments, environment, document=None):
        self.calls.append((arguments, environment.copy(), document))
        if arguments[0] == 'config':
            return subprocess.CompletedProcess(arguments, 0)
        if arguments[0] == 'login':
            self.assertEqual(environment['BW_PASSWORD'], PASSWORD)
            (Path(environment['BITWARDENCLI_APPDATA_DIR']) / 'data.json').write_text(json.dumps({'token': TOKEN}))
            return subprocess.CompletedProcess(arguments, 0, SESSION.encode())
        self.assertNotIn('BW_PASSWORD', environment)
        self.assertEqual(environment['BW_SESSION'], SESSION)
        if arguments == ['status']:
            result = {'status': 'unlocked', 'userEmail': 'synthetic@example.invalid',
                      'serverUrl': 'https://vault.invalid', 'userId': USER}
            return subprocess.CompletedProcess(arguments, 0, json.dumps(result).encode())
        if arguments == ['create', 'item']:
            self.assertEqual(document['organizationId'], ORG)
            self.assertEqual(document['collectionIds'], [COLLECTION])
            return subprocess.CompletedProcess(arguments, 0 if self.cli_allows else 1,
                json.dumps({'id': ITEM}).encode(), PASSWORD.encode())
        self.assertEqual(arguments, ['delete', 'item', ITEM, '--permanent'])
        return subprocess.CompletedProcess(arguments, self.cli_cleanup_code)

    def request(self, server, token, path, payload=None, method='GET'):
        self.assertEqual(server, 'https://vault.invalid')
        self.assertEqual(token, TOKEN)
        self.requests.append((path, payload, method))
        if path == '/accounts/profile':
            return 200, {'id': ITEM if self.bad_profile else USER, 'organizations': self.membership}
        if path == '/ciphers/create':
            self.assertEqual(method, 'POST')
            self.assertEqual(payload['cipher']['organizationId'], ORG)
            self.assertEqual(payload['cipher']['encryptedFor'], USER)
            self.assertEqual(payload['collectionIds'], [COLLECTION])
            return self.server_code, self.server_result
        self.assertEqual(path, '/ciphers/' + ITEM)
        self.assertEqual(method, 'DELETE')
        return self.cleanup_code, None

    def run_main(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = DENIAL.main()
        output = stdout.getvalue() + stderr.getvalue()
        for secret in (PASSWORD, SESSION, TOKEN, USER, ORG, COLLECTION):
            self.assertNotIn(secret, output)
        for arguments, _, _ in self.calls:
            self.assertNotIn(PASSWORD, arguments)
            self.assertNotIn(SESSION, arguments)
            self.assertNotIn(TOKEN, arguments)
        return code, output

    def test_stock_cli_receives_values_on_stdin_and_captures_diagnostics(self):
        environment = {'PATH': '/usr/bin:/bin'}
        document = {'name': PASSWORD}
        with patch.object(DENIAL.subprocess, 'run') as run:
            STOCK_BW(['create', 'item'], environment, document)
        arguments, options = run.call_args
        self.assertEqual(arguments, (['bw', 'create', 'item'],))
        self.assertEqual(json.loads(base64.b64decode(options['input'])), document)
        self.assertEqual(options['stdout'], subprocess.PIPE)
        self.assertEqual(options['stderr'], subprocess.PIPE)
        self.assertEqual(options['timeout'], 45)

    def test_cleanup_transport_failure_keeps_private_item_id(self):
        def failed():
            raise TimeoutError

        with self.assertRaises(TimeoutError):
            DENIAL.cleanup_or_record(ITEM, failed)
        record, = Path(self.temporary.name).glob('vault-denial-cleanup-*.json')
        self.assertEqual(json.loads(record.read_text()), {'itemId': ITEM})

    def test_cli_and_authenticated_server_denial_both_required(self):
        code, output = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn('account and server refused', output)
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(any(arguments == ['create', 'item'] for arguments, _, _ in self.calls))

    def test_unrelated_http_or_auth_errors_are_not_permission_proof(self):
        for code, result in ((401, {'message': 'unauthorized'}), (400, {'message': 'Invalid collection ID provided'}),
                             (422, {'message': 'Invalid user cipher'}), (500, {'message': PASSWORD})):
            self.server_code, self.server_result = code, result
            self.assertEqual(self.run_main()[0], 1)

    def test_unexpected_cli_success_is_cleaned_and_fails(self):
        self.cli_allows = True
        self.assertEqual(self.run_main()[0], 1)
        self.assertEqual(self.calls[-1][0], ['delete', 'item', ITEM, '--permanent'])
        self.assertEqual(self.requests, [])
        self.cli_cleanup_code = 1
        self.assertEqual(self.run_main()[0], 1)
        record, = Path(self.temporary.name).glob('vault-denial-cleanup-*.json')
        self.assertEqual(json.loads(record.read_text()), {'itemId': ITEM})

    def test_unexpected_server_success_is_permanently_deleted_and_fails(self):
        self.server_code, self.server_result = 200, {'id': ITEM}
        self.assertEqual(self.run_main()[0], 1)
        self.assertEqual(self.requests[-1], ('/ciphers/' + ITEM, None, 'DELETE'))
        self.cleanup_code = 403
        self.assertEqual(self.run_main()[0], 1)
        record, = Path(self.temporary.name).glob('vault-denial-cleanup-*.json')
        self.assertEqual(json.loads(record.read_text()), {'itemId': ITEM})
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)

    def test_wrong_account_or_foreign_membership_fails_before_write(self):
        self.bad_profile = True
        self.assertEqual(self.run_main()[0], 1)
        self.bad_profile = False
        self.membership = [{'id': ORG}]
        self.assertEqual(self.run_main()[0], 1)
        self.assertFalse(any(path == '/ciphers/create' for path, _, _ in self.requests))

    def test_only_homelab_to_work_and_https_without_credentials(self):
        for key, value in (('T3_RUNTIME_VARIANT', 'work'), ('BW_SERVER', 'http://vault.invalid'),
                           ('BW_SERVER', 'https://user:password@vault.invalid'),
                           ('VAULT_DENIED_SCOPE', json.dumps({**SCOPE, 'environment': 'homelab'}))):
            with patch.dict(os.environ, {key: value}):
                self.assertEqual(self.run_main()[0], 1)
        self.assertEqual(self.calls, [])

    def test_access_token_selection_fails_closed_on_missing_or_multiple(self):
        self.assertEqual(DENIAL.access_token({'nested': [{'token': TOKEN}, TOKEN]}, USER), TOKEN)
        for data in ({}, {'token': 'opaque'}, {'tokens': [TOKEN, TOKEN + 'other']}):
            with self.assertRaises(RuntimeError):
                DENIAL.access_token(data, USER)


if __name__ == '__main__':
    unittest.main()
