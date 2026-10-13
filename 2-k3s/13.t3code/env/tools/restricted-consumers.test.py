import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
installed = Path('/usr/local/lib/t3-vault-restricted')
files = installed if installed.is_dir() else ROOT / 'one/files'
sys.path.insert(0, str(files))
spec = importlib.util.spec_from_file_location('restricted_helper', files / 'vault-restricted.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class ConsumerTests(unittest.TestCase):
    def test_actual_node_and_kubectl_start_quietly(self):
        for command in [['node', '-e', 'console.log("synthetic-output");process.exit(0)'],
                        ['kubectl', 'version', '--client=true', '-o=json']]:
            with self.subTest(command=command[0]):
                self.assertIsNotNone(shutil.which(command[0]), 'required runtime consumer missing')
                self.assertEqual(subprocess.run(command, stdin=subprocess.DEVNULL,
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                               timeout=10).returncode, 0)
                self.assertEqual(helper.quiet_command(command, {}, 10), 0)

    def test_actual_consumers_through_helper_entrypoint(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                if self.path != '/vault-secret' or self.headers['Authorization'] != 'Bearer homelab.synthetic-consumer-000000000000':
                    self.send_error(401)
                    return
                data = json.dumps({'password': 'synthetic-consumer-password', 'username': None, 'fields': {}}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                services = Path(directory) / 'services.json'
                services.write_text(json.dumps({'environment': 'homelab', 'services': ['vault-secret'],
                                                'vault_reader': 'restricted-v1'}))
                services.chmod(0o600)
                env = {'PATH': os.environ['PATH'], 'HOME': directory, 'T3_RUNTIME_VARIANT': 'homelab',
                       'MCP_HUB_INSTANCE': 'homelab', 'MCP_HUB_URL': 'http://127.0.0.1:' + str(server.server_port),
                       'MCP_HUB_TOKEN': 'homelab.synthetic-consumer-000000000000', 'MCP_HUB_SERVICES_FILE': str(services)}
                prefix = ['/usr/local/bin/vault-restricted'] if installed.is_dir() else [sys.executable, str(files / 'vault-restricted.py')]
                for command in [['node', '-e', 'if(process.env.VAULT_PASSWORD!=="synthetic-consumer-password")process.exit(7);console.log(process.env.VAULT_PASSWORD)'],
                                ['kubectl', 'version', '--client=true', '-o=json']]:
                    with self.subTest(command=command[0]):
                        result = subprocess.run(prefix + ['run', '--collection-id', '00000000-0000-4000-8000-000000000003',
                            '--item-id', '00000000-0000-4000-8000-000000000005', '--password-env', 'VAULT_PASSWORD', '--'] + command,
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
                        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
                        self.assertEqual(result.stdout, b'vault-restricted: command finished; exit code 0\n')
                        self.assertEqual(result.stderr, b'')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_cgroup_budget_and_inherited_file_limits_are_not_replaced(self):
        limits = {name: resource.getrlimit(getattr(resource, name))
                  for name in ['RLIMIT_AS', 'RLIMIT_FSIZE', 'RLIMIT_NOFILE']}
        code = ('import resource; expected=' + repr(limits) + ';'
                'assert all(resource.getrlimit(getattr(resource,k))==v for k,v in expected.items());'
                'assert resource.getrlimit(resource.RLIMIT_CORE)==(0,0)')
        self.assertEqual(helper.quiet_command([sys.executable, '-c', code], {}, 10), 0)


if __name__ == '__main__':
    unittest.main()
