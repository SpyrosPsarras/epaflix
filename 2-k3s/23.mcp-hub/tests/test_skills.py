#!/usr/bin/env python3
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

HUB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUB / "files"))
import hub_services

skills = None
if importlib.util.find_spec("fastmcp"):
    import httpx2
    from starlette.testclient import TestClient
    import mcp_hub
    from upstream import Upstream

    SPEC = importlib.util.spec_from_file_location("skills", HUB / "instances/skills/skills.py")
    skills = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(skills)

TOKEN = "synthetic_skills_fixture_0001"
META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {"extensions": {"io.modelcontextprotocol/skills": {}}}}


def write_skill(root, name, description):
    (root / name).mkdir(parents=True)
    (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\nBody of {name}.\n")


def call(client, path, method, token, params=None):
    params = params or {}
    name = {"Mcp-Name": params["uri"]} if "uri" in params else {}
    return client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {"_meta": META, **params}},
                       headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream",
                                "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method, **name})


def names(reply):
    return sorted(entry["frontmatter"]["name"] for entry in reply.json()["result"]["skills"])


@unittest.skipIf(skills is None, "fastmcp runs in the skills CI venv")
class SkillsBackendTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)

    def test_authenticated_listing_follows_the_synced_link(self):
        first, second = self.base / "first", self.base / "second"
        write_skill(first, "alpha", "Synthetic skill alpha.")
        write_skill(second, "alpha", "Synthetic skill alpha.")
        write_skill(second, "beta", "Synthetic skill beta.")
        link = self.base / "skills"
        link.symlink_to(first)
        with TestClient(skills.app(TOKEN, link)) as client:
            self.assertEqual(client.get("/healthz").status_code, 200)
            for token in ("wrong-token-fixture", "homelab.fixture-client"):
                self.assertEqual(call(client, "/mcp", "skills/list", token).status_code, 401)
            discover = call(client, "/mcp", "server/discover", TOKEN)
            self.assertIn("io.modelcontextprotocol/skills", discover.json()["result"]["capabilities"]["extensions"])
            self.assertEqual(names(call(client, "/mcp", "skills/list", TOKEN)), ["alpha"])
            swap = self.base / "next"
            swap.symlink_to(second)
            os.replace(swap, link)
            self.assertEqual(names(call(client, "/mcp", "skills/list", TOKEN)), ["alpha", "beta"])

    def test_hub_forwards_skills_requests(self):
        write_skill(self.base / "skills", "alpha", "Synthetic skill alpha.")
        backend = skills.app(TOKEN, self.base / "skills")
        upstream = Upstream("skills", "http://backend/mcp", lambda: {"Authorization": "Bearer " + TOKEN},
                            transport=httpx2.ASGITransport(app=backend))
        hub = mcp_hub.build_app({"fixture": hashlib.sha256(b"homelab.fixture").hexdigest()}, modules=(),
                                ups=[("/skills", upstream)], environment="homelab")
        with TestClient(backend), TestClient(hub) as client:
            reply = call(client, "/skills", "skills/list", "homelab.fixture")
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertEqual(names(reply), ["alpha"])
            uri = reply.json()["result"]["skills"][0]["uri"]
            read = call(client, "/skills", "resources/read", "homelab.fixture", {"uri": uri})
            self.assertIn("Body of alpha.", read.json()["result"]["contents"][0]["text"])

    def test_weak_or_client_prefixed_credentials_refuse(self):
        for token in ("", "short", "homelab.fixture-client-token", "white space token value"):
            with self.assertRaises(ValueError):
                skills.app(token, self.base)


class SkillsRenderTests(unittest.TestCase):
    def test_both_hubs_serve_their_own_forgejo_repo(self):
        self.assertEqual(hub_services.PATHS["skills"], "/skills")
        repos = {"homelab": "spypsarras/skills.git", "work": "spypsarras/skills-work.git"}
        for environment, repo in repos.items():
            with self.subTest(environment=environment):
                docs = list(yaml.safe_load_all(subprocess.check_output(
                    ["kustomize", "build", str(HUB / "instances" / environment)])))
                deployment = next(doc for doc in docs if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "skills-mcp")
                pod = deployment["spec"]["template"]["spec"]
                self.assertFalse(pod["automountServiceAccountToken"])
                containers = {container["name"]: container for container in pod["containers"]}
                self.assertRegex(containers["git-sync"]["image"], r"^registry\.k8s\.io/git-sync/git-sync:v[\d.]+@sha256:[0-9a-f]{64}$")
                sync_env = {env["name"]: env for env in containers["git-sync"]["env"]}
                settings = sync_env["GITSYNC_REPO"]["valueFrom"]["configMapKeyRef"]
                config = next(doc for doc in docs if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == settings["name"])
                self.assertEqual(config["data"][settings["key"]],
                                 "http://forgejo.forgejo.svc.cluster.local:3000/" + repo)
                self.assertEqual(sync_env["GITSYNC_USERNAME"]["valueFrom"]["secretKeyRef"], {"name": "skills-git", "key": "username"})
                self.assertEqual(containers["git-sync"]["readinessProbe"]["httpGet"], {"path": "/", "port": "sync"})
                self.assertEqual(sync_env["GITSYNC_HTTP_BIND"]["value"], ":8080")
                serve_env = {env["name"]: env for env in containers["skills"]["env"]}
                self.assertEqual(serve_env["SKILLS_UPSTREAM_TOKEN"]["valueFrom"]["secretKeyRef"],
                                 {"name": "mcp-hub-skills", "key": "token"})
                self.assertEqual(sync_env["GITSYNC_ROOT"]["value"], "/git/root")
                self.assertEqual(serve_env["SKILLS_ROOT"]["value"], "/git/root/" + sync_env["GITSYNC_LINK"]["value"])
                self.assertIn('until [ -e "$SKILLS_ROOT" ]; do sleep 2; done && exec python', containers["skills"]["command"][2])
                mounts = {mount["name"]: mount for mount in containers["skills"]["volumeMounts"]}
                self.assertTrue(mounts["git"]["readOnly"])
                self.assertFalse(any(doc["kind"] == "Secret" for doc in docs))
                self.assertTrue(all(doc["metadata"].get("namespace") == "mcp-hub-" + environment
                                    for doc in docs if doc["kind"] != "Namespace"))

    def test_forgejo_admits_only_the_skills_backends(self):
        policy = next(doc for doc in yaml.safe_load_all((HUB.parent / "38.forgejo/network-policy.yaml").read_text())
                      if doc["metadata"]["name"] == "forgejo-ingress")
        peers = policy["spec"]["ingress"][0]["from"]
        for environment in ("homelab", "work"):
            self.assertIn({"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "mcp-hub-" + environment}},
                           "podSelector": {"matchLabels": {"app": "skills-mcp"}}}, peers)


if __name__ == "__main__":
    unittest.main()
