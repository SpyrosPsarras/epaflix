#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
import yaml

HUB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUB / "files"))
SPEC = importlib.util.spec_from_file_location("searxng", HUB / "instances/searxng/searxng.py")
search = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(search)


class SearchTests(unittest.TestCase):
    def test_authenticated_search_only_and_no_legacy_defaults(self):
        token = "upstream-fixture"
        with TestClient(search.app(token)) as client:
            self.assertEqual(client.get("/healthz").status_code, 200)
            for credential in (None, "work.fixture", "homelab.fixture", "legacy-fixture"):
                headers = {} if credential is None else {"Authorization": "Bearer " + credential}
                self.assertEqual(client.post("/mcp", headers=headers).status_code, 401)
            headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream",
                       "Content-Type": "application/json"}
            init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "fixture", "version": "0"}}}
            self.assertEqual(client.post("/mcp", headers=headers, json=init).status_code, 200)
            reply = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertEqual([tool["name"] for tool in reply.json()["result"]["tools"]], ["searxng_search"])
            with patch.object(search.searxng_mcp, "_search", return_value={"results": []}) as fetch:
                reply = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "searxng_search", "arguments": {"query": "synthetic"}}})
                self.assertEqual(json.loads(reply.json()["result"]["content"][0]["text"]), {"results": []})
                fetch.assert_called_once()
            for route in ("/gmail", "/drive", "/kubernetes", "/notion", "/vault-secret", "/vaultwarden"):
                self.assertEqual(client.post(route, headers=headers).status_code, 404)

    def test_empty_or_client_prefixed_upstream_credentials_refuse(self):
        for token in ("", "work.fixture", "homelab.fixture", "white space"):
            with self.assertRaises(ValueError):
                search.app(token)

    def test_both_hubs_render_independent_authenticated_search_backends(self):
        for environment in ("homelab", "work"):
            with self.subTest(environment=environment):
                docs = list(yaml.safe_load_all(subprocess.check_output([
                    "kustomize", "build", str(HUB / "instances" / environment)])))
                deployments = {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == "Deployment"}
                self.assertEqual(set(deployments), {"mcp-hub", "searxng-mcp", "workspace-mcp"}
                                 if environment == "homelab" else {"mcp-hub", "searxng-mcp"})
                search_deployment = deployments["searxng-mcp"]
                template = search_deployment["spec"]["template"]
                pod = template["spec"]
                self.assertFalse(pod["automountServiceAccountToken"])
                self.assertEqual(pod["containers"][0]["env"][0]["valueFrom"]["secretKeyRef"],
                                 {"name": "mcp-hub-searxng", "key": "token"})
                self.assertEqual(template["metadata"]["annotations"]["mcp-hub.epaflix.com/inputs-revision"], "r1")
                self.assertEqual(deployments["mcp-hub"]["spec"]["template"]["metadata"]["annotations"][
                    "mcp-hub.epaflix.com/inputs-revision"], "r3")
                selector = search_deployment["spec"]["selector"]["matchLabels"]
                self.assertEqual(selector, {"app": "work-searxng-mcp" if environment == "work" else "searxng-mcp"})
                self.assertEqual(template["metadata"]["labels"], selector)
                service = next(doc for doc in docs if doc["kind"] == "Service" and doc["metadata"]["name"] == "searxng-mcp")
                self.assertEqual(service["spec"]["selector"], selector)
                runner = next(volume["configMap"]["name"] for volume in pod["volumes"] if volume["name"] == "runner")
                config = next(doc for doc in docs if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == runner)
                self.assertIn("searxng.py", config["data"])
                for doc in docs:
                    self.assertNotEqual(doc["kind"], "Secret")
                    if doc["kind"] != "Namespace":
                        self.assertEqual(doc["metadata"].get("namespace"), "mcp-hub-" + environment)


if __name__ == "__main__":
    unittest.main()
