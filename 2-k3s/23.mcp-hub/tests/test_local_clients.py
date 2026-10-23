#!/usr/bin/env python3
"""Internal ingress and local machine client boundaries, using fixtures only."""

import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
import urllib.error

import yaml

HUB = Path(__file__).resolve().parents[1]
ROOT = HUB.parents[1]
sys.path.insert(0, str(HUB / "tools"))


class IngressTests(unittest.TestCase):
    def test_each_instance_uses_only_internal_traefik_and_lan_dns(self):
        vault = yaml.safe_load((HUB.parent / "26.vaultwarden/ingress.yaml").read_text())
        for environment in ("homelab", "work"):
            documents = list(yaml.safe_load_all(subprocess.check_output(
                ["kustomize", "build", str(HUB / "instances" / environment)])))
            routes = [doc for doc in documents if doc["kind"] == "IngressRoute"]
            self.assertEqual(len(routes), 1)
            route = routes[0]
            host = "mcp-" + environment + ".epaflix.com"
            self.assertEqual(route["metadata"]["namespace"], "mcp-hub-" + environment)
            self.assertEqual(route["spec"]["entryPoints"], ["internal"])
            self.assertEqual(route["spec"]["tls"], vault["spec"]["tls"])
            self.assertEqual(route["spec"]["routes"], [{"kind": "Rule", "match": "Host(`" + host + "`)",
                "services": [{"name": "mcp-hub", "port": 8000}]}])
            self.assertNotIn("Ingress", [doc["kind"] for doc in documents])
            deployment = next(doc for doc in documents if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "mcp-hub")
            annotations = deployment["spec"]["template"]["metadata"]["annotations"]
            self.assertEqual(annotations["mcp-hub.epaflix.com/inputs-revision"], "r4" if environment == "homelab" else "r3")
            self.assertNotIn("mcp-hub.epaflix.com/local-clients-revision", annotations)
            dns = ROOT / "1-proxmox/pihole"
            self.assertIn("address=/" + host + "/192.168.10.102", (dns / "dnsmasq.d/10-epaflix.conf").read_text())
            self.assertIn('local-zone: "' + host + '." static', (dns / "unbound-no-aaaa-leak.conf").read_text())


class LocalClientTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("local_clients", HUB / "tools/local-clients.py")
        self.clients = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.clients)
        self.environment = "homelab"
        self.namespace = "mcp-hub-homelab"
        self.services = {"environment": "homelab", "services": ["searxng"]}
        self.credentials = {"clients": {"t3code": "a" * 64}, "upstreams": {
            "searxng": {"url": "https://homelab.example.test/mcp", "token": "synthetic-upstream"}}}
        self.resources = {"mcp-hub-instance": {"apiVersion": "v1", "kind": "Secret", "metadata": {
            "name": "mcp-hub-instance", "namespace": self.namespace, "resourceVersion": "1"}, "data": {
                "credentials.json": self.encode(json.dumps(self.credentials)),
                "services.json": self.encode(json.dumps(self.services)), "another-ticket": self.encode("preserve")}}}
        self.conflict = False
        self.patches = 0

    @staticmethod
    def encode(value):
        return base64.b64encode(value.encode()).decode()

    def kubectl(self, argv, **kwargs):
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(argv[:5], ["kubectl", "--context", "epaflix", "-n", self.namespace])
        args = argv[5:]
        result = subprocess.CompletedProcess(argv, 0, "", "")
        if args[:2] == ["get", "secret"]:
            if args[2] not in self.resources:
                result.returncode, result.stderr = 1, "NotFound"
            elif args[-1].startswith("jsonpath="):
                result.stdout = self.resources[args[2]]["data"]["services.json"]
            else:
                result.stdout = json.dumps(copy.deepcopy(self.resources[args[2]]))
        elif args == ["create", "-f", "-"]:
            document = json.loads(kwargs["input"])
            name = document["metadata"]["name"]
            if name in self.resources:
                result.returncode, result.stderr = 1, "AlreadyExists"
            else:
                self.resources[name] = document
        elif args == ["patch", "secret", "mcp-hub-instance", "--type=json", "--patch-file=/dev/stdin"]:
            document = self.resources["mcp-hub-instance"]
            patch_doc = json.loads(kwargs["input"])
            self.patches += 1
            if self.conflict:
                self.conflict = False
                updated = json.loads(base64.b64decode(document["data"]["credentials.json"]))
                updated["clients"]["parallel-ticket"] = "b" * 64
                document["data"]["credentials.json"] = self.encode(json.dumps(updated))
                document["metadata"]["resourceVersion"] = "2"
            if patch_doc[0] != {"op": "test", "path": "/metadata/resourceVersion",
                                "value": document["metadata"]["resourceVersion"]}:
                result.returncode, result.stderr = 1, "Conflict"
            else:
                self.assertEqual(len(patch_doc), 2)
                self.assertEqual(patch_doc[1]["path"], "/data/credentials.json")
                document["data"]["credentials.json"] = patch_doc[1]["value"]
                document["metadata"]["resourceVersion"] = str(int(document["metadata"]["resourceVersion"]) + 1)
        else:
            self.fail("unexpected kubectl operation")
        return result

    def test_provision_is_append_only_retries_concurrent_writes_and_reuses_tokens(self):
        self.conflict = True
        with patch.object(self.clients.subprocess, "run", side_effect=self.kubectl), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.clients.provision("epaflix", "homelab", "homepc")
            original = copy.deepcopy(self.resources)
            self.clients.provision("epaflix", "homelab", "homepc")
            self.clients.provision("epaflix", "homelab", "laptop")
        self.assertEqual(self.patches, 3)
        current = json.loads(base64.b64decode(self.resources["mcp-hub-instance"]["data"]["credentials.json"]))
        self.assertEqual(set(current["clients"]), {"t3code", "parallel-ticket", "homepc", "laptop"})
        self.assertEqual(current["upstreams"], self.credentials["upstreams"])
        self.assertEqual(current["clients"]["t3code"], "a" * 64)
        self.assertEqual(self.resources["mcp-hub-instance"]["data"]["another-ticket"], self.encode("preserve"))
        values = []
        for machine in ("homepc", "laptop"):
            token = base64.b64decode(self.resources["mcp-hub-client-" + machine]["data"]["token"]).decode()
            self.assertTrue(token.startswith("homelab."))
            self.assertEqual(len(token), 51)
            self.assertEqual(hashlib.sha256(token.encode()).hexdigest(), current["clients"][machine])
            self.assertNotIn(token, output.getvalue())
            self.assertNotIn(token, json.dumps(current))
            values.append(token)
        self.assertNotEqual(values[0], values[1])
        self.assertEqual(original["mcp-hub-client-homepc"], self.resources["mcp-hub-client-homepc"])

    def test_work_clients_are_independent_from_homelab_clients(self):
        staged = []
        for environment in ("homelab", "work"):
            self.namespace = "mcp-hub-" + environment
            self.resources = {"mcp-hub-instance": copy.deepcopy(self.resources["mcp-hub-instance"])}
            with patch.object(self.clients.subprocess, "run", side_effect=self.kubectl):
                for machine in ("homepc", "laptop"):
                    self.clients.provision("epaflix", environment, machine)
                    value = self.clients.token(self.resources["mcp-hub-client-" + machine], environment)
                    self.assertTrue(value.startswith(environment + "."))
                    staged.append(value)
            instance = self.resources["mcp-hub-instance"]
            instance["data"]["credentials.json"] = self.encode(json.dumps(self.credentials))
        self.assertEqual(len(set(staged)), 4)

    def test_owner_vault_transfer_uses_stdin_and_never_emits_the_token(self):
        with patch.object(self.clients.subprocess, "run", side_effect=self.kubectl):
            self.clients.provision("epaflix", "homelab", "homepc")
        value = self.clients.token(self.resources["mcp-hub-client-homepc"], "homelab")
        writes = []

        def run(argv, **kwargs):
            for arg in argv:
                self.assertNotIn(value, arg)
            self.assertTrue(kwargs["capture_output"])
            if argv[0] == "kubectl":
                return self.kubectl(argv, **kwargs)
            if argv == ["bw", "status"]:
                output = json.dumps({"status": "unlocked"})
            elif argv == ["bw", "get", "template", "item"]:
                output = json.dumps({"name": "", "type": 1})
            elif argv == ["bw", "encode"]:
                item = json.loads(kwargs["input"])
                self.assertEqual(item["login"]["password"], value)
                output = base64.b64encode(kwargs["input"].encode()).decode()
            elif argv == ["bw", "create", "item"]:
                writes.append(json.loads(base64.b64decode(kwargs["input"])))
                output = json.dumps(writes[-1])
            else:
                self.fail("unexpected vault operation")
            return subprocess.CompletedProcess(argv, 0, output, "")

        with patch.object(self.clients.subprocess, "run", side_effect=run), \
                patch("builtins.input", return_value="fixture title"), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.clients.vault("epaflix", "homelab", "homepc")
        self.assertEqual(len(writes), 1)
        self.assertNotIn(value, output.getvalue())
        self.assertEqual(writes[0]["login"]["username"], "homepc")
        with patch.object(self.clients.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, '{"status":"locked"}', "")) as run, self.assertRaises(RuntimeError):
            self.clients.vault("epaflix", "homelab", "homepc")
        self.assertEqual(run.call_count, 1)

    def test_local_check_initializes_both_hubs_and_requires_cross_hub_401(self):
        tokens = {environment: environment + "." + "x" * 43 for environment in ("homelab", "work")}
        opened = []

        def request(req, **kwargs):
            environment = "homelab" if "mcp-homelab." in req.full_url else "work"
            opened.append(environment)
            own = req.get_header("Authorization") == "Bearer " + tokens[environment]
            if not own:
                raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO())
            self.assertEqual(json.loads(req.data)["method"], "initialize")
            result = Mock()
            result.status = 200
            result.headers = {"content-type": "application/json"}
            result.read.return_value = b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}'
            result.__enter__ = Mock(return_value=result)
            result.__exit__ = Mock(return_value=False)
            return result

        def staged(context, environment, name):
            return {"data": {"token": self.encode(tokens[environment])}}

        with patch.object(self.clients, "secret", side_effect=staged), \
                patch.object(self.clients, "services", side_effect=lambda context, environment: {
                    "environment": environment, "services": ["searxng"]}), \
                patch.object(self.clients.urllib.request, "build_opener") as build, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            build.return_value.open.side_effect = request
            self.clients.check("epaflix", "homepc")
            self.assertEqual(opened, ["homelab", "homelab", "work", "work"])
            for value in tokens.values():
                self.assertNotIn(value, output.getvalue())
            build.return_value.open.side_effect = None
            build.return_value.open.return_value = MagicMock()
            with self.assertRaises(RuntimeError):
                self.clients.check("epaflix", "homepc")

    def test_cli_dispatches_each_action_and_validates_its_required_options(self):
        for action, options, expected in (
                ("provision", ["--instance", "homelab", "--machine", "homepc"], ("epaflix", "homelab", "homepc")),
                ("vault", ["--instance", "work", "--machine", "laptop"], ("epaflix", "work", "laptop")),
                ("configure", ["--instance", "work", "--config", "/tmp/fixture-mcp.json"],
                 ("epaflix", "work", Path("/tmp/fixture-mcp.json"))),
                ("check", ["--machine", "homepc"], ("epaflix", "homepc"))):
            argv = ["local-clients.py", action, "--context", "epaflix", *options]
            with self.subTest(action=action), patch.object(sys, "argv", argv), \
                    patch.object(self.clients, action) as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.clients.main(), 0)
                run.assert_called_once_with(*expected)
            with patch.object(sys, "argv", argv[:4]), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                self.clients.main()
            self.assertEqual(error.exception.code, 2)

    def test_cli_failure_suppresses_command_output_and_credential_details(self):
        with patch.object(sys, "argv", ["local-clients.py", "provision", "--context", "epaflix",
                                       "--instance", "work", "--machine", "laptop"]), \
                patch.object(self.clients, "provision", side_effect=RuntimeError("synthetic-sensitive-error")), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(self.clients.main(), 1)
        self.assertNotIn("synthetic-sensitive-error", stderr.getvalue())

    def test_missing_instance_and_existing_different_grant_never_get_overwritten(self):
        for missing in (False, True):
            if missing:
                self.resources.clear()
            else:
                credentials = copy.deepcopy(self.credentials)
                credentials["clients"]["homepc"] = "c" * 64
                self.resources["mcp-hub-instance"]["data"]["credentials.json"] = self.encode(json.dumps(credentials))
            before = copy.deepcopy(self.resources)
            with patch.object(self.clients.subprocess, "run", side_effect=self.kubectl), self.assertRaises(RuntimeError):
                self.clients.provision("epaflix", "homelab", "homepc")
            self.assertEqual(before, self.resources)

    def test_local_pi_config_keeps_both_hubs_and_unrelated_entries_without_secret_values(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "mcp.json"
            initial = {"mcpServers": {"mine": {"command": "example"},
                "homelab-drive": {"url": "https://old.example.test/drive"},
                "vaultwarden": {"url": "https://legacy.example.test/vaultwarden"}}}
            target.write_text(json.dumps(initial))
            with patch.object(self.clients.subprocess, "run", side_effect=self.kubectl):
                self.clients.configure("epaflix", "homelab", target)
            self.environment, self.namespace = "work", "mcp-hub-work"
            self.services = {"environment": "work", "services": ["searxng", "kubernetes"]}
            self.resources["mcp-hub-instance"]["data"]["services.json"] = self.encode(json.dumps(self.services))
            with patch.object(self.clients.subprocess, "run", side_effect=self.kubectl):
                self.clients.configure("epaflix", "work", target)
                before = target.stat().st_mtime_ns
                self.clients.configure("epaflix", "work", target)
                self.assertEqual(before, target.stat().st_mtime_ns)
            config = json.loads(target.read_text())["mcpServers"]
            self.assertEqual(set(config), {"mine", "vaultwarden", "homelab-searxng", "work-searxng", "work-kubernetes-epaflix"})
            for environment in ("homelab", "work"):
                entry = config[environment + "-searxng"]
                self.assertEqual(entry["url"], "https://mcp-" + environment + ".epaflix.com/searxng")
                self.assertEqual(entry["headers"], {"Authorization":
                    "!echo Bearer $(secret-tool lookup service mcp-hub key " + environment + ")"})
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
