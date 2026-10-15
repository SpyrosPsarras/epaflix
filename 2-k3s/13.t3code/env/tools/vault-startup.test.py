#!/usr/bin/env python3
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
import unittest
from unittest.mock import call, patch

SOURCE = Path(__file__).resolve().parents[2] / 'env/files/vault-client.py'
FAKE = '''#!/usr/bin/env python3
import json,os,pathlib,sys,time
from http.server import BaseHTTPRequestHandler,HTTPServer
home=pathlib.Path.home()
config=json.loads((home/'fixture.json').read_text())
args=sys.argv[1:]
logged_in=(config['cached'] and not config.get('logged_out')) or (home/'logged-in').exists()
(pathlib.Path(os.environ['BITWARDENCLI_APPDATA_DIR'])/'data.json').touch()
with (home/'calls').open('a') as output:
    output.write(json.dumps({'args':args,'password':os.environ.get('BW_PASSWORD'),
                            'session':os.environ.get('BW_SESSION'),
                            'data':os.environ.get('BITWARDENCLI_APPDATA_DIR')})+'\\n')
def state():
    authenticated=logged_in and not (config.get('unauthenticated_response') and not (home/'logged-in').exists())
    status='locked' if authenticated else 'unauthenticated'
    if authenticated and not (home/'lock').exists() and (os.environ.get('BW_SESSION') or (home/'unlocked').exists()):
        status='unlocked'
    return {'status':status,
            'userEmail':config.get('email','agent@homelab.example.test') if authenticated else None,
            'serverUrl':config.get('server','https://vault.homelab.example.test')}
if args == ['status']:
    print(json.dumps(state()))
elif args[:2] == ['config','server']:
    assert args == ['config','server','https://vault.homelab.example.test']
elif args[0] in ('login','unlock'):
    assert args == (['login','agent@homelab.example.test'] if args[0]=='login' else ['unlock'])+['--passwordenv','BW_PASSWORD','--raw']
    assert os.environ['BW_PASSWORD']=='synthetic-login-password'
    if args[0]=='login' and (home/'server-pid').exists():
        assert not pathlib.Path('/proc/'+(home/'server-pid').read_text()).exists()
    if config.get('fail'):
        print('synthetic-login-password',file=sys.stderr)
        sys.exit(1)
    (home/'logged-in').touch()
    print('synthetic-session')
elif args == ['sync']:
    assert os.environ['BW_SESSION']=='synthetic-session'
elif args == ['serve','--hostname','127.0.0.1','--port','8087']:
    assert 'BW_PASSWORD' not in os.environ
    (home/'server-pid').write_text(str(os.getpid()))
    if not logged_in:
        sys.exit(1)
    def write_listener(uid):
        address=config.get('listener_address','0100007F')
        table='tcp6' if len(address)==32 else 'tcp'
        inode=config.get('listener_inode',12345)
        (home/'proc/net'/table).write_text('sl local_address rem_address st tx_queue rx_queue tr uid timeout inode\\n'+
            f'0: {address}:{config["port"]:04X} {"0"*len(address)}:0000 0A 0:0 00:0 0 {uid} 0 {inode}\\n')
    class Handler(BaseHTTPRequestHandler):
        requests=0
        def log_message(self,*args): pass
        def do_POST(self):
            with (home/'http-posts').open('a') as output:
                output.write(self.path+'\\n')
            if self.path=='/unlock':
                assert self.headers['Content-Type']=='application/json'
                assert json.loads(self.rfile.read(int(self.headers['Content-Length'])))=={'password':'synthetic-login-password'}
                if config.get('fail'):
                    print('synthetic-login-password',file=sys.stderr,flush=True)
                    self.send_response(400)
                    self.end_headers()
                    return
                (home/'unlocked').touch()
                data=json.dumps({'success':True,'data':{'object':'message','noColor':False,
                    'title':'Your vault is now unlocked!','message':'Set BW_SESSION=synthetic-session',
                    'raw':'synthetic-session'}}).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.end_headers()
                self.wfile.write(data)
                return
            assert self.path=='/sync'
            (home/'sync-started').touch()
            while config.get('block_sync') and not (home/'sync-release').exists():
                time.sleep(0.01)
            time.sleep(config.get('sync_delay',0))
            if config.get('sync_fail'):
                self.send_response(503)
                self.end_headers()
                return
            if config.get('sync_lock'):
                (home/'lock').touch()
            data=json.dumps({'success':True,'data':{'object':'message','noColor':False,
                'title':'Syncing complete.','message':None}}).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            status=state()
            responses=config.get('responses',[])
            selected=responses[Handler.requests] if Handler.requests<len(responses) else 'ok'
            if status['status']=='unauthenticated':
                selected='unauthenticated'
            Handler.requests+=1
            with (home/'http-calls').open('a') as output:
                output.write(selected+'\\n')
            if selected=='error':
                self.send_response(503)
                self.end_headers()
                return
            if selected=='foreign':
                status['userEmail']='other@work.example.test'
            data=json.dumps({'success':True,'data':{'object':'template',
                'template':[] if selected=='malformed' else status}}).encode()
            if config.get('swap_listener'):
                write_listener(os.getuid()+1)
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.end_headers()
            self.wfile.write(data)
    server=HTTPServer(('127.0.0.1',config['port']),Handler)
    if 'listener_uid' in config:
        descriptors=home/'proc'/str(os.getpid())/'fd'
        descriptors.mkdir(parents=True)
        (descriptors/'3').symlink_to('socket:[12345]')
        write_listener(config['listener_uid'])
    if config.get('listener_exit'):
        sys.exit(1)
    server.serve_forever()
else:
    sys.exit(2)
'''


class RetryTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('vault', SOURCE)
        self.vault = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.vault)

    def test_retry_returns_the_action_result_after_transient_failures(self):
        attempts = []

        def action():
            attempts.append(None)
            if len(attempts) < 3:
                raise self.vault.Refused
            return 'ready'

        with patch.object(self.vault.time, 'sleep') as sleep:
            self.assertEqual(self.vault.retry(action), 'ready')
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleep.call_args_list, [call(2), call(2)])

    def test_retry_uses_three_attempts_for_expected_and_unexpected_failures(self):
        for error in (self.vault.Refused, RuntimeError):
            with self.subTest(error=error):
                attempts = []

                def action():
                    attempts.append(None)
                    raise error

                with patch.object(self.vault.time, 'sleep') as sleep:
                    with self.assertRaises(error):
                        self.vault.retry(action)
                self.assertEqual(len(attempts), 3)
                self.assertEqual(sleep.call_args_list, [call(2), call(2)])

    def test_retry_never_retries_cancellation_or_listener_failures(self):
        for error in (self.vault.Cancelled, self.vault.UnsafeListener):
            with self.subTest(error=error):
                attempts = []

                def action():
                    attempts.append(None)
                    raise error

                with patch.object(self.vault.time, 'sleep') as sleep:
                    with self.assertRaises(error):
                        self.vault.retry(action)
                self.assertEqual(len(attempts), 1)
                sleep.assert_not_called()


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        executable = self.bin / 'bw'
        executable.write_text(FAKE)
        executable.chmod(0o700)
        self.scope = self.home / 'scope.json'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.env = {**os.environ, 'HOME': str(self.home), 'PATH': str(self.bin) + ':' + os.environ['PATH'],
                    'BW_EMAIL': 'agent@homelab.example.test', 'BW_PASSWORD': 'synthetic-login-password',
                    'BW_SERVER': 'https://vault.homelab.example.test', 'T3_RUNTIME_VARIANT': 'homelab',
                    'BW_ORGANIZATION_ID': '11111111-1111-4111-8111-111111111111',
                    'BW_COLLECTION_ID': '22222222-2222-4222-8222-222222222222'}
        self.prefix = [sys.executable, '-c',
            'import importlib.util,sys; s=importlib.util.spec_from_file_location("vault",sys.argv[1]); '
            'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
            'm.ADDRESS=sys.argv[2]; m.SCOPE_FILE=m.Path(sys.argv[3]); m.HEALTH_INTERVAL=0.05; '
            'sys.exit(m.main(sys.argv[4:]))', str(SOURCE), f'http://127.0.0.1:{self.port}', str(self.scope), 'serve']

    def tearDown(self):
        self.tmp.cleanup()

    def fixture(self, **changes):
        (self.home / 'fixture.json').write_text(json.dumps({'cached': False, 'port': self.port, **changes}))
        data = self.home / 'client'
        data.mkdir(mode=0o700, exist_ok=True)
        if changes.get('cached'):
            (data / 'data.json').write_text('{}')
        else:
            (data / 'data.json').unlink(missing_ok=True)
        if 'listener_uid' in changes:
            net = self.home / 'proc/net'
            net.mkdir(parents=True, exist_ok=True)
            for name in ('tcp', 'tcp6'):
                (net / name).write_text('sl local_address rem_address st tx_queue rx_queue tr uid timeout inode\n')

    def proc_prefix(self):
        prefix = self.prefix.copy()
        prefix[2] = prefix[2].replace('m.ADDRESS=sys.argv[2];',
            'm.PROC=m.Path(sys.argv[3]).parent/"proc"; m.ADDRESS=sys.argv[2];')
        return prefix

    def calls(self):
        return [json.loads(line) for line in (self.home / 'calls').read_text().splitlines()]

    def wait_ready(self, process, timeout=10):
        deadline = time.monotonic() + timeout
        while not self.scope.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.scope.exists())
        self.assertIsNone(process.poll())

    def assert_quiet(self, stdout, stderr):
        for secret in (b'synthetic-login-password', b'synthetic-session'):
            self.assertNotIn(secret, stdout + stderr)

    def test_fresh_login_and_cached_unlock_publish_only_nonsecret_scope(self):
        for cached in (False, True):
            self.fixture(cached=cached)
            (self.home / 'calls').unlink(missing_ok=True)
            with subprocess.Popen(self.prefix, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                self.wait_ready(process)
                self.assertEqual(json.loads(self.scope.read_text()), {'environment': 'homelab',
                    'organizationId': '11111111-1111-4111-8111-111111111111',
                    'collectionId': '22222222-2222-4222-8222-222222222222'})
                self.assertEqual(self.scope.stat().st_mode & 0o777, 0o444)
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 130, stderr)
                self.assert_quiet(stdout, stderr)
            calls = self.calls()
            self.assertEqual([call['args'][0] for call in calls],
                             ['serve'] if cached else ['status', 'config', 'login', 'serve'])
            self.assertIsNone(calls[-1]['password'])
            self.assertEqual(calls[-1]['session'], None if cached else 'synthetic-session')
            self.assertEqual(calls[-1]['data'], str(self.home / 'client'))
            self.assertEqual((self.home / 'client').stat().st_mode & 0o777, 0o700)
            self.assertFalse(self.scope.exists())
            self.assertFalse(Path('/proc/' + (self.home / 'server-pid').read_text()).exists())

    def test_cached_restart_starts_serve_before_sync_and_publishes_only_after_sync(self):
        self.fixture(cached=True, block_sync=True)
        with subprocess.Popen(self.prefix, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            try:
                deadline = time.monotonic() + 5
                while not (self.home / 'sync-started').exists() and not self.scope.exists() and (
                        process.poll() is None and time.monotonic() < deadline):
                    time.sleep(0.01)
                self.assertTrue((self.home / 'sync-started').exists())
                self.assertEqual([call['args'][0] for call in self.calls()], ['serve'])
                self.assertEqual((self.home / 'http-posts').read_text().splitlines(), ['/unlock', '/sync'])
                self.assertFalse(self.scope.exists())
                (self.home / 'sync-release').touch()
                self.wait_ready(process)
                self.assertGreaterEqual(len((self.home / 'http-calls').read_text().splitlines()), 2)
            finally:
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
                self.assert_quiet(stdout, stderr)

    def test_foreign_listener_is_refused_without_any_http_request_or_login_fallback(self):
        changes = [
            {'listener_uid': os.getuid() + 1},
            {'listener_uid': os.getuid() + 1, 'listener_exit': True},
            {'listener_uid': os.getuid(), 'listener_inode': 99999},
            {'listener_uid': os.getuid(), 'listener_inode': 99999, 'listener_address': '00000000'},
            {'listener_uid': os.getuid(), 'listener_inode': 99999,
             'listener_address': '0000000000000000FFFF00000100007F'},
            {'listener_uid': os.getuid(), 'listener_inode': 99999,
             'listener_address': '00000000000000000000000000000000'}]
        for change in changes:
            with self.subTest(change=change):
                self.fixture(cached=True, **change)
                for name in ('calls', 'http-calls', 'http-posts', 'unlocked'):
                    (self.home / name).unlink(missing_ok=True)
                with subprocess.Popen(self.proc_prefix(), env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                    try:
                        deadline = time.monotonic() + 5
                        while process.poll() is None and not self.scope.exists() and time.monotonic() < deadline:
                            time.sleep(0.01)
                    finally:
                        if process.poll() is None:
                            process.send_signal(signal.SIGTERM)
                        stdout, stderr = process.communicate(timeout=5)
                        self.assert_quiet(stdout, stderr)
                self.assertFalse((self.home / 'http-posts').exists())
                self.assertFalse((self.home / 'http-calls').exists())
                self.assertEqual(process.returncode, 1)
                self.assertEqual([call['args'][0] for call in self.calls()], ['serve'])
                self.assertFalse(self.scope.exists())
                self.assertFalse(Path('/proc/' + (self.home / 'server-pid').read_text()).exists())

    def test_owned_listener_proceeds_to_unlock_and_sync(self):
        self.fixture(cached=True, listener_uid=os.getuid())
        with subprocess.Popen(self.proc_prefix(), env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            try:
                self.wait_ready(process)
                self.assertEqual((self.home / 'http-posts').read_text().splitlines(), ['/unlock', '/sync'])
            finally:
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
                self.assert_quiet(stdout, stderr)

    def test_listener_ownership_is_rechecked_before_sending_the_password(self):
        self.fixture(cached=True, listener_uid=os.getuid(), swap_listener=True)
        with subprocess.Popen(self.proc_prefix(), env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            try:
                deadline = time.monotonic() + 5
                while process.poll() is None and not self.scope.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
                self.assert_quiet(stdout, stderr)
        self.assertEqual(process.returncode, 1)
        self.assertEqual((self.home / 'http-calls').read_text().splitlines(), ['ok'])
        self.assertFalse((self.home / 'http-posts').exists())
        self.assertEqual([call['args'][0] for call in self.calls()], ['serve'])
        self.assertFalse(self.scope.exists())

    def test_sync_can_take_longer_than_the_normal_http_deadline(self):
        self.fixture(cached=True, sync_delay=11)
        with subprocess.Popen(self.prefix, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            try:
                self.wait_ready(process, timeout=15)
            finally:
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
                self.assert_quiet(stdout, stderr)
        self.assertEqual(process.returncode, 130)
        self.assertIn(b'vault: local client ready', stdout)

    def test_failed_sync_or_relocked_account_never_publishes_scope(self):
        for change in ({'sync_fail': True}, {'sync_lock': True}):
            with self.subTest(change=change):
                self.fixture(cached=True, **change)
                (self.home / 'lock').unlink(missing_ok=True)
                result = subprocess.run(self.prefix, env=self.env, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 1)
                self.assertTrue((self.home / 'sync-started').exists())
                self.assertFalse(self.scope.exists())
                self.assertNotIn(b'vault: local client ready', result.stdout)
                self.assertFalse(Path('/proc/' + (self.home / 'server-pid').read_text()).exists())
                self.assert_quiet(result.stdout, result.stderr)

    def test_failed_cached_unlock_does_not_fall_back_to_login(self):
        self.fixture(cached=True, fail=True)
        result = subprocess.run(self.prefix, env=self.env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual([call['args'][0] for call in self.calls()], ['serve'])
        self.assertEqual((self.home / 'http-posts').read_text().splitlines(), ['/unlock'] * 3)
        self.assertFalse(self.scope.exists())
        self.assert_quiet(result.stdout, result.stderr)

    def test_unauthenticated_cached_data_stops_serve_before_fresh_login(self):
        for change in ({'logged_out': True}, {'unauthenticated_response': True}):
            with self.subTest(change=change):
                self.fixture(cached=True, **change)
                for name in ('logged-in', 'calls', 'http-posts'):
                    (self.home / name).unlink(missing_ok=True)
                with subprocess.Popen(self.prefix, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                    try:
                        self.wait_ready(process)
                    finally:
                        process.send_signal(signal.SIGTERM)
                        stdout, stderr = process.communicate(timeout=5)
                        self.assert_quiet(stdout, stderr)
                self.assertEqual([call['args'][0] for call in self.calls()],
                                 ['serve', 'status', 'config', 'login', 'serve'])
                self.assertEqual((self.home / 'http-posts').read_text().splitlines(), ['/sync'])
                self.assertFalse(self.scope.exists())
                self.assertFalse(Path('/proc/' + (self.home / 'server-pid').read_text()).exists())

    def test_failed_login_is_bounded_quiet_and_clears_stale_scope(self):
        self.fixture(fail=True)
        self.scope.write_text('stale')
        result = subprocess.run(self.prefix, env=self.env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(sum(call['args'][0] == 'login' for call in self.calls()), 3)
        self.assertFalse(self.scope.exists())
        self.assert_quiet(result.stdout, result.stderr)

    def test_foreign_cached_account_or_server_is_not_unlocked(self):
        for change in ({'email': 'other@work.example.test'}, {'server': 'https://vault.work.example.test'}):
            with self.subTest(change=change):
                self.fixture(cached=True, **change)
                (self.home / 'calls').unlink(missing_ok=True)
                result = subprocess.run(self.prefix, env=self.env, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 1)
                self.assertEqual([call['args'][0] for call in self.calls()], ['serve'])
                self.assertFalse((self.home / 'http-posts').exists())
                self.assertFalse(Path('/proc/' + (self.home / 'server-pid').read_text()).exists())
                self.assertFalse(self.scope.exists())
                self.assert_quiet(result.stdout, result.stderr)

    def test_foreign_resident_account_is_not_unlocked(self):
        self.fixture(cached=True, responses=['foreign'] * 100)
        result = subprocess.run(self.prefix, env=self.env, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 1)
        self.assertEqual([call['args'][0] for call in self.calls()], ['serve'])
        self.assertFalse((self.home / 'http-posts').exists())
        self.assertFalse(self.scope.exists())
        self.assertFalse(Path('/proc/' + (self.home / 'server-pid').read_text()).exists())
        self.assert_quiet(result.stdout, result.stderr)

    def test_health_failures_reset_after_success_and_stop_after_three_consecutive_failures(self):
        responses = ['ok', 'ok', 'error', 'error', 'ok', 'error', 'error', 'ok', 'error', 'error', 'error']
        self.fixture(cached=True, responses=responses)
        result = subprocess.run(self.prefix, env=self.env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual((self.home / 'http-calls').read_text().splitlines(), responses)
        self.assertFalse(self.scope.exists())
        self.assertFalse(Path('/proc/' + (self.home / 'server-pid').read_text()).exists())
        self.assert_quiet(result.stdout, result.stderr)

    def test_malformed_or_foreign_status_uses_the_same_three_failure_tolerance(self):
        responses = ['ok', 'ok', 'malformed', 'foreign', 'ok', 'malformed', 'foreign', 'malformed']
        self.fixture(cached=True, responses=responses)
        result = subprocess.run(self.prefix, env=self.env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual((self.home / 'http-calls').read_text().splitlines(), responses)
        self.assertFalse(self.scope.exists())
        self.assertFalse(Path('/proc/' + (self.home / 'server-pid').read_text()).exists())
        self.assert_quiet(result.stdout, result.stderr)

    def test_two_transient_failures_do_not_restart_the_client(self):
        responses = ['ok', 'ok', 'error', 'error', 'ok', 'error', 'error', 'ok']
        self.fixture(cached=True, responses=responses)
        with subprocess.Popen(self.prefix, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            self.wait_ready(process)
            deadline = time.monotonic() + 5
            while len((self.home / 'http-calls').read_text().splitlines()) < len(responses) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(len((self.home / 'http-calls').read_text().splitlines()), len(responses))
            self.assertIsNone(process.poll())
            self.assertTrue(self.scope.exists())
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130)
            self.assert_quiet(stdout, stderr)

    def test_locked_server_removes_scope_and_stops_client(self):
        self.fixture(cached=True)
        with subprocess.Popen(self.prefix, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            self.wait_ready(process)
            (self.home / 'lock').touch()
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 1, stderr)
            self.assertFalse(self.scope.exists())
            self.assertFalse(Path('/proc/' + (self.home / 'server-pid').read_text()).exists())
            self.assert_quiet(stdout, stderr)

    def test_work_sidecar_scope_and_https_login_validation(self):
        self.fixture(cached=True)
        self.env['T3_RUNTIME_VARIANT'] = 'work'
        with subprocess.Popen(self.prefix, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            self.wait_ready(process)
            self.assertEqual(json.loads(self.scope.read_text())['environment'], 'work')
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130)
            self.assert_quiet(stdout, stderr)
        self.env['BW_SERVER'] = 'http://vault.homelab.example.test'
        (self.home / 'calls').unlink()
        result = subprocess.run(self.prefix, env=self.env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.home / 'calls').exists())
        self.assertFalse(self.scope.exists())


if __name__ == '__main__':
    unittest.main()
