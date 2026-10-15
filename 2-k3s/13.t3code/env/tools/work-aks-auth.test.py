#!/usr/bin/env python3
"""Offline Azure CLI fixtures. The production binary is never invoked."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('work_fixture', Path(__file__).with_name('work-runtime.test.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
auth = fixture.auth

FAKE_AZ = '''#!/usr/bin/python3
import json, os, pathlib, sys, time
home = pathlib.Path(os.environ['HOME'])
assert os.environ['AZURE_CONFIG_DIR'] == str(home / '.azure')
assert not any(name in os.environ for name in ('MCP_HUB_TOKEN', 'ANTHROPIC_AUTH_TOKEN', 'SSH_AUTH_SOCK', 'AZURE_CLIENT_SECRET'))
assert not any(name.startswith(('BW_', 'BITWARDENCLI_', 'VAULT_')) for name in os.environ)
args = sys.argv[1:]
with (home / 'calls').open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
if '--tenant' in args and '--subscription' in args:
    sys.stderr.write('Please specify only one of subscription and tenant, not both')
    sys.exit(1)
busy = home / '.azure/busy'
try:
    busy.mkdir()
except FileExistsError:
    sys.stderr.write('fixture concurrent Azure call')
    sys.exit(1)
try:
    time.sleep(float((home / 'delay').read_text()) if (home / 'delay').exists() else 0)
    mode = (home / 'mode').read_text() if (home / 'mode').exists() else 'success'
    if mode == 'auth-required' or (mode == 'token-auth-required' and 'get-access-token' in args):
        sys.stderr.write('interaction_required AADSTS700082 PRIVATE-FIXTURE-DETAIL')
        sys.stdout.write('partial failed fixture output')
        sys.exit(1)
    if mode == 'non-auth':
        sys.stderr.write('network or RBAC failure PRIVATE-FIXTURE-DETAIL')
        sys.exit(1)
    if args[0] == 'login':
        sys.stdout.write('private enrollment fixture')
        sys.stderr.write('private device-code fixture')
        sys.exit(1 if mode == 'login-denied' else 0)
    if args[:2] == ['account', 'show']:
        result = {'tenantId': '11111111-1111-1111-1111-111111111111',
            'id': '33333333-3333-3333-3333-333333333333',
            'user': {'name': 'fixture@work.example.test', 'type': 'user'}}
        if mode == 'foreign-account':
            result['user']['name'] = 'foreign@example.test'
    else:
        result = {'tenant': '11111111-1111-1111-1111-111111111111',
            'subscription': '33333333-3333-3333-3333-333333333333',
            'accessToken': 'synthetic-work-access-token', 'expires_on': int(time.time()) + 600}
        if mode == 'expired':
            result['expires_on'] = 1
        if mode == 'foreign-token':
            result['tenant'] = '44444444-4444-4444-4444-444444444444'
    print(json.dumps(result))
finally:
    busy.rmdir()
'''


class WorkAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home, self.inputs = self.root / 'home', self.root / 'inputs'
        self.home.mkdir()
        fixture.fixture(self.inputs)
        marker = self.home / '.config/t3-runtime/environment'
        marker.parent.mkdir(parents=True)
        marker.write_text('work\n')
        binary = self.root / 'az'
        binary.write_text(FAKE_AZ)
        binary.chmod(0o700)
        self.binary_patch = patch.object(auth, 'AZ', str(binary))
        self.binary_patch.start()
        self.environment_patch = patch.dict(os.environ, {'PATH': os.environ['PATH'],
            'AZURE_CLIENT_SECRET': 'must-not-be-inherited', 'ANTHROPIC_AUTH_TOKEN': 'must-not-be-inherited'}, clear=True)
        self.environment_patch.start()

    def tearDown(self):
        self.environment_patch.stop()
        self.binary_patch.stop()
        self.tmp.cleanup()

    def calls(self):
        path = self.home / 'calls'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_token(self):
        return auth.run('token', self.inputs, self.home)

    def mode(self, value):
        (self.home / 'mode').write_text(value)

    def test_exec_credential_and_exact_tenant_audience_subscription_clean_environment(self):
        credential = self.run_token()
        self.assertEqual(credential['kind'], 'ExecCredential')
        self.assertEqual(credential['apiVersion'], 'client.authentication.k8s.io/v1')
        self.assertEqual(credential['status']['token'], 'synthetic-work-access-token')
        self.assertTrue(credential['status']['expirationTimestamp'].endswith('Z'))
        config = auth.validate(self.inputs)
        account_args, args = self.calls()
        self.assertEqual(account_args[:2], ['account', 'show'])
        self.assertEqual(account_args[account_args.index('--subscription') + 1], config['subscriptionId'])
        self.assertNotIn('--tenant', args)
        for flag, value in [('--resource', config['audience']), ('--subscription', config['subscriptionId'])]:
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertEqual((self.home / '.azure').stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.home / '.azure/t3-work-auth.lock').stat().st_mode & 0o777, 0o600)

    def test_auth_required_and_expired_session_never_login_or_return_partial_output(self):
        for mode in ('auth-required', 'token-auth-required', 'expired'):
            self.mode(mode)
            with self.assertRaisesRegex(auth.AuthError, 'authentication required') as error:
                self.run_token()
            self.assertNotIn('PRIVATE-FIXTURE-DETAIL', str(error.exception))
            self.assertNotIn('partial', str(error.exception))
        self.assertTrue(all(args[0] != 'login' for args in self.calls()))

    def test_non_auth_errors_do_not_enroll_or_disclose_details(self):
        self.mode('non-auth')
        with self.assertRaisesRegex(auth.AuthError, 'no login attempted') as error:
            self.run_token()
        self.assertNotIn('PRIVATE-FIXTURE-DETAIL', str(error.exception))
        self.assertEqual(len(self.calls()), 1)

    def test_foreign_account_or_token_refused(self):
        for mode in ('foreign-account', 'foreign-token'):
            self.mode(mode)
            with self.assertRaises(auth.AuthError): self.run_token()
        self.assertTrue(all(args[0] != 'login' for args in self.calls()))

    def test_parallel_refreshes_are_serialized(self):
        (self.home / 'delay').write_text('0.08')
        with ThreadPoolExecutor(max_workers=4) as pool:
            credentials = list(pool.map(lambda _: self.run_token(), range(4)))
        self.assertEqual(len(credentials), 4)
        self.assertEqual(len(self.calls()), 8)
        self.assertFalse((self.home / '.azure/busy').exists())

    def test_lock_timeout_and_azure_command_timeout_are_bounded(self):
        env = auth.environment(self.home)
        with (self.home / '.azure/t3-work-auth.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with patch.object(auth, 'LOCK_TIMEOUT', 0.05), self.assertRaisesRegex(auth.AuthError, 'waiting'):
                self.run_token()
        self.assertEqual(self.calls(), [])
        (self.home / 'delay').write_text('1')
        with patch.object(auth, 'TOKEN_TIMEOUT', 0.05), self.assertRaisesRegex(auth.AuthError, 'command timed out'):
            self.run_token()
        with auth.locked(env):
            pass

    def test_explicit_login_only_uses_private_terminal_then_verifies_account(self):
        with tempfile.TemporaryFile() as terminal, patch.object(auth, 'open', return_value=terminal, create=True):
            self.assertIsNone(auth.run('login', self.inputs, self.home))
        self.assertEqual(self.calls()[0][0], 'login')
        self.assertIn('--use-device-code', self.calls()[0])
        self.assertEqual(self.calls()[1][:2], ['account', 'show'])

    def test_login_and_token_requests_share_the_lock_and_login_timeout_is_bounded(self):
        (self.home / 'delay').write_text('0.08')
        with tempfile.TemporaryFile() as terminal, patch.object(auth, 'open', return_value=terminal, create=True):
            with ThreadPoolExecutor(max_workers=2) as pool:
                login = pool.submit(auth.run, 'login', self.inputs, self.home)
                token = pool.submit(self.run_token)
                self.assertIsNone(login.result())
                self.assertEqual(token.result()['kind'], 'ExecCredential')
        self.assertEqual(len(self.calls()), 4)
        (self.home / 'delay').write_text('1')
        with tempfile.TemporaryFile() as terminal, patch.object(auth, 'open', return_value=terminal, create=True):
            with patch.object(auth, 'LOGIN_TIMEOUT', 0.05), self.assertRaisesRegex(auth.AuthError, 'enrollment timed out'):
                auth.run('login', self.inputs, self.home)

    def test_login_refuses_without_terminal_and_handles_denial(self):
        with patch.object(auth, 'open', side_effect=OSError('no tty'), create=True), self.assertRaises(OSError):
            auth.run('login', self.inputs, self.home)
        self.assertEqual(self.calls(), [])
        self.mode('login-denied')
        with tempfile.TemporaryFile() as terminal, patch.object(auth, 'open', return_value=terminal, create=True):
            with self.assertRaisesRegex(auth.AuthError, 'enrollment failed'):
                auth.run('login', self.inputs, self.home)

    def test_missing_foreign_config_or_exec_environment_refused_before_azure(self):
        for mutate in (lambda obj: obj.update(environment='homelab'), lambda obj: obj.update(tenantId=''),
                       lambda obj: obj.update(contexts=[])):
            fixture.fixture(self.inputs)
            path = self.inputs / 'work-auth.json'
            obj = json.loads(path.read_text())
            mutate(obj)
            path.write_text(json.dumps(obj))
            fixture.refresh(self.inputs)
            with self.assertRaises(auth.AuthError): self.run_token()
        fixture.fixture(self.inputs)
        path = self.inputs / 'kubeconfig'
        obj = json.loads(path.read_text())
        obj['users'][0]['user']['exec']['env'] = [{'name': 'AZURE_CONFIG_DIR', 'value': '/foreign'}]
        path.write_text(json.dumps(obj))
        fixture.refresh(self.inputs)
        with self.assertRaises(auth.AuthError): self.run_token()
        (self.inputs / 'work-auth.json').unlink()
        with self.assertRaises(FileNotFoundError): self.run_token()
        self.assertEqual(self.calls(), [])

    def test_vault_runner_environment_keeps_work_auth_without_forwarding_values(self):
        with patch.dict(os.environ, {'T3_VAULT_CLIENT': '1', 'VAULT_PASSWORD': 'synthetic-work-password'}):
            self.assertEqual(self.run_token()['kind'], 'ExecCredential')

    def test_local_work_ssh_agent_environment_is_refused_before_azure(self):
        with patch.dict(os.environ, {'SSH_AUTH_SOCK': '/run/t3-vault/ssh-agent.sock'}):
            with self.assertRaises(auth.AuthError):
                self.run_token()
        self.assertEqual(self.calls(), [])

    def test_every_vault_client_variable_is_refused_including_empty_values(self):
        for name in ('BW_EMAIL', 'BW_PASSWORD', 'BW_SERVER', 'BW_ORGANIZATION_ID', 'BW_COLLECTION_ID',
                     'BW_SESSION', 'BW_CLIENTID', 'BW_CLIENTSECRET', 'BW_UNKNOWN', 'BITWARDENCLI_APPDATA_DIR'):
            for value in ('synthetic-unapproved', ''):
                with self.subTest(name=name, present=bool(value)), patch.dict(os.environ, {name: value}):
                    with self.assertRaises(auth.AuthError):
                        self.run_token()
        self.assertEqual(self.calls(), [])

    def test_foreign_home_cache_environment_and_actions_refused(self):
        for name in ('AZURE_CONFIG_DIR', 'SSH_AUTH_SOCK', 'BW_SESSION'):
            with patch.dict(os.environ, {name: '/foreign'}), self.assertRaises(auth.AuthError): self.run_token()
        (self.home / '.config/t3-runtime/environment').write_text('homelab\n')
        with self.assertRaises(auth.AuthError): self.run_token()
        for action in ('id', 'token; id', 'login extra'):
            with self.assertRaises(auth.AuthError): auth.run(action, self.inputs, self.home)
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()
