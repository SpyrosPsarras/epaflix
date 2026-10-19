import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import uuid

import yaml

HUB = Path(__file__).resolve().parents[1]
RUNNER = Path(os.environ.get("GOOGLE_RUNNER_DIR", HUB / "instances/google"))
RAW_TEST = os.environ.get("WORKSPACE_MCP_RAW_TEST") == "1"
EMAIL = "fixture+fresh@example.test"
CLIENT_ID = "synthetic-client-id"
CLIENT_SECRET = "synthetic-client-secret"
REFRESH_TOKEN = "synthetic-fresh-refresh"
TOKEN = "synthetic_independent_google_bearer"
complete_scope_set = {
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid",
}
expected_tool_names = {
    "search_gmail_messages", "get_gmail_message_content", "get_gmail_messages_content_batch",
    "send_gmail_message", "get_gmail_attachment_content", "get_gmail_thread_content",
    "modify_gmail_message_labels", "list_gmail_labels", "manage_gmail_label", "draft_gmail_message",
    "list_gmail_filters", "manage_gmail_filter", "get_gmail_threads_content_batch",
    "batch_modify_gmail_message_labels", "search_drive_files", "get_drive_file_content",
    "get_drive_file_download_url", "create_drive_file", "create_drive_folder", "import_to_google_doc",
    "import_to_google_slides", "import_to_google_sheets", "get_drive_shareable_link", "list_drive_items",
    "copy_drive_file", "update_drive_file", "manage_drive_access", "set_drive_file_permissions",
    "get_drive_file_permissions", "check_drive_file_public_access", "gmail_send_draft",
}
RAW_ARGS = ["--transport", "streamable-http", "--single-user", "--tools", "gmail", "drive",
            "--tool-tier", "complete", "--disabled-tools", "start_google_auth"]


def load(name):
    spec = importlib.util.spec_from_file_location("google_" + name, RUNNER / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seeded(directory):
    return load("seed").seed(Path(directory), EMAIL, CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN)


@unittest.skipIf(RAW_TEST, "gateway SDK runs in the host fixture environment")
class GatewayTests(unittest.TestCase):
    def test_direct_auth_all_methods(self):
        import httpx2
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse, Response, StreamingResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        sys.path.insert(0, str(HUB / "files"))
        gateway = load("gateway")
        raw_requests = []

        async def raw(request):
            body = await request.body()
            raw_requests.append((request.method, dict(request.headers), body))
            if request.method == "DELETE":
                return Response(status_code=204)
            if request.method == "GET":
                async def events():
                    yield b'event: message\ndata: {"jsonrpc":"2.0","id":9,"result":{}}\n\n'
                return StreamingResponse(events(), media_type="text/event-stream",
                                         headers={"mcp-session-id": "fixture-session"})
            message = json.loads(body)
            if message["method"] == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "synthetic-raw", "version": "0"}}
            elif message["method"] == "tools/list":
                result = {"tools": [{"name": "synthetic_read", "inputSchema": {"type": "object"}}]}
            else:
                result = {"content": [{"type": "text", "text": "synthetic read result"}], "isError": False}
            return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": result},
                                headers={"mcp-session-id": "fixture-session", "x-internal": "hidden"})

        transport = httpx2.ASGITransport(app=Starlette(routes=[
            Route("/mcp", raw, methods=["GET", "POST", "DELETE"])]))
        with TestClient(gateway.app(TOKEN, transport=transport)) as client:
            self.assertEqual(client.get("/healthz").status_code, 200)
            for method in ("GET", "POST", "DELETE"):
                for bearer in (None, "Bearer wrong", TOKEN, "Basic " + TOKEN, "Bearer ",
                               b"Bearer non-ascii-\xff", "Bearer homelab.fixture",
                               "Bearer work.fixture", "Bearer legacy.fixture"):
                    headers = {} if bearer is None else {"Authorization": bearer}
                    response = client.request(method, "/mcp", headers=headers)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(raw_requests, [])
            headers = {"Authorization": "Bearer " + TOKEN, "Cookie": "private=fixture",
                       "Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                       "mcp-protocol-version": "2025-06-18", "mcp-session-id": "fixture-session",
                       "last-event-id": "fixture-event"}
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "fixture", "version": "0"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "synthetic_read", "arguments": {}}},
            ]
            for message in messages:
                response = client.post("/mcp", headers=headers, json=message)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["id"], message["id"])
                self.assertEqual(response.headers["mcp-session-id"], "fixture-session")
                self.assertNotIn("x-internal", response.headers)
            self.assertEqual(response.json()["result"]["content"][0]["text"], "synthetic read result")
            response = client.get("/mcp", headers=headers)
            self.assertIn('event: message\ndata: {"jsonrpc"', response.text)
            self.assertIn("text/event-stream", response.headers["content-type"])
            self.assertEqual(client.delete("/mcp", headers=headers).status_code, 204)
            for method, forwarded, body in raw_requests:
                self.assertNotIn("authorization", forwarded)
                self.assertNotIn("cookie", forwarded)
                self.assertEqual(forwarded["mcp-protocol-version"], "2025-06-18")
                self.assertEqual(forwarded["mcp-session-id"], "fixture-session")
                self.assertEqual(forwarded["last-event-id"], "fixture-event")
            for route in ("/gmail", "/drive", "/notion", "/kubernetes", "/vault-secret", "/vaultwarden", "/searxng"):
                self.assertEqual(client.post(route, headers=headers).status_code, 404)
        for invalid in ("", "short", "homelab.fixture-long-enough", "work.fixture-long-enough", "has whitespace here", "non-ascii-\u00e9-long"):
            with self.assertRaises(ValueError):
                gateway.app(invalid)


