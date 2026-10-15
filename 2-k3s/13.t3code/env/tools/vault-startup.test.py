#!/usr/bin/env python3
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

SOURCE = Path(__file__).resolve().parents[2] / 'env/files/vault-client.py'
FAKE = '''#!/usr/bin/env python3
import json,os,pathlib,sys
from http.server import BaseHTTPRequestHandler,HTTPServer
home=pathlib.Path.home()
config=json.loads((home/'fixture.json').read_text())
args=sys.argv[1:]
with (home/'calls').open('a') as output:
    output.write(json.dumps({'args':args,'password':os.environ.get('BW_PASSWORD'),
                            'session':os.environ.get('BW_SESSION'),
                            'data':os.environ.get('BITWARDENCLI_APPDATA_DIR')})+'\\n')
if args == ['status']:
    print(json.dumps({'status':'locked' if config['cached'] or (home/'logged-in').exists() else 'unauthenticated',
                      'userEmail':config.get('email','agent@homelab.example.test'),
                      'serverUrl':'https://vault.homelab.example.test'}))
elif args[:2] == ['config','server']:
    assert args == ['config','server','https://vault.homelab.example.test']
elif args[0] in ('login','unlock'):
    assert args == (['login','agent@homelab.example.test'] if args[0]=='login' else ['unlock'])+['--passwordenv','BW_PASSWORD','--raw']
    assert os.environ['BW_PASSWORD']=='synthetic-login-password'
    if config.get('fail'):
        print('synthetic-login-password',file=sys.stderr)
        sys.exit(1)
    (home/'logged-in').touch()
    print('synthetic-session')
elif args == ['sync']:
    assert os.environ['BW_SESSION']=='synthetic-session'
elif args == ['serve','--hostname','127.0.0.1','--port','8087']:
    assert 'BW_PASSWORD' not in os.environ
    assert os.environ['BW_SESSION']=='synthetic-session'
    (home/'server-pid').write_text(str(os.getpid()))
    class Handler(BaseHTTPRequestHandler):
        requests=0
        def log_message(self,*args): pass
        def do_GET(self):
            responses=config.get('responses',[])
            selected=responses[Handler.requests] if Handler.requests<len(responses) else 'ok'
            Handler.requests+=1
            with (home/'http-calls').open('a') as output:
                output.write(selected+'\\n')
            if selected=='error':
                self.send_response(503)
                self.end_headers()
                return
            data=json.dumps({'success':True,'data':{'object':'template','template':{
                'status':'locked' if (home/'lock').exists() else 'unlocked',
                'serverUrl':'https://vault.homelab.example.test',
                'userEmail':'other@work.example.test' if selected=='foreign' else 'agent@homelab.example.test'}}}).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.end_headers()
            self.wfile.write(data)
    HTTPServer(('127.0.0.1',config['port']),Handler).serve_forever()
else:
    sys.exit(2)
'''


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

    def calls(self):
        return [json.loads(line) for line in (self.home / 'calls').read_text().splitlines()]

    def wait_ready(self, process):
        deadline = time.monotonic() + 10
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
                             ['status', 'unlock', 'sync', 'serve'] if cached else ['status', 'config', 'login', 'sync', 'serve'])
            self.assertIsNone(calls[-1]['password'])
            self.assertEqual(calls[-1]['data'], str(self.home / 'client'))
            self.assertEqual((self.home / 'client').stat().st_mode & 0o777, 0o700)
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

    def test_foreign_cached_account_is_not_unlocked(self):
        self.fixture(cached=True, email='other@work.example.test')
        result = subprocess.run(self.prefix, env=self.env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual([call['args'] for call in self.calls()], [['status'], ['status'], ['status']])
        self.assertFalse(self.scope.exists())
        self.assert_quiet(result.stdout, result.stderr)

    def test_health_failures_reset_after_success_and_stop_after_three_consecutive_failures(self):
        responses = ['ok', 'error', 'error', 'ok', 'error', 'error', 'ok', 'error', 'error', 'error']
        self.fixture(cached=True, responses=responses)
        result = subprocess.run(self.prefix, env=self.env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual((self.home / 'http-calls').read_text().splitlines(), responses)
        self.assertFalse(self.scope.exists())
        self.assertFalse(Path('/proc/' + (self.home / 'server-pid').read_text()).exists())
        self.assert_quiet(result.stdout, result.stderr)

    def test_two_transient_failures_do_not_restart_the_client(self):
        responses = ['ok', 'error', 'error', 'ok', 'error', 'error', 'ok']
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
