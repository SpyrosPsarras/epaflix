#!/usr/bin/env python3
"""Offline request, client generation and Kustomize fixtures for the hubs."""

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from mcp.server.mcpserver import MCPServer
from starlette.testclient import TestClient
import yaml

HUB = Path(__file__).resolve().parents[1]
ROOT = HUB.parents[1]
sys.path[:0] = [str(HUB / "files"), str(HUB / "tools")]
import hub_clients
import hub_services
import mcp_hub
import notion_grant
import vaultwarden_mcp
from upstream import Upstream

logging.disable(logging.CRITICAL)
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "fixture", "version": "0"}}}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def auth(token):
    return {**HEADERS, "Authorization": "Bearer " + token}


def fixture(path, environment, names=("searxng",)):
    path.mkdir(parents=True, exist_ok=True)
    services = {"environment": environment, "services": list(names)}
    credentials = {"clients": {"fixture": digest(environment + ".fixture-client")}, "upstreams": {
        name: {"url": "https://" + environment + ".example.test/mcp", "token": environment + "-backend-fixture"}
        for name in names}}
    for name, value in (("services.json", services), ("credentials.json", credentials)):
        (path / name).write_text(json.dumps(value))
    return services, credentials


def backend(environment):
    def server():
        mcp = MCPServer(environment)

        @mcp.tool()
        def echo(value: str) -> str:
            return environment + ":" + value

        return mcp

    return mcp_hub.build_app({"fixture": digest(environment + "-backend-fixture")},
                             modules=[("/mcp", server)], ups=[])


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def app(self, environment, transport=None):
        path = self.home / environment
        fixture(path, environment, names=("searxng" if environment == "homelab" else "drive",))
        env = {"HUB_INSTANCE": environment, "HUB_SERVICES_FILE": str(path / "services.json"),
               "HUB_CREDENTIALS_FILE": str(path / "credentials.json")}
        with patch.dict(os.environ, env, clear=True), patch.object(mcp_hub, "upstreams", side_effect=AssertionError), \
                patch.object(mcp_hub, "Upstream", side_effect=lambda *args, **kwargs:
                             Upstream(*args, transport=transport, **kwargs)):
            return mcp_hub.configured_app()

    def test_requests_through_both_hubs_and_direct_backend_auth(self):
        with contextlib.ExitStack() as stack:
            clients = {}
            direct = {}
            for environment in hub_services.ENVIRONMENTS:
                app = backend(environment)
                direct[environment] = stack.enter_context(TestClient(app))
                clients[environment] = stack.enter_context(TestClient(
                    self.app(environment, httpx2.ASGITransport(app=app))))
            legacy = stack.enter_context(TestClient(mcp_hub.build_app(
                {"fixture": digest("legacy-fixture"), "accidental-copy": digest("homelab.fixture-client")},
                modules=(), ups=[])))
            for environment, client in clients.items():
                own = environment + ".fixture-client"
                selected_path = "/searxng" if environment == "homelab" else "/drive"
                other = "work" if environment == "homelab" else "homelab"
                self.assertEqual(client.get("/healthz").status_code, 200)
                for token in (None, "wrong", other + ".fixture-client", "legacy-fixture"):
                    headers = HEADERS if token is None else auth(token)
                    self.assertEqual(client.post(selected_path, json=INIT, headers=headers).status_code, 401)
                reply = client.post(selected_path, json=INIT, headers=auth(own))
                self.assertEqual(reply.status_code, 200)
                self.assertEqual(reply.json()["result"]["serverInfo"]["name"], environment)
                reply = client.post(selected_path, headers=auth(own), json={
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"value": "offline"}}})
                self.assertEqual(reply.json()["result"]["content"][0]["text"], environment + ":offline")
                for path in set(hub_services.PATHS.values()) - {selected_path}:
                    for method in ("get", "post", "delete"):
                        self.assertEqual(getattr(client, method)(path, headers=auth(own)).status_code, 404)
                self.assertEqual(legacy.get("/absent", headers=auth(own)).status_code, 401)
                self.assertEqual(legacy.get("/absent", headers=auth("legacy-fixture")).status_code, 404)
                for token in (None, own, other + "-backend-fixture", "legacy-fixture"):
                    headers = HEADERS if token is None else auth(token)
                    self.assertEqual(direct[environment].post("/mcp", json=INIT, headers=headers).status_code, 401)
                self.assertEqual(direct[environment].post("/mcp", json=INIT,
                    headers=auth(environment + "-backend-fixture")).status_code, 200)

    def test_invalid_instance_configuration_never_uses_legacy_defaults(self):
        path = self.home / "config"
        services, credentials = fixture(path, "homelab")
        bad_services = [None, {}, {**services, "environment": "legacy"},
                        {**services, "environment": "work"}, {**services, "services": []},
                        {**services, "services": ["unknown"]}, {**services, "services": ["searxng", "searxng"]},
                        {**services, "services": ["vaultwarden"]}, {**services, "services": ["vault-secret"]}]
        for value in bad_services:
            (path / "services.json").write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                mcp_hub.instance_app("homelab", path / "services.json", path / "credentials.json")
        (path / "services.json").write_text(json.dumps(services))
        bad_credentials = [None, {}, {**credentials, "clients": {}}, {**credentials, "clients": {"x": 7}},
                           {**credentials, "upstreams": {}},
                           {**credentials, "upstreams": {**credentials["upstreams"], "drive": {}}}]
        for connection in ({}, {"url": "https://up.example.test/mcp"},
                           {"url": "https://user:password@up.example.test/mcp", "token": "fixture"},
                           {"url": "file:///tmp/mcp", "token": "fixture"},
                           {"url": "https://up.example.test:bad/mcp", "token": "fixture"},
                           {"url": "https://up.example.test:0/mcp", "token": "fixture"},
                           {"url": "https://up.example.test/mcp?token=fixture", "token": "fixture"},
                           {"url": "https://up.example.test/mcp", "token": ""},
                           {"url": "https://up.example.test/mcp", "token": "homelab.fixture-client"},
                           {"url": "https://up.example.test/mcp", "token": "work.fixture-client"},
                           {"url": "https://up.example.test/mcp", "token": "line\nfixture"},
                           {"url": "https://up.example.test/mcp", "token": "fixture\x00"}):
            bad_credentials.append({**credentials, "upstreams": {"searxng": connection}})
        for value in bad_credentials:
            (path / "credentials.json").write_text(json.dumps(value))
            with self.assertRaises(ValueError), patch.object(mcp_hub, "upstreams", side_effect=AssertionError):
                mcp_hub.instance_app("homelab", path / "services.json", path / "credentials.json")
        with self.assertRaises(FileNotFoundError):
            mcp_hub.instance_app("homelab", path / "missing", path / "credentials.json")
        for env in ({"HUB_INSTANCE": "homelab"}, {"HUB_SERVICES_FILE": str(path)},
                    {"HUB_CREDENTIALS_FILE": str(path)}, {"HUB_INSTANCE": ""}):
            with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError), \
                    patch.object(mcp_hub, "load_clients", side_effect=AssertionError):
                mcp_hub.configured_app()

    def test_instances_reject_retired_vault_routes_even_with_reader_marker(self):
        for environment in ("homelab", "work"):
            for name in ("vaultwarden", "vault-secret", "vault-attachment"):
                for marker in ({}, {"vault_reader": "restricted-v1"}):
                    config = {"environment": environment, "services": [name], **marker}
                    with self.subTest(environment=environment, service=name, marker=bool(marker)):
                        with self.assertRaises(ValueError):
                            hub_services.validate_services(config, environment)

    def test_client_cli_uses_same_allowlist_and_removes_stale_servers(self):
        for environment in hub_services.ENVIRONMENTS:
            path = self.home / environment
            services, _ = fixture(path, environment, names=("searxng", "drive"))
            for kind, register, key in (("pi", hub_clients.pi, "mcpServers"),
                                        ("claude", hub_clients.claude, "mcpServers"),
                                        ("opencode", hub_clients.opencode, "mcp")):
                target = path / (kind + ".json")
                initial = register({}, "https://legacy.example.test", "FIXTURE_TOKEN")
                initial[key]["unrelated"] = {"url": "https://unrelated.example.test"}
                target.write_text(json.dumps(initial))
                cmd = [sys.executable, str(HUB / "tools/hub_clients.py"), kind, str(target),
                       "https://" + environment + ".example.test", "FIXTURE_TOKEN", str(path / "services.json")]
                env = {**os.environ, "MCP_HUB_INSTANCE": environment}
                subprocess.run(cmd, env=env, check=True, capture_output=True)
                generated = json.loads(target.read_text())
                self.assertEqual(set(generated[key]), {"searxng", "drive", "unrelated"})
                for name in services["services"]:
                    self.assertEqual(generated[key][name]["url"],
                                     "https://" + environment + ".example.test" + hub_services.PATHS[name])
                before = target.stat().st_mtime_ns
                subprocess.run(cmd, env=env, check=True, capture_output=True)
                self.assertEqual(target.stat().st_mtime_ns, before)
                failed = subprocess.run(cmd, env={**env, "MCP_HUB_INSTANCE": "invalid"}, capture_output=True)
                self.assertNotEqual(failed.returncode, 0)
                self.assertEqual(target.stat().st_mtime_ns, before)
            codex = path / "codex"
            codex.mkdir()
            (codex / "config.toml").write_text('[mcp_servers.vaultwarden]\nurl="https://legacy.example.test/vaultwarden"\n')
            calls = []
            hub_clients.codex(str(codex), "https://hub.example.test", "FIXTURE_TOKEN",
                              run=lambda cmd, **kwargs: calls.append(cmd), services=services)
            self.assertTrue(["codex", "mcp", "remove", "vaultwarden"] in calls)
            self.assertEqual({cmd[3] for cmd in calls if cmd[2] == "add"}, {"searxng", "drive"})

    def test_instance_entrypoints_fail_closed_before_starting_agents(self):
        for location in ("one", "env"):
            source = (ROOT / "2-k3s/13.t3code" / location / "files/entrypoint.sh").read_text()
            prefix = source[:source.index("T3_PROJECT_REPO=${")]
            start = source.index('if [[ -n ${MCP_HUB_TOKEN:-}')
            block = source[start:source.index("T3_HOME=$HOME/.t3", start)]
            block = block.replace("python3 /scripts/hub_clients.py", sys.executable + " " + str(HUB / "tools/hub_clients.py"))
            script = prefix + block + '\nprintf "agents-started"\n'
            for options in ({"MCP_HUB_INSTANCE": "work"},
                            {"MCP_HUB_SERVICES_FILE": str(self.home / "missing")},
                            {"MCP_HUB_INSTANCE": "work", "MCP_HUB_TOKEN": "work.fixture-client",
                             "MCP_HUB_URL": "https://work.example.test"},
                            {"MCP_HUB_SERVICES_FILE": str(self.home / "missing"),
                             "MCP_HUB_TOKEN": "work.fixture-client", "MCP_HUB_URL": "https://work.example.test"}):
                result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                        env={"PATH": os.environ["PATH"], "HOME": str(self.home), **options})
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("agents-started", result.stdout)
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                    env={"PATH": os.environ["PATH"], "HOME": str(self.home)})
            self.assertEqual(result.returncode, 0)
            self.assertIn("agents-started", result.stdout)

    def test_instance_vault_runner_never_reads_legacy_key_or_calls_vault(self):
        spec = importlib.util.spec_from_file_location("vault_run", ROOT / "2-k3s/13.t3code/one/files/vault-run.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        for options in ({"MCP_HUB_INSTANCE": "homelab"}, {"MCP_HUB_INSTANCE": "work"},
                        {"MCP_HUB_SERVICES_FILE": "missing"}):
            with patch.dict(os.environ, options, clear=True), patch("builtins.open", side_effect=AssertionError), \
                    patch.object(runner.urllib.request, "urlopen", side_effect=AssertionError):
                with self.assertRaises(RuntimeError):
                    runner.fetch("/fixture/item")

    def test_legacy_vault_and_command_time_secret_auth(self):
        secret = "backend-vault-fixture"
        app = vaultwarden_mcp.http_app(secret)
        with patch.object(vaultwarden_mcp, "_secret", return_value={"password": "synthetic-only", "fields": {}}), \
                TestClient(app) as direct:
            ups = [(path, Upstream(path, "https://vault.example.test" + target,
                    lambda: {"x-hub-secret": secret}, transport=httpx2.ASGITransport(app=app)))
                    for path, target in (("/vaultwarden", "/vaultwarden"), ("/vault-secret", "/secret"))]
            with TestClient(mcp_hub.build_app({"fixture": digest("legacy-fixture")}, modules=(), ups=ups)) as legacy:
                for path in ("/vaultwarden", "/vault-secret"):
                    self.assertEqual(legacy.post(path, headers=HEADERS).status_code, 401)
                    self.assertEqual(legacy.post(path, headers=auth("work.fixture-client")).status_code, 401)
                response = legacy.post("/vaultwarden", json=INIT, headers=auth("legacy-fixture"))
                self.assertEqual(response.json()["result"]["serverInfo"]["name"], "vaultwarden")
                response = legacy.post("/vault-secret", json={"path": "/fixture/item"}, headers=auth("legacy-fixture"))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["password"] == "synthetic-only")
            for path in ("/vaultwarden", "/secret"):
                for headers in ({}, {"x-hub-secret": "wrong"}, auth("legacy-fixture")):
                    self.assertEqual(direct.post(path, headers=headers).status_code, 401)
            self.assertEqual(direct.post("/secret", json={"path": "/fixture/item"},
                                        headers={"x-hub-secret": secret}).status_code, 200)

    def test_notion_refresh_owns_only_each_instances_grant(self):
        writes = []
        refreshes = []
        real_grant = notion_grant.Grant

        def call(store, method, body=None):
            environment = "homelab" if "/mcp-hub-homelab/" in store.url else "work"
            self.assertIn("/namespaces/mcp-hub-" + environment + "/secrets/mcp-hub-notion-grant", store.url)
            if method == "PATCH":
                writes.append(store.url)
                return {}
            data = {"token_endpoint": "https://notion.example.test/token", "client_id": environment,
                    "refresh_token": environment + "-refresh-fixture", "expires_at": "0"}
            return {"metadata": {"resourceVersion": "1"},
                    "data": {key: base64.b64encode(value.encode()).decode() for key, value in data.items()}}

        def post(url, form):
            refreshes.append(form["client_id"])
            self.assertTrue(form["refresh_token"] == form["client_id"] + "-refresh-fixture")
            return 200, {"access_token": "fixture-access", "refresh_token": "fixture-rotated", "expires_in": 3600}

        for environment in hub_services.ENVIRONMENTS:
            path = self.home / environment
            _, credentials = fixture(path, environment, names=("notion",))
            credentials["upstreams"]["notion"] = {"url": "https://mcp.notion.com/mcp"}
            (path / "credentials.json").write_text(json.dumps(credentials))
            with patch.object(notion_grant.SecretStore, "_call", call), \
                    patch.object(notion_grant, "Grant", side_effect=lambda store: real_grant(store, post=post)):
                app = mcp_hub.instance_app(environment, path / "services.json", path / "credentials.json")
                self.assertTrue(any(route.path == "/notion" for route in app.routes))
            self.assertIn("--instance " + environment, notion_grant.SecretStore(
                "mcp-hub-" + environment, "mcp-hub-notion-grant").bootstrap)
        self.assertEqual(refreshes, ["homelab", "work"])
        self.assertEqual(len(set(writes)), 2)

    def test_bootstrap_targets_only_selected_namespace_without_auth_flows(self):
        spec = importlib.util.spec_from_file_location("bootstrap_notion", HUB / "tools/bootstrap-notion.py")
        bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap)
        for environment in hub_services.ENVIRONMENTS:
            with patch.object(bootstrap.subprocess, "run") as run:
                bootstrap._kubectl("fixture-context", "get", "secret", bootstrap.NAME,
                                   namespace="mcp-hub-" + environment)
                self.assertEqual(run.call_args.args[0][3:5], ["-n", "mcp-hub-" + environment])
            with patch.object(sys, "argv", ["bootstrap-notion.py", "--instance", environment]), \
                    patch.object(bootstrap, "_json", side_effect=AssertionError), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    bootstrap.main()
            with patch.object(sys, "argv", ["bootstrap-notion.py", "--instance", environment,
                                             "--context", "fixture-context"]), \
                    patch.object(bootstrap, "_json", side_effect=[{"token_endpoint": "https://notion.example.test/token"},
                        {"access_token": "fixture-access", "refresh_token": "fixture-refresh"}]), \
                    patch.object(bootstrap, "_existing", return_value=None) as existing, \
                    patch.object(bootstrap, "_register", return_value=("fixture-client", "")) as register, \
                    patch.object(bootstrap, "_authorize", return_value=("fixture-code", "fixture-verifier")), \
                    patch.object(bootstrap, "_kubectl", return_value=subprocess.CompletedProcess([], 0)) as kubectl, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                bootstrap.main()
                existing.assert_called_once_with("fixture-context", namespace="mcp-hub-" + environment)
                self.assertEqual(register.call_args.kwargs["client_name"], "mcp-hub " + environment)
                self.assertEqual(kubectl.call_args.kwargs["namespace"], "mcp-hub-" + environment)
                self.assertEqual(json.loads(kubectl.call_args.kwargs["stdin"])["metadata"]["namespace"],
                                 "mcp-hub-" + environment)
                self.assertNotIn("fixture-access", output.getvalue())
                self.assertNotIn("fixture-refresh", output.getvalue())
            with patch.object(bootstrap.subprocess, "run") as run:
                bootstrap._kubectl("fixture-context", "get", "secret", bootstrap.NAME)
                self.assertEqual(run.call_args.args[0][3:5], ["-n", "mcp-hub"])


