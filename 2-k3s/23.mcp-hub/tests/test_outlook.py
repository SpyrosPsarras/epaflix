import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest

import yaml

HUB = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HUB / "files"), str(HUB / "tools")]
TOKEN = "outlook-upstream-fixture-token"
SECRETS = ("fixture-access", "fixture-refresh", "fixture-device-code")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeGrant:
    def __init__(self):
        self.tokens, self.invalidated = ["stale", "fresh"], 0

    def headers(self):
        return {"Authorization": "Bearer " + self.tokens[0]}

    def invalidate(self):
        self.invalidated += 1
        self.tokens.pop(0)


class GatewayTests(unittest.TestCase):
    def test_auth_and_graph_bearer_injection(self):
        import httpx2
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Route
        from starlette.testclient import TestClient

        gateway = load(HUB / "instances/outlook/gateway.py", "outlook_gateway")
        seen = []

        async def raw(request):
            seen.append(dict(request.headers))
            if request.headers["authorization"] == "Bearer stale":
                return Response("expired", status_code=401, headers={"www-authenticate": "Bearer"})
            message = json.loads(await request.body())
            return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}})

        grant = FakeGrant()
        transport = httpx2.ASGITransport(app=Starlette(routes=[Route("/mcp", raw, methods=["GET", "POST", "DELETE"])]))
        with TestClient(gateway.app(TOKEN, grant, transport=transport)) as client:
            self.assertEqual(client.get("/healthz").status_code, 200)
            for method in ("GET", "POST", "DELETE"):
                for bearer in (None, "Bearer wrong", TOKEN, "Basic " + TOKEN, "Bearer ", "Bearer work.fixture"):
                    headers = {} if bearer is None else {"Authorization": bearer}
                    self.assertEqual(client.request(method, "/mcp", headers=headers).status_code, 401)
            self.assertEqual(seen, [])
            response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                                   headers={"Authorization": "Bearer " + TOKEN, "Cookie": "private=fixture",
                                            "Accept": "application/json, text/event-stream"})
            self.assertEqual(response.json(), {"jsonrpc": "2.0", "id": 4, "result": {"ok": True}})
            self.assertEqual([h["authorization"] for h in seen], ["Bearer stale", "Bearer fresh"])
            self.assertEqual(grant.invalidated, 1)
            self.assertNotIn("cookie", seen[-1])
            for route in ("/gmail", "/drive", "/notion", "/outlook", "/searxng"):
                self.assertEqual(client.post(route, headers={"Authorization": "Bearer " + TOKEN}).status_code, 404)
        for invalid in ("", "short", "has whitespace in it here", "work.fixture-long-enough"):
            with self.assertRaises(ValueError):
                gateway.app(invalid, grant)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.bootstrap = load(HUB / "tools/bootstrap-outlook.py", "bootstrap_outlook")
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))

    def tearDown(self):
        for secret in SECRETS:
            self.assertNotIn(secret, self.output.getvalue())

    def run_sign_in(self, replies):
        calls, now = [], [0.0]

        def post(url, form):
            calls.append((url, form))
            return replies.pop(0)

        def sleep(seconds):
            now[0] += seconds

        result = self.bootstrap.sign_in("tenant/x", "app", post=post, sleep=sleep, clock=lambda: now[0])
        return result, calls, now[0]

    def device(self):
        return {"device_code": "fixture-device-code", "message": "open the page, enter ABC", "interval": 5,
                "expires_in": 900}

    def test_device_flow_waits_then_checks_scopes(self):
        granted = {"access_token": "fixture-access", "refresh_token": "fixture-refresh", "expires_in": 3600,
                   "scope": "https://graph.microsoft.com/Mail.ReadWrite Mail.Send MailboxSettings.Read "
                            "MailboxSettings.ReadWrite User.Read profile openid email"}
        (endpoint, tokens), calls, elapsed = self.run_sign_in(
            [self.device(), {"error": "authorization_pending"}, {"error": "slow_down"}, granted])
        self.assertEqual(endpoint, "https://login.microsoftonline.com/tenant%2Fx/oauth2/v2.0/token")
        self.assertEqual(tokens, granted)
        self.assertEqual(calls[0][1]["scope"], self.bootstrap.SCOPE)
        self.assertIn("offline_access", calls[0][1]["scope"].split())
        self.assertEqual(elapsed, 5 + 5 + 10)
        self.assertIn("enter ABC", self.output.getvalue())

    def test_failures_write_nothing(self):
        cases = {
            "refused": [{"error": "invalid_client"}],
            "denied": [self.device(), {"error": "access_denied"}],
            "incomplete": [self.device(), {"access_token": "fixture-access", "scope": "Mail.Send"}],
            "fewer scopes": [self.device(), {"access_token": "fixture-access", "refresh_token": "fixture-refresh",
                                             "scope": "Mail.ReadWrite User.Read"}],
            "timeout": [{**self.device(), "expires_in": 9}] + [{"error": "authorization_pending"}] * 3,
        }
        for name, replies in cases.items():
            with self.subTest(name), self.assertRaises(SystemExit) as raised:
                self.run_sign_in(replies)
            self.assertIn("nothing written", str(raised.exception))

    def test_secret_create_and_conflict_safe_replace(self):
        runs = []

        def run(command, **kwargs):
            runs.append((command, kwargs.get("input")))
            stdout = existing if "get" in command else ""
            return subprocess.CompletedProcess(command, 0, stdout, "")

        for existing, verb in (("", "create"), (json.dumps({"metadata": {"resourceVersion": "42"}}), "replace")):
            runs.clear()
            self.bootstrap.write_secret("ctx", {"refresh_token": "fixture-refresh"}, run=run)
            command, document = runs[-1]
            self.assertEqual(command, ["kubectl", "--context", "ctx", "-n", "mcp-hub-work", verb, "-f", "-"])
            document = json.loads(document)
            self.assertEqual(document["metadata"].get("resourceVersion"), "42" if verb == "replace" else None)
            self.assertEqual(document["metadata"]["name"], "mcp-hub-outlook-grant")
            self.assertEqual(base64.b64decode(document["data"]["refresh_token"]).decode(), "fixture-refresh")