class SeedTests(unittest.TestCase):
    def test_seed_and_restart_contract(self):
        seed = load("seed")
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            credential_path = seeded(directory)
            credential_json = json.loads(credential_path.read_text())
            self.assertEqual(credential_path, Path(directory) / "fixture%2Bfresh@example.test.json")
            self.assertEqual(credential_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(set(credential_json), {"refresh_token", "token_uri", "client_id", "client_secret", "scopes"})
            self.assertEqual(set(credential_json["scopes"]), complete_scope_set)
            self.assertEqual(credential_json["refresh_token"], REFRESH_TOKEN)
            self.assertEqual(credential_json["token_uri"], "https://oauth2.googleapis.com/token")
            self.assertEqual(credential_json["client_id"], CLIENT_ID)
            self.assertEqual(credential_json["client_secret"], CLIENT_SECRET)
            credential_json.update(token="synthetic-workspace-access", expiry="2099-01-01T00:00:00Z")
            credential_path.write_text(json.dumps(credential_json))
            with tempfile.TemporaryDirectory() as restart:
                fresh = json.loads(seeded(restart).read_text())
                self.assertEqual(fresh["refresh_token"], REFRESH_TOKEN)
                self.assertNotIn("token", fresh)
            for index in range(4):
                for missing in ("", "   ", None):
                    values = [EMAIL, CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN]
                    values[index] = missing
                    with self.assertRaises(ValueError):
                        seed.seed(Path(directory), *values)
            with patch.object(Path, "open", side_effect=PermissionError("private identity")):
                with self.assertRaisesRegex(ValueError, "^Google credential seed failed$"):
                    seeded(directory)
        for private in (EMAIL, CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN):
            self.assertNotIn(private, output.getvalue())

    def test_seed_entrypoint_fails_closed_without_output(self):
        for missing in ("USER_GOOGLE_EMAIL", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"):
            with tempfile.TemporaryDirectory() as directory:
                env = {**os.environ, "USER_GOOGLE_EMAIL": EMAIL, "GOOGLE_OAUTH_CLIENT_ID": CLIENT_ID,
                       "GOOGLE_OAUTH_CLIENT_SECRET": CLIENT_SECRET, "GOOGLE_REFRESH_TOKEN": REFRESH_TOKEN,
                       "WORKSPACE_MCP_CREDENTIALS_DIR": directory}
                env.pop(missing)
                result = subprocess.run([sys.executable, str(RUNNER / "seed.py")], env=env, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(list(Path(directory).iterdir()), [])
                for private in (EMAIL, CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN):
                    self.assertNotIn(private, result.stdout + result.stderr)

    def test_legacy_gmail_selftest(self):
        sys.path.insert(0, str(HUB / "files"))
        import gmail_mcp
        output = io.StringIO()
        with patch.object(gmail_mcp.urllib.request, "urlopen", side_effect=AssertionError("HTTP forbidden")), contextlib.redirect_stdout(output):
            gmail_mcp._selftest()
        self.assertEqual(output.getvalue().strip(), "gmail selftest OK")


@unittest.skipIf(RAW_TEST, "rendering runs on the host")
class RenderTests(unittest.TestCase):
    def test_homelab_only_render(self):
        with tempfile.TemporaryDirectory(dir=HUB / "instances") as directory:
            Path(directory, "kustomization.yaml").write_text(
                "resources:\n  - ../base\n  - ../google\nnamespace: mcp-hub-homelab\n")
            docs = list(yaml.safe_load_all(subprocess.check_output(["kustomize", "build", directory])))
        deployment = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "workspace-mcp")
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["strategy"]["type"], "Recreate")
        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertFalse(pod["enableServiceLinks"])
        self.assertEqual(pod["securityContext"], {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                                                   "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}})
        raw, gateway = ({c["name"]: c for c in pod["containers"]}[n] for n in ("raw", "gateway"))
        env = {e["name"]: e.get("value", e.get("valueFrom")) for e in raw["env"]}
        for key, value in {"WORKSPACE_MCP_HOST": "127.0.0.1", "WORKSPACE_MCP_PORT": "8001", "HOME": "/tmp",
                           "WORKSPACE_MCP_DISABLE_LOCAL_FILES": "true", "WORKSPACE_MCP_CREDENTIALS_DIR": "/creds",
                           "PYTHONPATH": "/app:/legacy"}.items():
            self.assertEqual(env[key], value)
        self.assertNotIn("PORT", env)
        self.assertEqual(raw["workingDir"], "/app")
        self.assertEqual(raw["image"], "ghcr.io/taylorwilsdon/google_workspace_mcp:2.0.1")
        self.assertIn(shlex.join(RAW_ARGS), raw["command"][2])
        self.assertIn("/app/.venv/bin/python /runner/seed.py && unset GOOGLE_REFRESH_TOKEN && exec /app/.venv/bin/python /runner/workspace.py", raw["command"][2])
        for key, secret_key in (("USER_GOOGLE_EMAIL", "email"), ("GOOGLE_OAUTH_CLIENT_ID", "client-id"),
                                ("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret"), ("GOOGLE_REFRESH_TOKEN", "refresh-token")):
            self.assertEqual(env[key], {"secretKeyRef": {"name": "mcp-hub-gmail", "key": secret_key}})
        raw_mounts = {m["mountPath"]: m for m in raw["volumeMounts"]}
        for path in ("/runner/seed.py", "/runner/workspace.py", "/legacy/gmail_mcp.py"):
            self.assertTrue(raw_mounts[path]["readOnly"])
            self.assertEqual(raw_mounts[path]["subPath"], Path(path).name)
        for path in ("/creds", "/tmp"):
            self.assertFalse(raw_mounts[path].get("readOnly", False))
        gateway_env = {e["name"]: e.get("value", e.get("valueFrom")) for e in gateway["env"]}
        self.assertEqual(set(gateway_env), {"GOOGLE_UPSTREAM_TOKEN", "PYTHONPATH", "HOME", "PYTHONDONTWRITEBYTECODE"})
        self.assertEqual(gateway_env["GOOGLE_UPSTREAM_TOKEN"], {"secretKeyRef": {"name": "mcp-hub-google-upstream", "key": "token"}})
        self.assertEqual(gateway["image"], "docker.io/library/python:3.13-alpine")
        self.assertIn("mcp==2.2.0", gateway["command"][2])
        gateway_mounts = {m["mountPath"]: m for m in gateway["volumeMounts"]}
        self.assertEqual(set(gateway_mounts), {"/app", "/runner/gateway.py", "/opt/py", "/tmp"})
        self.assertEqual(gateway_mounts["/runner/gateway.py"]["subPath"], "gateway.py")
        self.assertTrue(gateway_mounts["/runner/gateway.py"]["readOnly"])
        volumes = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(volumes["creds"]["emptyDir"]["medium"], "Memory")
        shared = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == volumes["app"]["configMap"]["name"])
        self.assertEqual(shared["data"]["gmail_mcp.py"], (HUB / "files/gmail_mcp.py").read_text())
        runner = volumes["runner"]["configMap"]["name"]
        self.assertRegex(runner, r"^google-runner-[a-z0-9]+$")
        config = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == runner)
        self.assertEqual(set(config["data"]), {"gateway.py", "seed.py", "workspace.py"})
        for container in (raw, gateway):
            self.assertNotIn("envFrom", container)
            self.assertEqual(container["securityContext"], {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                          "capabilities": {"drop": ["ALL"]}})
            self.assertIn("readinessProbe", container)
            self.assertIn("livenessProbe", container)
        self.assertEqual(raw["readinessProbe"]["exec"]["command"][-1], "http://127.0.0.1:8001/health")
        self.assertEqual(raw["livenessProbe"]["exec"]["command"][-1], "http://127.0.0.1:8001/health")
        self.assertEqual(gateway["readinessProbe"]["httpGet"]["path"], "/healthz")
        self.assertEqual(raw["resources"], {"requests": {"cpu": "10m", "memory": "160Mi"}, "limits": {"memory": "512Mi"}})
        self.assertEqual(gateway["resources"], {"requests": {"cpu": "10m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}})
        service = next(d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == "workspace-mcp")
        service_ports = [p["port"] for p in service["spec"]["ports"]]
        self.assertEqual(service_ports, [8000])
        self.assertEqual(service["spec"]["ports"][0]["targetPort"], "http")
        self.assertFalse(any(d["kind"] in ("Secret", "Ingress", "IngressRoute") for d in docs))
        for environment in ("homelab", "work"):
            existing = list(yaml.safe_load_all(subprocess.check_output(["kustomize", "build", str(HUB / "instances" / environment)])))
            self.assertFalse(any(d["kind"] == "Deployment" and d["metadata"]["name"] == "workspace-mcp" for d in existing))


@unittest.skipUnless(RAW_TEST, "raw FastMCP tests run in the pinned source/image environment")
class RawTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from core.server import SecureFastMCP
        from core.config import USER_GOOGLE_EMAIL
        from auth.oauth_config import is_oauth21_enabled
        import gmail_mcp
        self.assertEqual(USER_GOOGLE_EMAIL, EMAIL)
        self.assertFalse(is_oauth21_enabled())
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch("auth.google_auth.start_auth_flow", side_effect=AssertionError("interactive auth forbidden")))
        self.refresh_count = 0
        self.access_token = None
        self.gmail = gmail_mcp
        self.workspace = load("workspace")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.credential_path = seeded(self.directory.name)
        self.server = SecureFastMCP("synthetic-google")
        self.workspace.register_send_draft(self.server, self.credential_path)
        self.addCleanup(setattr, gmail_mcp, "AUTH", None)

    def http(self, request, **kwargs):
        self.requests.append(request)
        if request.full_url == "https://oauth2.googleapis.com/token":
            form = urllib.parse.parse_qs(request.data.decode())
            self.assertEqual(form, {"client_id": [CLIENT_ID], "client_secret": [CLIENT_SECRET],
                                    "refresh_token": [REFRESH_TOKEN], "grant_type": ["refresh_token"]})
            self.refresh_count += 1
            self.access_token = "synthetic-refreshed-access" if self.refresh_count == 1 else "synthetic-second-access"
            return io.BytesIO(json.dumps({"access_token": self.access_token, "expires_in": 3600}).encode())
        self.assertEqual(request.method, "POST")
        self.assertEqual(urllib.parse.urlsplit(request.full_url).path, "/gmail/v1/users/me/drafts/send")
        self.assertEqual(json.loads(request.data), {"id": "draft-fixture"})
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.access_token)
        return io.BytesIO(b'{"id":"message-fixture","threadId":"thread-fixture"}')

    async def call(self, arguments):
        from fastmcp import Client
        async with Client(self.server) as client:
            return await client.call_tool("gmail_send_draft", arguments, raise_on_error=False)

    async def test_send_draft_listed_and_sent(self):
        from fastmcp import Client
        self.requests = []
        with patch.object(self.gmail.urllib.request, "urlopen", side_effect=self.http):
            async with Client(self.server) as client:
                tools = await client.list_tools()
                self.assertEqual([t.name for t in tools], ["gmail_send_draft"])
                self.assertEqual(tools[0].input_schema["required"], ["draft_id"])
                self.assertEqual(tools[0].input_schema["properties"]["draft_id"]["type"], "string")
                result = await client.call_tool("gmail_send_draft", {"draft_id": "draft-fixture"})
        send_result = json.loads(result.content[0].text)
        self.assertIs(send_result["sent"], True)
        self.assertEqual(send_result, {"sent": True, "message_id": "message-fixture", "thread_id": "thread-fixture"})
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(sum(r.full_url.endswith("/drafts/send") for r in self.requests), 1)

    async def test_send_draft_invalid_id_is_mcp_error(self):
        with patch.object(self.gmail.urllib.request, "urlopen", side_effect=AssertionError("HTTP forbidden")) as network:
            for arguments in ({}, {"draft_id": ""}, {"draft_id": "  "}, {"draft_id": "draft\nfixture"}, {"draft_id": "draft\x7ffixture"}, {"draft_id": "draft\u0085fixture"}):
                result = await self.call(arguments)
                self.assertTrue(result.is_error)
            network.assert_not_called()
        for status in (400, 404):
            self.gmail.AUTH._token, self.gmail.AUTH._expires = "synthetic-refreshed-access", time.time() + 3600
            private = "synthetic-private-google-response " + REFRESH_TOKEN
            error = urllib.error.HTTPError("https://gmail.googleapis.com", status, "error", {}, io.BytesIO(private.encode()))
            with patch.object(self.gmail.urllib.request, "urlopen", side_effect=error) as network, \
                    self.assertLogs("fastmcp", level="ERROR") as logs:
                result = await self.call({"draft_id": "draft-fixture"})
                self.assertTrue(result.is_error)
                self.assertEqual(network.call_count, 1)
                text = " ".join(c.text for c in result.content) + "\n".join(logs.output)
                self.assertIn("Google draft send failed", text)
                self.assertNotIn(private, text)
                self.assertNotIn(REFRESH_TOKEN, text)

    async def test_send_draft_missing_credentials_no_network(self):
        from core.server import SecureFastMCP
        with patch.object(self.gmail.urllib.request, "urlopen", side_effect=AssertionError("HTTP forbidden")) as network:
            with patch.object(self.gmail, "Auth", wraps=self.gmail.Auth) as constructor:
                for bad in (self.credential_path.with_name("absent.json"), Path(self.directory.name)):
                    with self.assertRaisesRegex(ValueError, "^Google draft credentials unavailable$"):
                        self.workspace.register_send_draft(SecureFastMCP("missing"), bad)
                grant = json.loads(self.credential_path.read_text())
                for field in ("client_id", "client_secret", "refresh_token", "token_uri", "scopes"):
                    missing = {k: v for k, v in grant.items() if k != field}
                    for invalid in (missing, *({**grant, field: value} for value in (None, "", "   ", []))):
                        self.credential_path.write_text(json.dumps(invalid))
                        with self.assertRaisesRegex(ValueError, "^Google draft credentials unavailable$"):
                            self.workspace.register_send_draft(SecureFastMCP("missing"), self.credential_path)
                self.credential_path.write_text("not json " + EMAIL)
                with self.assertRaisesRegex(ValueError, "^Google draft credentials unavailable$"):
                    self.workspace.register_send_draft(SecureFastMCP("missing"), self.credential_path)
                with patch.object(Path, "read_text", side_effect=PermissionError(EMAIL)):
                    with self.assertRaisesRegex(ValueError, "^Google draft credentials unavailable$"):
                        self.workspace.register_send_draft(SecureFastMCP("missing"), self.credential_path)
                constructor.assert_not_called()
            self.gmail.AUTH = None
            result = await self.call({"draft_id": "draft-fixture"})
            self.assertTrue(result.is_error)
            self.assertIn("Google draft credentials unavailable", result.content[0].text)
            network.assert_not_called()

    async def test_send_draft_refresh_never_writes(self):
        before = self.credential_path.read_bytes(), self.credential_path.stat().st_mtime_ns
        self.requests = []
        with patch.object(self.gmail.urllib.request, "urlopen", side_effect=self.http):
            first = await self.call({"draft_id": "draft-fixture"})
            self.assertFalse(first.is_error)
            self.gmail.AUTH._expires = 0
            with contextlib.ExitStack() as stack:
                for name in ("write_text", "write_bytes", "touch", "unlink", "rename", "replace", "open"):
                    stack.enter_context(patch.object(Path, name, side_effect=AssertionError("file mutation forbidden")))
                stack.enter_context(patch("builtins.open", side_effect=AssertionError("file mutation forbidden")))
                for name in ("open", "unlink", "remove", "rename", "replace", "utime"):
                    stack.enter_context(patch("os." + name, side_effect=AssertionError("file mutation forbidden")))
                second = await self.call({"draft_id": "draft-fixture"})
                self.assertFalse(second.is_error)
        self.assertEqual(before, (self.credential_path.read_bytes(), self.credential_path.stat().st_mtime_ns))
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.gmail.AUTH._token, "synthetic-second-access")
        self.assertGreater(self.gmail.AUTH._expires, time.time())
        self.assertEqual(self.gmail.AUTH.refresh_token, REFRESH_TOKEN)

    async def test_post_filter_hook_preserves_summary_and_uses_seed_filename(self):
        import main as pinned_main
        events = []
        sentinel = object()
        def original(server):
            events.append(("filter", server))
            return sentinel
        def register(server, path):
            events.append(("register", server, path))
        def run():
            self.assertIs(pinned_main.filter_server_tools(self.server), sentinel)
        with patch.dict(os.environ, {"WORKSPACE_MCP_CREDENTIALS_DIR": self.directory.name, "USER_GOOGLE_EMAIL": EMAIL}), \
                patch.object(pinned_main, "filter_server_tools", original), patch.object(pinned_main, "main", run), \
                patch.object(self.workspace, "register_send_draft", register):
            self.workspace.main()
        self.assertEqual(events, [("filter", self.server), ("register", self.server, self.credential_path)])


@unittest.skipUnless(os.environ.get("WORKSPACE_MCP_IMAGE_TEST") == "1", "required image contract is enabled in CI")
class ImageTests(unittest.TestCase):
    def test_pinned_image_complete_inventory(self):
        self.assertIsNotNone(shutil.which("docker"), "Docker is required for image mode")
        docs = list(yaml.safe_load_all((RUNNER / "workspace.yaml").read_text()))
        pod = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]
        raw = next(c for c in pod["containers"] if c["name"] == "raw")
        name = "workspace-mcp-fixture-" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory, "env")
            values = {e["name"]: e["value"] for e in raw["env"] if "value" in e}
            values.update(USER_GOOGLE_EMAIL=EMAIL, GOOGLE_OAUTH_CLIENT_ID=CLIENT_ID,
                          GOOGLE_OAUTH_CLIENT_SECRET=CLIENT_SECRET, GOOGLE_REFRESH_TOKEN=REFRESH_TOKEN,
                          GOOGLE_RUNNER_DIR="/runner", WORKSPACE_MCP_RAW_TEST="1", MCP_ENABLE_OAUTH21="false")
            env_file.write_text("\n".join(k + "=" + v for k, v in values.items()) + "\n")
            env_file.chmod(0o600)
            command = ["docker", "run", "--detach", "--name", name, "--network", "none", "--read-only",
                       "--user", "1000:1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                       "--workdir", "/app", "--tmpfs", "/tmp:uid=1000,gid=1000", "--tmpfs", "/creds:uid=1000,gid=1000",
                       "--env-file", str(env_file), "--volume", str(RUNNER) + ":/runner:ro",
                       "--volume", str(HUB / "files/gmail_mcp.py") + ":/legacy/gmail_mcp.py:ro",
                       "--volume", str(Path(__file__).resolve()) + ":/fixtures/tests/test_google.py:ro",
                       "--entrypoint", raw["command"][0], raw["image"], *raw["command"][1:]]
            try:
                subprocess.run(command, check=True, capture_output=True, text=True, timeout=300)
                probe = "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=2).read()"
                for attempt in range(90):
                    ready = subprocess.run(["docker", "exec", name, "/app/.venv/bin/python", "-c", probe], capture_output=True, timeout=10)
                    if ready.returncode == 0:
                        break
                    time.sleep(1)
                else:
                    self.fail("Pinned image did not become healthy on loopback port 8001")
                inventory = '''import asyncio, json
from fastmcp import Client
async def main():
    async with Client("http://127.0.0.1:8001/mcp") as client:
        print(json.dumps(sorted(t.name for t in await client.list_tools())))
asyncio.run(main())
'''
                result = subprocess.run(["docker", "exec", name, "/app/.venv/bin/python", "-c", inventory],
                                        capture_output=True, text=True, check=True, timeout=60)
                served_tool_names = set(json.loads(result.stdout))
                self.assertEqual(served_tool_names, expected_tool_names)
                self.assertEqual(len(json.loads(result.stdout)), 31)
                self.assertNotIn("start_google_auth", served_tool_names)
                tests = subprocess.run(["docker", "exec", name, "/app/.venv/bin/python",
                                        "/fixtures/tests/test_google.py", "RawTests", "-v"],
                                       capture_output=True, text=True, timeout=180)
                self.assertEqual(tests.returncode, 0, tests.stdout + tests.stderr)
            finally:
                subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=30)


if __name__ == "__main__":
    unittest.main()