class RenderTests(unittest.TestCase):
    def test_homelab_stable_secret_requires_tracked_input_revision(self):
        annotation = "mcp-hub.epaflix.com/inputs-revision"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copytree(HUB / "files", root / "files", ignore=shutil.ignore_patterns("__pycache__", "clients.json"))
            shutil.copytree(HUB / "instances", root / "instances")
            overlay = root / "fixture-homelab"
            fixture(overlay, "homelab")
            config = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
                      "resources": ["../instances/homelab"],
                      "secretGenerator": [{"name": "mcp-hub-instance", "namespace": "mcp-hub-homelab",
                                           "files": ["services.json", "credentials.json"],
                                           "options": {"disableNameSuffixHash": True}}]}
            (overlay / "kustomization.yaml").write_text(yaml.safe_dump(config))

            def render(path):
                return list(yaml.safe_load_all(subprocess.check_output(["kustomize", "build", str(path)])))

            documents = render(overlay)
            deployment = next(doc for doc in documents if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "mcp-hub")
            template = deployment["spec"]["template"]
            self.assertEqual(deployment["metadata"]["namespace"], "mcp-hub-homelab")
            self.assertEqual(template["metadata"]["annotations"][annotation], "r3")
            volume = next(volume for volume in template["spec"]["volumes"] if volume["name"] == "instance")
            self.assertEqual(volume["secret"]["secretName"], "mcp-hub-instance")
            secret = next(doc for doc in documents if doc["kind"] == "Secret")
            self.assertEqual(secret["metadata"]["name"], "mcp-hub-instance")
            self.assertEqual(secret["metadata"]["namespace"], "mcp-hub-homelab")
            work = render(root / "instances/work")
            work_hub = next(doc for doc in work if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "mcp-hub")
            self.assertEqual(work_hub["spec"]["template"]["metadata"]["annotations"][annotation], "r2")

            credentials = json.loads((overlay / "credentials.json").read_text())
            credentials["upstreams"]["searxng"]["token"] = "synthetic-rotated-upstream"
            (overlay / "credentials.json").write_text(json.dumps(credentials))
            rotated = render(overlay)
            rotated_secret = next(doc for doc in rotated if doc["kind"] == "Secret")
            self.assertNotEqual(rotated_secret["data"], secret["data"])
            self.assertEqual(next(doc for doc in rotated if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "mcp-hub"), deployment)

            path = root / "instances/homelab/kustomization.yaml"
            path.write_text(path.read_text().replace('"r3"', '"synthetic-r4"'))
            revised = render(overlay)
            rolled = next(doc for doc in revised if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "mcp-hub")
            self.assertNotEqual(rolled["spec"]["template"], template)
            self.assertEqual(rolled["spec"]["template"]["metadata"]["annotations"][annotation], "synthetic-r4")
            rolled["spec"]["template"]["metadata"]["annotations"][annotation] = "r3"
            self.assertEqual(revised, rotated)
            self.assertEqual(render(root / "instances/work"), work)

    def test_synthetic_secret_injection_and_independent_rollouts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copytree(HUB / "files", root / "files", ignore=shutil.ignore_patterns("__pycache__", "clients.json"))
            shutil.copytree(HUB / "instances", root / "instances")
            code_hashes = []
            for environment in hub_services.ENVIRONMENTS:
                overlay = root / ("fixture-" + environment)
                fixture(overlay, environment)
                config = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
                          "resources": ["../instances/" + environment],
                          "secretGenerator": [{"name": "mcp-hub-instance", "namespace": "mcp-hub-" + environment,
                                               "files": ["services.json", "credentials.json"]}]}
                (overlay / "kustomization.yaml").write_text(yaml.safe_dump(config))

                def render():
                    result = subprocess.run(["kustomize", "build", str(overlay)], check=True, capture_output=True, text=True)
                    return list(yaml.safe_load_all(result.stdout))

                documents = render()
                deployment = next(doc for doc in documents if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "mcp-hub")
                pod = deployment["spec"]["template"]["spec"]
                self.assertEqual(deployment["metadata"]["namespace"], "mcp-hub-" + environment)
                self.assertEqual(deployment["spec"]["strategy"]["type"], "Recreate")
                self.assertEqual(deployment["spec"]["replicas"], 1)
                container = pod["containers"][0]
                for probe in ("readinessProbe", "livenessProbe"):
                    self.assertEqual(container[probe]["httpGet"]["path"], "/healthz")
                secret = next(doc for doc in documents if doc["kind"] == "Secret")
                volume = next(volume for volume in pod["volumes"] if volume["name"] == "instance")
                self.assertEqual(volume["secret"]["secretName"], secret["metadata"]["name"])
                self.assertFalse(volume["secret"].get("optional", False))
                app = next(doc for doc in documents if doc["kind"] == "ConfigMap" and "mcp_hub.py" in doc["data"])
                self.assertNotIn("clients.json", app["data"])
                code_hashes.append(app["metadata"]["name"])
                role = next(doc for doc in documents if doc["kind"] == "Role")
                self.assertEqual(role["rules"][0]["resourceNames"], ["mcp-hub-notion-grant"])
                binding = next(doc for doc in documents if doc["kind"] == "RoleBinding")
                self.assertEqual(binding["subjects"][0]["namespace"], "mcp-hub-" + environment)
                credentials = json.loads((overlay / "credentials.json").read_text())
                credentials["clients"]["fixture"] = digest(environment + ".rotated-fixture")
                (overlay / "credentials.json").write_text(json.dumps(credentials))
                rolled = next(doc for doc in render() if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "mcp-hub")
                self.assertNotEqual(rolled["spec"]["template"], deployment["spec"]["template"])
            self.assertEqual(len(set(code_hashes)), 1)

    def test_legacy_render_without_decrypting_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "hub"
            shutil.copytree(HUB, root, ignore=shutil.ignore_patterns("*.enc.yaml", "__pycache__", "tests"))
            config = yaml.safe_load((root / "kustomization.yaml").read_text())
            config.pop("generators")
            config["resources"].append("fixture-secrets.yaml")
            secrets = [{"apiVersion": "v1", "kind": "Secret", "metadata": {
                        "name": "mcp-hub-" + name, "namespace": "mcp-hub",
                        "annotations": {"mcp-hub.epaflix.com/revision": "synthetic"}},
                        "stringData": {"fixture": "synthetic-only"}} for name in ("gmail", "vaultwarden")]
            (root / "fixture-secrets.yaml").write_text(yaml.safe_dump_all(secrets))
            (root / "kustomization.yaml").write_text(yaml.safe_dump(config))
            result = subprocess.run(["kustomize", "build", str(root)], check=True, capture_output=True, text=True)
            documents = list(yaml.safe_load_all(result.stdout))
            app = next(doc for doc in documents if doc["kind"] == "ConfigMap" and "mcp_hub.py" in doc["data"])
            self.assertIn("clients.json", app["data"])
            self.assertIn("hub_services.py", app["data"])
            self.assertEqual(sum(doc["kind"] == "Deployment" for doc in documents), 4)
            self.assertNotIn("mcp-hub.epaflix.com/inputs-revision", json.dumps(documents))

    def test_argocd_automates_hubs_and_runtimes_with_distinct_self_heal_policies(self):
        result = subprocess.run(["kustomize", "build", str(ROOT / "2-k3s/11.argocd/apps")],
                                check=True, capture_output=True, text=True)
        apps = {doc["metadata"]["name"]: doc for doc in yaml.safe_load_all(result.stdout)}
        for environment in hub_services.ENVIRONMENTS:
            app = apps["mcp-hub-" + environment]
            self.assertEqual(app["spec"]["syncPolicy"]["automated"], {"selfHeal": True, "prune": True})
            self.assertEqual(app["spec"]["destination"]["namespace"], "mcp-hub-" + environment)
            runtime_sync = apps["t3code-" + environment]["spec"]["syncPolicy"]["automated"]
            self.assertTrue(runtime_sync["prune"])
            self.assertFalse(runtime_sync.get("selfHeal", False))
        self.assertNotIn("vault-reader-homelab", apps)
        self.assertIn("automated", apps["mcp-hub"]["spec"]["syncPolicy"])


if __name__ == "__main__":
    unittest.main()
