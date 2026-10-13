import base64
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import uvicorn

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'files'),
               str(Path(__file__).resolve().parents[2] / '13.t3code/one/files')]
import mcp_hub
import vaultwarden_reader_http as adapter
import test_vaultwarden_reader as fixture

HELPER = Path(__file__).resolve().parents[2] / '13.t3code/one/files/vault-restricted.py'
CLIENT = 'homelab.synthetic-client-grant-0000000000000000'
META = 'synthetic-independent-metadata-grant-00000000'
BROKER = 'synthetic-independent-broker-grant-00000000000'


@contextlib.contextmanager
def serve(app):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(64)
    address = 'http://127.0.0.1:' + str(listener.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise AssertionError('fixture server did not start')
            time.sleep(0.01)
        yield address
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
        if thread.is_alive():
            raise AssertionError('fixture server did not stop')


class CountRequests:
    def __init__(self, app):
        self.app = app
        self.paths = []

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            self.paths.append(scope['path'])
        await self.app(scope, receive, send)


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.ReaderTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root = self.case.root
        self.root.chmod(0o700)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.logs = io.StringIO()
        handler = logging.StreamHandler(self.logs)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        self.connector = CountRequests(adapter.http_app(self.case.reader, META, BROKER))
        connector_address = self.stack.enter_context(serve(self.connector))
        self.services = {'environment': 'homelab', 'services': ['vaultwarden', 'vault-secret', 'vault-attachment'],
                         'vault_reader': 'restricted-v1'}
        self.services_file = self.root / 'services.json'
        self.write_services()
        self.credentials = {'clients': {'synthetic': hashlib.sha256(CLIENT.encode()).hexdigest(),
                                       'work': hashlib.sha256(b'work.synthetic-cross-environment').hexdigest()},
                            'upstreams': {name: {'url': connector_address + path, 'protocol': 'restricted-v1',
                                               'token': META if name == 'vaultwarden' else BROKER}
                                          for name, path in [('vaultwarden', '/vaultwarden'),
                                                             ('vault-secret', '/secret'),
                                                             ('vault-attachment', '/attachment')]}}
        self.hub_file = self.root / 'hub.json'
        self.hub_file.write_text(json.dumps(self.credentials))
        self.hub_file.chmod(0o600)
        self.hub = CountRequests(self.build_hub())
        hub_address = self.stack.enter_context(serve(self.hub))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('MCP_HUB_', 'VAULT_', 'BW_'))}
        self.env.update(MCP_HUB_INSTANCE='homelab', T3_RUNTIME_VARIANT='homelab',
                        MCP_HUB_URL=hub_address, MCP_HUB_TOKEN=CLIENT,
                        MCP_HUB_SERVICES_FILE=str(self.services_file), HOME=str(self.root),
                        VAULT_STALE='synthetic-stale-secret', BW_SESSION='synthetic-stale-session',
                        BW_PASSWORD='synthetic-stale-password', HTTP_PROXY='http://127.0.0.1:1')
        self.marker = self.root / 'child.json'
        self.secrets = ['synthetic-password-secret', 'synthetic-field-secret', 'synthetic-user-secret',
                        'synthetic-attachment-bytes', 'synthetic-stale-secret', 'synthetic-stale-session',
                        'synthetic-upstream-secret', CLIENT, META, BROKER]

    def build_hub(self):
        with patch.object(mcp_hub, 'upstreams', side_effect=AssertionError('legacy fallback')):
            return mcp_hub.instance_app('homelab', self.services_file, self.hub_file)

    def write_services(self):
        self.services_file.write_text(json.dumps(self.services))
        self.services_file.chmod(0o600)

    def argv(self, options=None, code=None, mode='run', reference=None):
        reference = fixture.REF if reference is None else reference
        args = [mode, '--collection-id', reference['collection_id']]
        args += ['--item-id', reference['item_id']] if 'item_id' in reference else ['--title', reference['title']]
        args += ['--password-env', 'VAULT_PASSWORD'] if options is None else options
        if mode == 'run':
            args += ['--', sys.executable, '-c', code or 'import pathlib; pathlib.Path(' + repr(str(self.marker)) + ').touch()']
        return args

    def launch(self, args, env=None, request_timeout=None):
        prefix = [sys.executable, str(HELPER)]
        if request_timeout is not None:
            script = 'import sys,importlib;sys.path.insert(0,' + repr(str(HELPER.parent)) + ');h=importlib.import_module("vault-restricted");h.REQUEST_TIMEOUT=' + str(request_timeout) + ';sys.exit(h.main(sys.argv[1:]))'
            prefix = [sys.executable, '-c', script]
        return subprocess.Popen(prefix + args, env=self.env if env is None else env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def run_helper(self, args, env=None, expected=0, request_timeout=None):
        with self.launch(args, env, request_timeout) as process:
            stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, expected, (stdout, stderr))
        self.assert_quiet(stdout, stderr)
        return stdout.decode(), stderr.decode()

    def assert_quiet(self, stdout=b'', stderr=b''):
        for secret in self.secrets:
            self.assertNotIn(secret, (stdout + stderr).decode())
            self.assertNotIn(secret, self.logs.getvalue())

    def refused(self, args, env=None, before_http=False):
        count = len(self.hub.paths)
        stdout, stderr = self.run_helper(args, env, expected=1)
        self.assertEqual(stdout, '')
        self.assertEqual(stderr, 'vault-restricted: request or execution refused\n')
        self.assertFalse(self.marker.exists())
        if before_http:
            self.assertEqual(len(self.hub.paths), count)

    def test_password_custom_fields_and_username_through_real_servers(self):
        code = '''import os,pathlib
assert os.environ['VAULT_PASSWORD']=='synthetic-password-secret'
assert os.environ['VAULT_API_TOKEN']=='synthetic-field-secret'
assert os.environ['VAULT_USER']=='synthetic-user-secret'
assert not any(k.startswith(('BW_','MCP_HUB_')) for k in os.environ)
assert 'VAULT_STALE' not in os.environ
pathlib.Path(%r).touch()
''' % str(self.marker)
        self.run_helper(self.argv(['--password-env', 'VAULT_PASSWORD', '--username-env', 'VAULT_USER',
                                   '--field', 'api token=VAULT_API_TOKEN'], code))
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.hub.paths, ['/vault-secret'])
        self.assertEqual(self.connector.paths, ['/secret'])

    def test_empty_strings_are_exact_values(self):
        self.case.scenario['items'][0]['login']['password'] = ''
        self.case.scenario['items'][0]['fields'][0]['value'] = ''
        self.case.save()
        self.run_helper(self.argv(['--password-env', 'VAULT_PASSWORD', '--field', 'api token=VAULT_TOKEN'],
                                  "import os; assert os.environ['VAULT_PASSWORD']==os.environ['VAULT_TOKEN']==''"))

    def test_missing_and_null_values_do_not_start_child(self):
        for changes, options in [({'login': {}}, None), ({'fields': []}, ['--field', 'missing=VAULT_TOKEN']),
                                 ({'fields': [{'name': 'api token', 'value': None}]}, ['--field', 'api token=VAULT_TOKEN'])]:
            with self.subTest(changes=changes):
                self.case.scenario['items'][0].update(changes)
                self.case.save()
                self.refused(self.argv(options))

    def test_field_mapping_collisions_and_invalid_names_refuse_before_http(self):
        for options in [['--password-env', 'VAULT_TOKEN', '--field', 'api token=VAULT_TOKEN'],
                        ['--field', 'api token=VAULT_TOKEN', '--field', 'other=VAULT_TOKEN'],
                        ['--field', 'api token=PATH'], ['--field', 'api token=VAULT_bad'],
                        ['--field', '=VAULT_TOKEN'], ['--field', 'api token'],
                        ['--password-env', 'VAULT_FILE', '--attachment', 'demo.txt', '--attachment-env', 'VAULT_FILE']]:
            with self.subTest(options=options):
                self.refused(self.argv(options), before_http=True)

    def test_work_mixed_missing_configuration_and_no_legacy_fallback(self):
        for changes in [{'MCP_HUB_INSTANCE': 'work'}, {'MCP_HUB_INSTANCE': 'mixed'},
                        {'T3_RUNTIME_VARIANT': 'work'}, {'T3_RUNTIME_VARIANT': 'mixed'},
                        {'MCP_HUB_TOKEN': 'work.synthetic-cross-environment'}, {'MCP_HUB_TOKEN': 'legacy.synthetic'},
                        {'MCP_HUB_URL': ''}, {'MCP_HUB_SERVICES_FILE': '/absent'}, {'MCP_HUB_INSTANCE': None},
                        {'MCP_HUB_TOKEN': None}, {'MCP_HUB_URL': None}]:
            with self.subTest(changes=changes):
                env = dict(self.env)
                for key, value in changes.items():
                    if value is None:
                        env.pop(key, None)
                    else:
                        env[key] = value
                self.refused(self.argv(), env, before_http=True)
        marker = self.root / '.config/t3-runtime/environment'
        marker.parent.mkdir(parents=True)
        for value in ['work\n', 'mixed\n']:
            marker.write_text(value)
            self.refused(self.argv(), before_http=True)

    def test_wrong_client_token_does_not_reach_connector(self):
        env = dict(self.env, MCP_HUB_TOKEN='homelab.synthetic-unregistered-grant')
        self.refused(self.argv(), env)
        self.assertEqual(self.connector.paths, [])
        self.assertEqual(self.case.calls(), [])

    def test_service_selection_requires_restricted_marker_and_matching_environment(self):
        for services in [{'environment': 'homelab', 'services': ['searxng']},
                         {'environment': 'work', 'services': ['searxng']},
                         {'environment': 'homelab', 'services': ['vault-secret']}]:
            self.services = services
            self.write_services()
            self.refused(self.argv(), before_http=True)

    def test_disabled_hub_route_does_not_start_child(self):
        self.services['services'] = ['vaultwarden']
        self.write_services()
        self.credentials['upstreams'] = {'vaultwarden': self.credentials['upstreams']['vaultwarden']}
        self.hub_file.write_text(json.dumps(self.credentials))
        disabled = CountRequests(self.build_hub())
        url = self.stack.enter_context(serve(disabled))
        self.services['services'].append('vault-secret')
        self.write_services()
        self.refused(self.argv(), dict(self.env, MCP_HUB_URL=url))
        self.assertEqual(disabled.paths, ['/vault-secret'])
        self.assertEqual(self.case.calls(), [])

    def test_scope_and_ambiguity_denial_no_child(self):
        outside = fixture.item(fixture.OTHER_ITEM)
        outside['collectionIds'] = [fixture.OTHER_COLLECTION]
        self.case.scenario['items'].append(outside)
        self.case.save()
        self.refused(self.argv(reference={'collection_id': fixture.COLLECTION, 'item_id': fixture.OTHER_ITEM}))
        outside['collectionIds'] = [fixture.COLLECTION]
        self.case.save()
        self.refused(self.argv(reference={'collection_id': fixture.COLLECTION, 'title': 'demo'}))

    def test_missing_and_revoked_grants_after_success_fail_closed(self):
        self.run_helper(self.argv())
        self.marker.unlink()
        self.case.scenario['collections'] = []
        self.case.save()
        self.refused(self.argv())
        calls = len(self.case.calls())
        self.refused(self.argv())
        self.assertEqual(len(self.case.calls()), calls)

    def test_attachment_grant_revocation_after_success_fails_closed(self):
        target = self.root / 'first.txt'
        options = ['--attachment', 'demo.txt', '--destination', str(target)]
        self.run_helper(self.argv(options, mode='download'))
        self.case.scenario['collections'] = []
        self.case.save()
        self.refused(self.argv(['--attachment', 'demo.txt', '--attachment-env', 'VAULT_FILE']))
        second = self.root / 'second.txt'
        self.refused(self.argv(['--attachment', 'demo.txt', '--destination', str(second)], mode='download'))
        self.assertFalse(second.exists())
        self.assertEqual(target.read_bytes(), b'synthetic-attachment-bytes\0\xff')

    def test_composed_attachment_ambiguity_refuses_without_content_read(self):
        self.case.scenario['items'][0]['attachments'].append({'id': 'b' * 32, 'fileName': 'demo.txt'})
        self.case.save()
        self.refused(self.argv(['--attachment', 'demo.txt', '--attachment-env', 'VAULT_FILE']))
        self.assertFalse(any(c['args'][0] == 'get' for c in self.case.calls()))

    def test_refresh_failure_after_success_never_uses_old_password(self):
        self.run_helper(self.argv())
        self.marker.unlink()
        self.case.scenario['fail'] = 'sync'
        self.case.save()
        self.refused(self.argv())

    def test_quiet_direct_encoded_split_partial_and_large_output(self):
        code = '''import os,sys,base64,pathlib
secret=os.environ['VAULT_PASSWORD']
print(secret)
print(base64.b64encode(secret.encode()).decode())
for c in secret: os.write(2,c.encode())
print(secret[:7],file=sys.stderr)
os.write(1,b'x'*1000000)
pathlib.Path(%r).touch()
''' % str(self.marker)
        stdout, stderr = self.run_helper(self.argv(code=code))
        self.assertEqual(stdout, 'vault-restricted: command finished; exit code 0\n')
        self.assertEqual(stderr, '')
        self.assertTrue(self.marker.exists())

    def test_argv_has_no_implicit_shell_and_exit_code_is_preserved(self):
        args = self.argv(code="import sys; assert sys.argv[1]=='$VAULT_PASSWORD; echo unsafe'; sys.exit(7)")
        args.append('$VAULT_PASSWORD; echo unsafe')
        stdout, stderr = self.run_helper(args, expected=7)
        self.assertEqual(stdout, 'vault-restricted: command finished; exit code 7\n')
        self.assertEqual(stderr, '')

    def test_temporary_attachment_password_and_field_consumption_and_cleanup(self):
        code = '''import os,pathlib,stat,sys,base64
p=pathlib.Path(os.environ['VAULT_FILE'])
assert p.read_bytes()==b'synthetic-attachment-bytes\\0\\xff'
assert stat.S_IMODE(p.stat().st_mode)==0o600
assert stat.S_IMODE(p.parent.stat().st_mode)==0o700
assert os.environ['VAULT_PASSWORD']=='synthetic-password-secret'
assert os.environ['VAULT_TOKEN']=='synthetic-field-secret'
os.write(1,p.read_bytes());print(base64.b64encode(p.read_bytes()).decode())
for c in p.read_bytes(): os.write(2,bytes([c]))
os.write(1,p.read_bytes()[:4])
pathlib.Path(%r).write_text(str(p))
''' % str(self.marker)
        stdout, stderr = self.run_helper(self.argv(['--password-env', 'VAULT_PASSWORD', '--field', 'api token=VAULT_TOKEN',
                                                  '--attachment', 'demo.txt', '--attachment-env', 'VAULT_FILE'], code))
        self.assertEqual(stdout, 'vault-restricted: command finished; exit code 0\n')
        self.assertEqual(stderr, '')
        path = Path(self.marker.read_text())
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())
        self.assertEqual(self.connector.paths, ['/secret', '/attachment'])

    def test_persistent_download_private_and_explicit_destination(self):
        target = self.root / 'download.txt'
        stdout, stderr = self.run_helper(self.argv(['--attachment', 'demo.txt', '--destination', str(target)], mode='download'))
        self.assertEqual(json.loads(stdout), {'status': 'saved', 'destination': str(target), 'bytes': 28})
        self.assertEqual(stderr, '')
        self.assertEqual(target.read_bytes(), b'synthetic-attachment-bytes\0\xff')
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_overwrite_symlink_traversal_and_public_directory_refused_before_fetch(self):
        existing = self.root / 'existing'
        existing.write_text('keep')
        linked = self.root / 'linked'
        linked.symlink_to(existing)
        directory_link = self.root / 'linked-dir'
        directory_link.symlink_to(self.root, target_is_directory=True)
        public = self.root / 'public'
        public.mkdir(mode=0o755)
        for target in [existing, linked, directory_link / 'new', public / 'new',
                       str(self.root) + '/../escape', 'relative.txt', str(self.root) + '/./new']:
            with self.subTest(target=target):
                self.refused(self.argv(['--attachment', 'demo.txt', '--destination', str(target)], mode='download'), before_http=True)
        self.assertEqual(existing.read_text(), 'keep')

    def test_attachment_fetch_failure_never_starts_child(self):
        self.refused(self.argv(['--password-env', 'VAULT_PASSWORD', '--attachment', 'absent.txt',
                               '--attachment-env', 'VAULT_FILE']))
        self.assertFalse(any(c['args'][0] == 'get' for c in self.case.calls()))

    def wait_marker(self, process):
        deadline = time.monotonic() + 5
        while not self.marker.exists():
            if process.poll() is not None or time.monotonic() > deadline:
                raise AssertionError('child fixture did not start')
            time.sleep(0.01)
        return json.loads(self.marker.read_text())

    def assert_dead(self, pid):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                status = Path('/proc/' + str(pid) + '/stat').read_text()
            except FileNotFoundError:
                return
            if status.split(') ')[1].startswith('Z'):
                return
            time.sleep(0.02)
        self.fail('process group descendant still alive')

    def sleeping_code(self, exit_parent=False):
        return '''import os,subprocess,pathlib,json,time
p=subprocess.Popen([%r,'-c','import time; time.sleep(30)'])
pathlib.Path(%r).write_text(json.dumps({'parent':os.getpid(),'descendant':p.pid,'file':os.environ.get('VAULT_FILE')}))
%s
''' % (sys.executable, str(self.marker), '' if exit_parent else 'time.sleep(30)')

    def test_timeout_cleans_process_group_and_command_attachment(self):
        stdout, stderr = self.run_helper(self.argv(['--attachment', 'demo.txt', '--attachment-env', 'VAULT_FILE',
                                                  '--timeout', '0.3'], self.sleeping_code()), expected=124)
        data = json.loads(self.marker.read_text())
        self.assert_dead(data['parent'])
        self.assert_dead(data['descendant'])
        self.assertFalse(Path(data['file']).parent.exists())
        self.assertEqual(stdout, 'vault-restricted: command timed out; exit code 124\n')
        self.assertEqual(stderr, '')

    def test_sigterm_and_sigint_cancellation_cleanup(self):
        for sig in [signal.SIGTERM, signal.SIGINT]:
            with self.subTest(signal=sig):
                self.marker.unlink(missing_ok=True)
                with self.launch(self.argv(['--attachment', 'demo.txt', '--attachment-env', 'VAULT_FILE'], self.sleeping_code())) as process:
                    data = self.wait_marker(process)
                    process.send_signal(sig)
                    stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 130)
                self.assert_quiet(stdout, stderr)
                self.assert_dead(data['parent'])
                self.assert_dead(data['descendant'])
                self.assertFalse(Path(data['file']).parent.exists())

    def test_descendants_cleaned_after_successful_parent_exit(self):
        self.run_helper(self.argv(code=self.sleeping_code(exit_parent=True)))
        data = json.loads(self.marker.read_text())
        self.assert_dead(data['parent'])
        self.assert_dead(data['descendant'])

    def test_child_resource_limits_and_closed_output_timeout(self):
        code = '''import resource,os,time
assert resource.getrlimit(resource.RLIMIT_CORE)==(0,0)
os.close(1);os.close(2);time.sleep(30)
'''
        self.run_helper(self.argv(['--password-env', 'VAULT_PASSWORD', '--timeout', '0.3'], code), expected=124)

    @contextlib.contextmanager
    def fake_response(self, body, status=200, headers=None, delay=0, drip=False, on_request=None):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(inner):
                seen.append(inner.path)
                inner.rfile.read(int(inner.headers.get('Content-Length', 0)))
                if on_request is not None:
                    on_request()
                time.sleep(delay)
                inner.send_response(status)
                for key, value in (headers or {'Content-Type': 'application/json'}).items():
                    inner.send_header(key, value)
                inner.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    if drip:
                        for byte in body:
                            inner.wfile.write(bytes([byte]))
                            inner.wfile.flush()
                            time.sleep(0.03)
                    else:
                        inner.wfile.write(body)
            def log_message(inner, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield dict(self.env, MCP_HUB_URL='http://127.0.0.1:' + str(server.server_port)), seen
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_remote_redirect_error_schema_encoding_and_size_failures(self):
        valid = json.dumps({'password': 'synthetic-password-secret', 'username': None, 'fields': {}}).encode()
        cases = [(b'synthetic-password-secret', 500, None), (valid, 201, None),
                 (valid, 302, {'Location': self.env['MCP_HUB_URL'] + '/vault-secret'}),
                 (b'{', 200, None), (b'[]', 200, None),
                 (b'{"password":"synthetic-password-secret","password":"other","username":null,"fields":{}}', 200, None),
                 (b'{"password":42,"username":null,"fields":{}}', 200, None),
                 (b'{"password":"x\\u0000y","username":null,"fields":{}}', 200, None),
                 (valid, 200, {'Content-Type': 'text/plain'}),
                 (valid, 200, {'Content-Type': 'application/json', 'Content-Encoding': 'gzip'}),
                 (valid, 200, {'Content-Type': 'application/json', 'Content-Length': str(12*1024*1024+1)}),
                 (b'x'*(12*1024*1024+1), 200, None)]
        count = len(self.hub.paths)
        for body, status, headers in cases:
            with self.subTest(status=status, size=len(body), headers=headers), self.fake_response(body, status, headers) as (env, seen):
                self.refused(self.argv(), env)
                self.assertEqual(seen, ['/vault-secret'])
        self.assertEqual(len(self.hub.paths), count)

    def test_remote_secret_schema_and_environment_size_bounds(self):
        payloads = [{'password': 'x' * 16385, 'username': None, 'fields': {}},
                    {'password': 'x', 'username': None, 'fields': {'bad': []}},
                    {'password': 'x', 'username': None, 'fields': {'bad': 'x\0y'}},
                    {'password': 'x', 'username': None, 'fields': {'f' + str(n): 'x' for n in range(129)}},
                    {'password': 'x', 'username': None},
                    {'password': 'x', 'username': None, 'fields': {}, 'unexpected': 'x'}]
        for value in payloads:
            with self.subTest(keys=list(value)), self.fake_response(json.dumps(value).encode()) as (env, _):
                self.refused(self.argv(), env)
        value = {'password': 'x' * 16384, 'username': None, 'fields': {str(n): 'x' * 16384 for n in range(4)}}
        with self.fake_response(json.dumps(value).encode()) as (env, _):
            options = ['--password-env', 'VAULT_PASSWORD']
            for n in range(4):
                options += ['--field', str(n) + '=VAULT_TOKEN_' + str(n)]
            self.refused(self.argv(options), env)

    def test_persistent_destination_race_does_not_overwrite(self):
        target = self.root / 'raced.txt'
        value = {'filename': 'demo.txt', 'content_b64': base64.b64encode(b'synthetic-attachment-bytes').decode()}
        with self.fake_response(json.dumps(value).encode(), on_request=lambda: target.write_text('keep')) as (env, _):
            self.refused(self.argv(['--attachment', 'demo.txt', '--destination', str(target)], mode='download'), env)
        self.assertEqual(target.read_text(), 'keep')

    def test_private_configuration_bounds(self):
        for value in [b'x' * 16385, b'{"environment":"homelab","environment":"work","services":[]}']:
            self.services_file.write_bytes(value)
            self.refused(self.argv(), before_http=True)
        self.write_services()
        self.services_file.chmod(0o644)
        self.refused(self.argv(), before_http=True)
        self.services_file.unlink()
        os.mkfifo(self.services_file, 0o600)
        self.refused(self.argv(), before_http=True)

    def test_remote_attachment_schema_base64_filename_and_decoded_size_failures(self):
        values = [{'filename': '../escape', 'content_b64': 'eA=='},
                  {'filename': 'a\\b', 'content_b64': 'eA=='}, {'filename': '.hidden', 'content_b64': 'eA=='},
                  {'filename': 'bad name', 'content_b64': 'eA=='}, {'filename': 'bad.', 'content_b64': 'eA=='},
                  {'filename': 'demo.txt', 'content_b64': 'invalid!'},
                  {'filename': 'demo.txt', 'content_b64': 'eB=='},
                  {'filename': 'demo.txt', 'content_b64': base64.b64encode(b'x'*(8*1024*1024+1)).decode()},
                  {'filename': 'demo.txt', 'content_b64': 'eA==', 'extra': 'unsafe'}]
        for value in values:
            with self.subTest(filename=value['filename']), self.fake_response(json.dumps(value).encode()) as (env, _):
                self.refused(self.argv(['--attachment', 'demo.txt', '--attachment-env', 'VAULT_FILE']), env)

    def test_slow_drip_cannot_extend_total_request_deadline(self):
        value = json.dumps({'password': 'synthetic-password-secret', 'username': None, 'fields': {}}).encode()
        with self.fake_response(value, drip=True) as (env, _):
            started = time.monotonic()
            self.run_helper(self.argv(), env, expected=1, request_timeout=0.15)
            self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(self.marker.exists())

    def test_request_total_deadline_and_fetch_cancellation(self):
        with self.fake_response(b'synthetic-password-secret', delay=0.6) as (env, _):
            started = time.monotonic()
            self.run_helper(self.argv(), env, expected=1, request_timeout=0.15)
            self.assertLess(time.monotonic() - started, 0.5)
        with self.fake_response(b'synthetic-password-secret', delay=0.6) as (env, seen):
            with self.launch(self.argv(), env) as process:
                deadline = time.monotonic() + 3
                while not seen and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(seen)
                process.terminate()
                stdout, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 130)
            self.assert_quiet(stdout, stderr)
        self.assertFalse(self.marker.exists())

    def test_invalid_reference_command_timeout_url_and_raw_output_option(self):
        for changes in [['run', '--collection-id', 'invalid', '--item-id', fixture.ITEM],
                        self.argv() + ['--raw-output'], self.argv(['--password-env', 'VAULT_PASSWORD', '--timeout', 'nan']),
                        self.argv(['--password-env', 'VAULT_PASSWORD', '--timeout', '121']),
                        self.argv(['--password-env', 'VAULT_PASSWORD', '--timeout', '0'])]:
            if '--raw-output' in changes:
                changes = self.argv(['--password-env', 'VAULT_PASSWORD', '--raw-output'])
            self.refused(changes, before_http=True)
        for url in ['https://user:password@example.test', 'http://work.example.test',
                    'https://example.test/?token=synthetic-password-secret', 'https://example.test/#fragment',
                    'https://example.test/path', 'https://example.test\\@elsewhere.test']:
            self.refused(self.argv(), dict(self.env, MCP_HUB_URL=url), before_http=True)


if __name__ == '__main__':
    unittest.main()
