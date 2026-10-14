import base64
import contextlib
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.response import addinfourl

import yaml

HUB = Path(__file__).resolve().parents[1]
ROOT = HUB.parents[1]
SCRIPT = HUB / 'reader/canary/canary.py'
HELPER = ROOT / '2-k3s/13.t3code/one/files/vault-restricted.py'
CLIENT = 'homelab.synthetic-client-canary-000000000000'
IDS = ['00000000-0000-4000-8000-' + str(n).zfill(12) for n in range(1, 7)]
FIXTURE = dict(collection_id=IDS[0], item_id=IDS[1], duplicate_item_ids=IDS[2:4],
               denied_collection_id=IDS[4], denied_item_id=IDS[5])
SERVICES = {'environment': 'homelab', 'services': ['vaultwarden', 'vault-secret', 'vault-attachment'],
            'vault_reader': 'restricted-v1'}


class CanaryTests(unittest.TestCase):
    def run_canary(self, leak=False, sse=False, reachable=False, refusal_status=400, broken_title=False,
                   accepted_write=None, write_effect=None, extra_visible=False, c5_refusal=False,
                   advertised_write=False, optimized=None):
        self.assertTrue(SCRIPT.exists(), 'manual canary script is missing')
        spec = importlib.util.spec_from_file_location('canary', SCRIPT)
        canary = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(canary)

        requests = []
        self.requests = requests
        write_seen = False
        write_calls = []
        self.write_calls = write_calls
        attachment_calls = 0
        write_arguments = {
            'vault_add': {'path': '/homelab-agent-canary/reader-canary-write-probe',
                          'password': 'synthetic-write-probe'},
            'vault_update': {'path': '/homelab-agent-canary/reader-canary', 'notes': 'synthetic-write-probe'},
            'vault_trash': {'path': '/homelab-agent-canary/reader-canary'},
            'vault_attach': {'path': '/homelab-agent-canary/reader-canary',
                             'filename': 'reader-canary-write-probe.txt', 'content_b64': 'c3ludGhldGlj'},
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                nonlocal write_seen, attachment_calls
                requests.append(self.path)
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                status, value = 200, {}
                if self.headers.get('Authorization') != 'Bearer ' + CLIENT:
                    status = 401
                elif self.path == '/vaultwarden':
                    method = body['method']
                    if method == 'initialize':
                        value = {'protocolVersion': '2025-06-18', 'capabilities': {},
                                 'serverInfo': {'name': 'synthetic', 'version': '0'}}
                    elif method == 'tools/list':
                        value = {'tools': [{'name': 'vault_list'}, {'name': 'vault_get'}]}
                        if advertised_write:
                            value['tools'].append({'name': 'vault_add'})
                    elif method == 'tools/call':
                        name, args = body['params']['name'], body['params']['arguments']
                        if name in write_arguments:
                            write_calls.append(name)
                        if name == 'vault_list':
                            value = [{'title': title} for title in ['reader-canary', 'reader-canary-duplicate',
                                                                   'reader-canary-duplicate']]
                            if extra_visible and not args:
                                value.append({'title': 'reader-canary-denied'})
                            if write_seen and write_effect == 'extra_item':
                                value.append({'title': 'reader-canary-write-probe'})
                        elif name == 'vault_get' and args['reference']['item_id'] == IDS[1]:
                            value = {'references': [{'collection_id': IDS[0], 'item_id': IDS[1]}],
                                     'title': 'reader-canary', 'type': 1, 'has_password': True,
                                     'custom_fields': ['API_TOKEN'], 'attachments': [{'id': 'synthetic', 'filename': 'canary.txt'}]}
                            if write_seen and write_effect == 'metadata':
                                value['custom_fields'] = ['API_TOKEN', 'WRITE_PROBE']
                            if leak:
                                value['password'] = 'canary-only-not-real'
                        elif name in write_arguments and args == write_arguments[name]:
                            write_seen = True
                            value = {'content': [{'type': 'text', 'text': 'synthetic write accepted'}]} if (
                                name == accepted_write) else {'isError': True, 'content': [
                                    {'type': 'text', 'text': 'Unknown tool'}]}
                        else:
                            message = ('invalid or unresolved collection reference' if refusal_status == 400 else
                                       'restricted reader unavailable') if name == 'vault_get' else 'Unknown tool'
                            value = {'isError': True, 'content': [{'type': 'text', 'text': message}]}
                        if 'isError' not in value:
                            value = {'content': [{'type': 'text', 'text': json.dumps(value)}]}
                    value = {'jsonrpc': '2.0', 'id': body['id'], 'result': value}
                elif self.path in ('/vault-secret', '/vault-attachment'):
                    reference = body.get('reference', {})
                    if (reference.get('collection_id') != IDS[0] or
                            reference.get('item_id') not in IDS[1:4] and reference.get('title') != 'reader-canary'):
                        status = refusal_status
                    elif broken_title and 'title' in reference:
                        status = 400
                    elif set(body) != ({'reference'} if self.path == '/vault-secret' else {'reference', 'attachment'}):
                        status = 400
                    elif self.path == '/vault-secret':
                        value = {'password': 'canary-only-not-real', 'username': None,
                                 'fields': {'API_TOKEN': 'synthetic-canary-token'}}
                    else:
                        attachment_calls += 1
                        if c5_refusal and attachment_calls == 2:
                            status = 503
                        value = {'filename': 'canary.txt', 'content_b64':
                                 base64.b64encode(b'reader-canary-synthetic-marker\n').decode()}
                else:
                    status = 404
                encoded = json.dumps(value).encode()
                event = sse and self.path == '/vaultwarden' and status == 200
                if event:
                    encoded = b'event: message\ndata: ' + encoded + b'\n\n'
                self.send_response(status)
                self.send_header('Content-Type', 'text/event-stream' if event else 'application/json')
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        with tempfile.TemporaryDirectory() as temp, ThreadingHTTPServer(('127.0.0.1', 0), Handler) as hub:
            thread = threading.Thread(target=hub.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(hub.shutdown)
            home = Path(temp)
            inputs = home / 'inputs'
            inputs.mkdir()
            for name, value in [('token', CLIENT), ('services.json', json.dumps(SERVICES)),
                                ('canary.json', json.dumps(FIXTURE))]:
                (inputs / name).write_text(value)
            with socket.socket() as closed:
                closed.bind(('127.0.0.1', 0))
                blocked_port = closed.getsockname()[1]
            address = 'http://127.0.0.1:' + str(hub.server_port)
            if optimized is not None:
                marker = home / 'helper-invoked'
                helper = [sys.executable, '-c',
                          'import pathlib,runpy; pathlib.Path(' + repr(str(marker)) + ').touch(); '
                          'runpy.run_path(' + repr(str(HELPER)) + ', run_name="__main__")']
                settings = {'INPUTS': str(inputs), 'HUB_URL': address, 'HELPER': helper,
                            'READER_URL': 'http://127.0.0.1:' + str(blocked_port) + '/vaultwarden'}
                bootstrap = ('import pathlib,runpy,sys; namespace=runpy.run_path(' + repr(str(SCRIPT)) + '); '
                             'settings=' + repr(settings) + '; settings["INPUTS"]=pathlib.Path(settings["INPUTS"]); '
                             'namespace["main"].__globals__.update(settings); sys.exit(namespace["main"]())')
                environment = dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE='1')
                environment.pop('PYTHONOPTIMIZE', None)
                if optimized == 'environment':
                    environment['PYTHONOPTIMIZE'] = '1'
                result = subprocess.run([sys.executable] + (['-O'] if optimized == 'flag' else []) +
                                        ['-c', bootstrap], env=environment, capture_output=True, text=True, timeout=30)
                self.assertFalse(marker.exists(), 'helper was invoked by an optimized canary')
                return result.returncode, result.stdout, result.stderr
            output, errors = io.StringIO(), io.StringIO()
            previous_umask = os.umask(0o077)
            try:
                with patch.object(canary, 'INPUTS', inputs), patch.object(canary, 'HUB_URL', address), \
                        patch.object(canary, 'HELPER', [sys.executable, str(HELPER)]), \
                        patch.object(canary, 'READER_URL', address + '/vaultwarden' if reachable else
                                     'http://127.0.0.1:' + str(blocked_port) + '/vaultwarden'), \
                        patch.dict(os.environ, {'HOME': str(home)}), \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    code = canary.main()
                self.assertEqual((home / 'services.json').stat().st_mode & 0o777, 0o600)
                return code, output.getvalue(), errors.getvalue()
            finally:
                os.umask(previous_umask)
                hub.shutdown()
                thread.join(3)

    def assert_quiet(self, output, errors):
        self.assertEqual(errors, '')
        for line in output.splitlines():
            self.assertRegex(line, r'^(PASS C(?:[1-9]|10|11)|FAIL C(?:[1-9]|10|11): [a-z ]+)$')
        for value in ['canary-only-not-real', 'synthetic-canary-token', 'reader-canary-synthetic-marker', CLIENT, *IDS]:
            self.assertNotIn(value, output + errors)
        self.assertEqual(len(output.splitlines()), 11)

    def test_optimized_interpreter_refused_before_requests_or_helper(self):
        for mode in ('environment', 'flag'):
            with self.subTest(mode=mode):
                code, output, errors = self.run_canary(optimized=mode)
                self.assert_quiet(output, errors)
                self.assertEqual(code, 1, output)
                self.assertEqual(output.splitlines(), [
                    'FAIL C' + str(n) + ': optimized interpreter refused' for n in range(1, 12)])
                self.assertEqual(self.requests, [])

    def test_complete_offline_canary_with_real_helper_json_and_sse(self):
        for sse in (False, True):
            with self.subTest(sse=sse):
                code, output, errors = self.run_canary(sse=sse)
                self.assert_quiet(output, errors)
                self.assertEqual(code, 0, output)
                self.assertEqual(output.splitlines(), ['PASS C' + str(n) for n in range(1, 12)])
                self.assertEqual(self.requests.count('/vaultwarden'), 22)
                self.assertEqual(self.requests.count('/vault-secret'), 14)
                self.assertEqual(self.requests.count('/vault-attachment'), 2)

    def test_unavailable_reader_is_not_duplicate_refusal(self):
        code, output, errors = self.run_canary(refusal_status=503)
        self.assert_quiet(output, errors)
        self.assertNotEqual(code, 0)
        self.assertIn('FAIL C6:', output)
        self.assertIn('FAIL C7:', output)

    def test_broken_title_lookup_fails_positive_control(self):
        code, output, errors = self.run_canary(broken_title=True)
        self.assert_quiet(output, errors)
        self.assertNotEqual(code, 0)
        self.assertIn('FAIL C6:', output)

    def test_helper_refusal_does_not_prove_a_child_assertion_ran(self):
        code, output, errors = self.run_canary(c5_refusal=True)
        self.assert_quiet(output, errors)
        self.assertNotEqual(code, 0)
        self.assertIn('FAIL C5:', output)

    def test_extra_unfiltered_item_fails_collection_scope(self):
        code, output, errors = self.run_canary(extra_visible=True)
        self.assert_quiet(output, errors)
        self.assertNotEqual(code, 0)
        self.assertIn('FAIL C2:', output)

    def test_real_write_arguments_fail_if_any_write_tool_accepts(self):
        for name in ('vault_add', 'vault_update', 'vault_trash', 'vault_attach'):
            with self.subTest(name=name):
                code, output, errors = self.run_canary(accepted_write=name)
                self.assert_quiet(output, errors)
                self.assertNotEqual(code, 0)
                self.assertIn('FAIL C9:', output)

    def test_advertised_write_tool_blocks_all_write_probes(self):
        code, output, errors = self.run_canary(advertised_write=True, accepted_write='vault_add')
        self.assert_quiet(output, errors)
        self.assertNotEqual(code, 0)
        self.assertIn('FAIL C9:', output)
        self.assertEqual(self.write_calls, [])

    def test_write_refusal_must_leave_fixture_list_and_metadata_unchanged(self):
        for effect in ('extra_item', 'metadata'):
            with self.subTest(effect=effect):
                code, output, errors = self.run_canary(write_effect=effect)
                self.assert_quiet(output, errors)
                self.assertNotEqual(code, 0)
                self.assertIn('FAIL C9:', output)

    def test_metadata_leak_and_reachable_reader_fail_without_logging_values(self):
        code, output, errors = self.run_canary(leak=True, reachable=True)
        self.assert_quiet(output, errors)
        self.assertNotEqual(code, 0)
        self.assertIn('FAIL C3:', output)
        self.assertIn('FAIL C10:', output)

    def test_timeout_budgets_cover_reader_and_helper_operations(self):
        spec = importlib.util.spec_from_file_location('canary_timeouts', SCRIPT)
        canary = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(canary)
        self.assertGreater(canary.HTTP_TIMEOUT, 125)
        with patch.object(canary.subprocess, 'run') as run:
            canary.helper({'collection_id': IDS[0], 'item_id': IDS[1]}, 'pass', attachment=True)
        command = run.call_args.args[0]
        child_timeout = float(command[command.index('--timeout') + 1])
        self.assertGreaterEqual(child_timeout, 0.1)
        self.assertLessEqual(child_timeout, 120)
        self.assertGreater(run.call_args.kwargs['timeout'], 2 * 130 + child_timeout)

    def test_http_calls_have_total_deadlines(self):
        spec = importlib.util.spec_from_file_location('canary_timeouts', SCRIPT)
        canary = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(canary)
        hub = canary.Hub(CLIENT)
        def slow_open(*args, **kwargs):
            time.sleep(0.2)
            headers = HTTPMessage()
            headers['Content-Type'] = 'application/json'
            return addinfourl(io.BytesIO(b'{}'), headers, 'http://127.0.0.1', 200)

        with patch.object(canary, 'HTTP_TIMEOUT', 0.05, create=True), \
                patch.object(hub.opener, 'open', side_effect=slow_open):
            with self.assertRaises(TimeoutError):
                hub.request('/vaultwarden', {}, CLIENT)

    def test_helper_calls_have_total_deadlines(self):
        spec = importlib.util.spec_from_file_location('canary_timeouts', SCRIPT)
        canary = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(canary)
        with tempfile.TemporaryDirectory() as temp:
            sleeper = Path(temp) / 'synthetic-helper.py'
            sleeper.write_text('import time\ntime.sleep(0.2)\n')
            with patch.object(canary, 'HELPER', [sys.executable, str(sleeper)]), \
                    patch.object(canary, 'HELPER_TIMEOUT', 0.05, create=True):
                with self.assertRaises(subprocess.TimeoutExpired):
                    canary.helper({'collection_id': IDS[0], 'item_id': IDS[1]}, 'pass')

    def test_manual_job_security_image_and_separation(self):
        path = HUB / 'reader/canary'
        self.assertTrue((path / 'kustomization.yaml').exists(), 'manual canary overlay is missing')
        docs = list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(path)])))
        job = next(doc for doc in docs if doc['kind'] == 'Job')
        self.assertEqual(job['metadata']['name'], 'vault-reader-canary')
        self.assertEqual(job['metadata']['namespace'], 'mcp-hub-homelab')
        self.assertEqual(job['spec']['backoffLimit'], 0)
        spec = importlib.util.spec_from_file_location('canary_timeouts', SCRIPT)
        canary = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(canary)
        self.assertGreater(job['spec']['activeDeadlineSeconds'],
                           8 * canary.HELPER_TIMEOUT + 28 * canary.HTTP_TIMEOUT + 3)
        self.assertLessEqual(job['spec']['activeDeadlineSeconds'], 6600)
        self.assertGreater(job['spec']['ttlSecondsAfterFinished'], 0)
        template = job['spec']['template']
        self.assertEqual(template['metadata']['labels']['app'], 'vault-reader-canary')
        pod = template['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(pod['restartPolicy'], 'Never')
        self.assertEqual(pod['securityContext']['runAsUser'], 1000)
        self.assertEqual(pod['securityContext']['runAsGroup'], 1000)
        self.assertTrue(pod['securityContext']['runAsNonRoot'])
        self.assertEqual(pod['securityContext']['seccompProfile']['type'], 'RuntimeDefault')
        container = pod['containers'][0]
        security = container['securityContext']
        self.assertTrue(security['readOnlyRootFilesystem'])
        self.assertFalse(security['allowPrivilegeEscalation'])
        self.assertEqual(security['capabilities']['drop'], ['ALL'])
        self.assertEqual(container['command'], ['python3', '/canary/canary.py'])
        runtime = next(doc for doc in yaml.safe_load_all(
            (ROOT / '2-k3s/13.t3code-homelab/statefulset.yaml').read_text()) if doc['kind'] == 'StatefulSet')
        image = next(c['image'] for c in runtime['spec']['template']['spec']['containers'] if c['name'] == 't3')
        self.assertEqual(container['image'], image)
        self.assertIn('limits', container['resources'])
        volumes = {v['name']: v for v in pod['volumes']}
        self.assertEqual(volumes['inputs']['secret']['secretName'], 'vault-canary-client')
        self.assertIn('emptyDir', volumes['home'])
        self.assertIn('emptyDir', volumes['tmp'])
        for overlay in ['reader/homelab', 'instances/homelab', 'instances/work']:
            rendered = subprocess.check_output(['kustomize', 'build', str(HUB / overlay)], text=True)
            self.assertNotIn('vault-reader-canary', rendered)
        apps = list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build',
                                      str(ROOT / '2-k3s/11.argocd/apps')])))
        self.assertFalse(any(doc['spec']['source'].get('path') == '2-k3s/23.mcp-hub/reader/canary' for doc in apps))


if __name__ == '__main__':
    unittest.main()
