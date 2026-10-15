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
SPEC = importlib.util.spec_from_file_location("work_searxng", HUB / "instances/work/searxng.py")
search = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(search)


class WorkSearchTests(unittest.TestCase):
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

    def test_work_render_has_independent_authenticated_backend_and_revision(self):
        docs = list(yaml.safe_load_all(subprocess.check_output(["kustomize", "build", str(HUB / "instances/work")])))
        deployments = {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == "Deployment"}
        self.assertEqual(set(deployments), {"mcp-hub", "searxng-mcp"})
        pod = deployments["searxng-mcp"]["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["containers"][0]["env"][0]["valueFrom"]["secretKeyRef"]["name"], "mcp-hub-searxng")
        self.assertEqual(deployments["mcp-hub"]["spec"]["template"]["metadata"]["annotations"][
            "mcp-hub.epaflix.com/inputs-revision"], "r2")
        for doc in docs:
            self.assertNotEqual(doc["kind"], "Secret")
            if doc["kind"] != "Namespace":
                self.assertEqual(doc["metadata"].get("namespace"), "mcp-hub-work")


if __name__ == "__main__":
    unittest.main()