class RenderTests(unittest.TestCase):
    def render(self, environment):
        return list(yaml.safe_load_all(subprocess.check_output(["kustomize", "build", str(HUB / "instances" / environment)])))

    def test_work_only_loopback_raw_and_gateway_only_grant_access(self):
        self.assertFalse(any("outlook" in doc["metadata"]["name"] for doc in self.render("homelab")))
        docs = self.render("work")
        deployment = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "outlook-mcp")
        self.assertEqual(deployment["metadata"]["namespace"], "mcp-hub-work")
        self.assertEqual((deployment["spec"]["replicas"], deployment["spec"]["strategy"]["type"]), (1, "Recreate"))
        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        raw, gateway = ({c["name"]: c for c in pod["containers"]}[n] for n in ("raw", "gateway"))
        self.assertEqual(raw["image"], "ghcr.io/softeria/ms-365-mcp-server:0.191.1")
        self.assertEqual(raw["args"], ["--http", "127.0.0.1:3000", "--org-mode", "--preset", "mail"])
        self.assertNotIn("ports", raw)
        self.assertFalse(any("secretKeyRef" in str(e) for e in raw["env"]))
        self.assertEqual({m["mountPath"] for m in raw["volumeMounts"]}, {"/tmp"})
        mounts = {m["mountPath"]: m["name"] for m in gateway["volumeMounts"]}
        self.assertEqual(mounts["/var/run/secrets/kubernetes.io/serviceaccount"], "grant-access")
        env = {e["name"]: e.get("valueFrom") for e in gateway["env"]}
        self.assertEqual(env["OUTLOOK_UPSTREAM_TOKEN"], {"secretKeyRef": {"name": "mcp-hub-outlook-upstream", "key": "token"}})
        for container in (raw, gateway):
            self.assertEqual(container["securityContext"], {"allowPrivilegeEscalation": False,
                                                            "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}})
        service = next(d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == "outlook-mcp")
        self.assertEqual([(p["port"], p["targetPort"]) for p in service["spec"]["ports"]], [(8000, "http")])
        role = next(d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "mcp-hub-outlook-grant")
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["secrets"],
                                          "resourceNames": ["mcp-hub-outlook-grant"], "verbs": ["get", "patch"]}])
        binding = next(d for d in docs if d["kind"] == "RoleBinding" and d["metadata"]["name"] == "mcp-hub-outlook-grant")
        self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": "outlook-mcp", "namespace": "mcp-hub-work"}])
        self.assertFalse(any(d["kind"] == "Secret" for d in docs))


if __name__ == "__main__":
    unittest.main()
