"""vault-run.py against a fake hub, with a synthetic item."""
import http.server
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPT = Path(__file__).with_name("vault-run.py")
ITEMS = {"/demo/token": {"username": "demo-user", "password": "synthetic-secret-1"},
         "/demo/fields": {"username": None, "password": "synthetic-secret-3",
                          "fields": {"api token": "synthetic-field-1", "x.y-z": "synthetic-field-2"}},
         "/demo/fields-only": {"username": None, "password": None, "fields": {"api token": "synthetic-field-3"}},
         "/demo/collision": {"username": None, "password": "p", "fields": {"api-token": "synthetic-field-4",
                                                                           "api token": "synthetic-field-5"}},
         "/demo/no-user": {"username": None, "password": "synthetic-secret-2"},
         "/demo/empty": {"username": None, "password": None}}
# The hub's error text for this path carries a secret; vault-run must not print it.
LEAKY = "/demo/leaky"
seen = []


class Hub(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        call = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append((self.headers["Authorization"], self.path, call))
        path = call["path"]
        if path == LEAKY:
            code, body = 502, b"upstream failed: synthetic-item-secret"
        elif path in ITEMS:
            code, body = 200, json.dumps(ITEMS[path]).encode()
        else:
            code, body = 404, f"ValueError: no entry at path '{path}'".encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class VaultRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), Hub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def run_script(self, *args):
        env = {**os.environ, "MCP_HUB_URL": f"http://127.0.0.1:{self.server.server_port}/", "MCP_HUB_TOKEN": "hub-token"}
        env["VAULT_USERNAME"] = "outer-user"  # vault-run must replace or drop it
        env["VAULT_FIELD_STALE"] = "outer-field"
        env["VAULT_PASSWORD"] = "outer-password"
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30)

    def test_command_gets_secret_and_username(self):
        seen.clear()
        r = self.run_script("/demo/token", "sh", "-c", 'printf "%s:%s" "$VAULT_USERNAME" "$VAULT_PASSWORD"')
        self.assertEqual((r.returncode, r.stdout), (0, "demo-user:synthetic-secret-1"))
        self.assertEqual(seen, [("Bearer hub-token", "/vault-secret", {"path": "/demo/token"})])

    def test_custom_fields_become_env_vars(self):
        r = self.run_script("/demo/fields", "sh", "-c", 'printf "%s|%s|%s" "$VAULT_FIELD_API_TOKEN" "$VAULT_FIELD_X_Y_Z" "${VAULT_FIELD_STALE-unset}"')
        self.assertEqual(r.stdout, "synthetic-field-1|synthetic-field-2|unset")

    def test_item_with_only_fields_runs_without_password(self):
        r = self.run_script("/demo/fields-only", "sh", "-c", 'printf "%s|%s" "$VAULT_FIELD_API_TOKEN" "${VAULT_PASSWORD-unset}"')
        self.assertEqual((r.returncode, r.stdout), (0, "synthetic-field-3|unset"))

    def test_colliding_field_names_fail_without_leaking(self):
        r = self.run_script("/demo/collision", "sh", "-c", "echo ran")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("ran", r.stdout)
        self.assertIn("VAULT_FIELD_API_TOKEN", r.stderr)
        self.assertNotIn("synthetic-field", r.stderr + r.stdout)

    def test_pc_fallback_reads_token_file(self):
        with tempfile.TemporaryDirectory() as home:
            Path(home, ".config/opencode").mkdir(parents=True)
            Path(home, ".config/opencode/mcp-hub.key").write_text("file-token\n")
            env = {k: v for k, v in os.environ.items() if k != "MCP_HUB_TOKEN"}
            env.update(HOME=home, MCP_HUB_URL=f"http://127.0.0.1:{self.server.server_port}")
            seen.clear()
            r = subprocess.run([sys.executable, str(SCRIPT), "/demo/token", "true"], env=env, capture_output=True,
                               text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(seen[0][0], "Bearer file-token")

    def test_no_username_leaves_it_unset(self):
        r = self.run_script("/demo/no-user", "sh", "-c", 'printf "%s|%s" "${VAULT_USERNAME-unset}" "$VAULT_PASSWORD"')
        self.assertEqual(r.stdout, "unset|synthetic-secret-2")

    def test_command_exit_code_passes_through(self):
        self.assertEqual(self.run_script("/demo/token", "sh", "-c", "exit 7").returncode, 7)

    def test_missing_item_fails_without_running_command(self):
        r = self.run_script("/demo/none", "sh", "-c", "echo ran")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("ran", r.stdout)
        self.assertIn("'/demo/none'", r.stderr)

    def test_hub_error_text_is_not_printed(self):
        r = self.run_script(LEAKY, "true")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("synthetic-item-secret", r.stderr + r.stdout)

    def test_bad_token_is_not_printed(self):
        env = {**os.environ, "MCP_HUB_URL": f"http://127.0.0.1:{self.server.server_port}", "MCP_HUB_TOKEN": "synthetic-hub-token\n"}
        r = subprocess.run([sys.executable, str(SCRIPT), "/demo/token", "true"], env=env, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("synthetic-hub-token", r.stderr + r.stdout)

    def test_missing_command_fails_cleanly(self):
        r = self.run_script("/demo/token", "/does-not-exist")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotIn("synthetic-secret-1", r.stderr + r.stdout)

    def test_item_without_password_fails(self):
        r = self.run_script("/demo/empty", "sh", "-c", "echo ran")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("ran", r.stdout)

    def test_malformed_response_is_not_printed(self):
        listener = socket.create_server(("127.0.0.1", 0))

        def reply():
            conn, _ = listener.accept()
            conn.recv(65536)
            conn.sendall(b"synthetic-upstream-secret\r\n")
            conn.close()
        threading.Thread(target=reply, daemon=True).start()
        env = {**os.environ, "MCP_HUB_URL": f"http://127.0.0.1:{listener.getsockname()[1]}", "MCP_HUB_TOKEN": "hub-token"}
        r = subprocess.run([sys.executable, str(SCRIPT), "/demo/token", "true"], env=env, capture_output=True, text=True, timeout=30)
        listener.close()
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("synthetic-upstream-secret", r.stderr + r.stdout)
        self.assertNotIn("Traceback", r.stderr)

    def test_hub_down_fails_without_leaking(self):
        env = {**os.environ, "MCP_HUB_URL": "http://127.0.0.1:1", "MCP_HUB_TOKEN": "hub-token"}
        r = subprocess.run([sys.executable, str(SCRIPT), "/demo/token", "true"], env=env, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("hub-token", r.stderr)


if __name__ == "__main__":
    unittest.main()
