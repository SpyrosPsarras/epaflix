"""Synthetic authenticated hub -> connector -> subprocess reader composition."""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import contextlib
from dataclasses import asdict
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

import anyio
import httpx2
from starlette.testclient import TestClient

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "files"),
               str(Path(__file__).resolve().parents[1] / "tools")]
import hub_clients
import hub_services
import mcp_hub
import upstream
from upstream import Upstream
import vaultwarden_reader as core
import vaultwarden_reader_http as adapter
import test_vaultwarden_reader as fixture

META_TOKEN = "synthetic-metadata-connector-token-0001"
BROKER_TOKEN = "synthetic-broker-connector-token-00001"
CLIENT = "homelab.synthetic-client"
WORK_CLIENT = "work.synthetic-client"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "0"}}}


def auth(token):
    return {**HEADERS, "Authorization": "Bearer " + token}


def private_file(path, value):
    path.write_text(value)
    path.chmod(0o600)
    return path


class RecordingTransport(httpx2.ASGITransport):
    def __init__(self, app):
        super().__init__(app=app)
        self.seen = []

    async def handle_async_request(self, request):
        self.seen.append((request.url.path, dict(request.headers)))
        return await super().handle_async_request(request)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.ReaderTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root = self.case.root
        spec = asdict(self.case.config)
        for name in ("credentials_dir", "scratch_dir", "executable"):
            spec[name] = str(spec[name])
        spec["collection_ids"] = list(spec["collection_ids"])
        self.private = {"environment": "homelab", "reader": spec,
                        "metadata_token_file": str(private_file(self.root / "metadata-token", META_TOKEN)),
                        "broker_token_file": str(private_file(self.root / "broker-token", BROKER_TOKEN))}
        self.config_file = private_file(self.root / "connector.json", json.dumps(self.private))
        config, metadata, broker = adapter.load_config(self.config_file)
        self.case.reader = core.RestrictedVaultReader(config)
        self.connector = adapter.http_app(self.case.reader, metadata, broker)
        self.transport = RecordingTransport(self.connector)
        self.services = {"environment": "homelab", "services": ["vaultwarden", "vault-secret", "vault-attachment"],
                         "vault_reader": "restricted-v1"}
        self.credentials = {"clients": {"synthetic": hashlib.sha256(CLIENT.encode()).hexdigest(),
                                       "copied-work": hashlib.sha256(WORK_CLIENT.encode()).hexdigest()},
                            "upstreams": {name: {"url": "http://reader.example.test" + hub_services.READER_PATHS[name],
                                                  "token": META_TOKEN if name == "vaultwarden" else BROKER_TOKEN,
                                                  "protocol": "restricted-v1"}
                                          for name in self.services["services"]}}
        self.save_hub()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.direct = self.stack.enter_context(TestClient(self.connector))
        self.hub = self.stack.enter_context(TestClient(self.build_hub()))

    def save_hub(self):
        self.services_file = private_file(self.root / "services.json", json.dumps(self.services))
        self.credentials_file = private_file(self.root / "hub.json", json.dumps(self.credentials))

    def build_hub(self):
        with patch.object(mcp_hub, "upstreams", side_effect=AssertionError("legacy must not be selected")), \
                patch.object(mcp_hub, "Upstream", side_effect=lambda *args, **kwargs:
                             Upstream(*args, transport=self.transport, **kwargs)):
            return mcp_hub.instance_app("homelab", self.services_file, self.credentials_file)

    def mcp(self, name, arguments):
        return self.hub.post("/vaultwarden", headers=auth(CLIENT), json={"jsonrpc": "2.0", "id": 3,
                             "method": "tools/call", "params": {"name": name, "arguments": arguments}})

    def broker(self, path="/vault-secret", reference=fixture.REF):
        value = {"reference": reference}
        if path == "/vault-attachment":
            value["attachment"] = "demo.txt"
        return self.hub.post(path, headers=auth(CLIENT), json=value)

    def assert_failure(self, response):
        self.assertTrue(response.status_code >= 400 or "error" in response.json() or
                        response.json().get("result", {}).get("isError"), response.text)
        self.assertNotIn("synthetic-", response.text)
        self.assertNotIn(str(self.root), response.text)
        self.assertEqual(response.headers.get("cache-control"), "no-store")

    def test_success_metadata_secret_attachment_and_empty_results(self):
        reply = self.hub.post("/vaultwarden", headers=auth(CLIENT), json=INIT)
        self.assertEqual(reply.json()["result"]["serverInfo"]["name"], "homelab-vault-reader")
        listed = self.mcp("vault_list", {})
        got = self.mcp("vault_get", {"reference": fixture.REF})
        values = json.loads(listed.json()["result"]["content"][0]["text"])
        self.assertEqual(values, [json.loads(got.json()["result"]["content"][0]["text"])])
        self.assertNotIn("synthetic-", got.text)
        secret = self.broker()
        self.assertEqual(secret.status_code, 200)
        self.assertEqual(secret.json(), self.case.reader.secret(fixture.REF))
        attachment = self.broker("/vault-attachment")
        self.assertEqual(attachment.status_code, 200)
        self.assertEqual(attachment.json()["filename"], "demo.txt")
        self.assertEqual(base64.b64decode(attachment.json()["content_b64"]), b"synthetic-attachment-bytes\0\xff")
        self.case.scenario["items"] = []
        self.case.save()
        self.assertEqual(json.loads(self.mcp("vault_list", {}).json()["result"]["content"][0]["text"]), [])
        self.assert_failure(self.broker())
        self.assertEqual(sum(c["args"][0] == "login" for c in self.case.calls()), 1)

    def test_broker_burst_must_survive_pinned_default_identity_login_budget(self):
        self.case.scenario["identity_login_limit"] = 10
        self.case.save()
        for _ in range(11):
            reply = self.broker()
            self.assertEqual(reply.status_code, 200, "ordinary broker reads must not exhaust API-key login limits")
        self.assertEqual(sum(c["args"][0] == "login" for c in self.case.calls()), 1)
        self.assertEqual(sum(c["args"][0] == "reader-sync" for c in self.case.calls()), 11)
        self.assertEqual(len((self.case.root / "identity-calls.jsonl").read_text().splitlines()), 1)

    def test_sync_identity_failure_after_success_never_serves_cached_data(self):
        self.case.scenario["identity_login_limit"] = 1
        self.case.save()
        self.assertEqual(self.broker().status_code, 200)
        self.case.scenario["token_expired"] = True
        self.case.save()
        self.assert_failure(self.broker())
        before = len(self.case.calls())
        self.assert_failure(self.broker())
        self.assertEqual(len(self.case.calls()), before)
        self.assertEqual(list(self.case.config.scratch_dir.iterdir()), [])

    def test_broker_expiry_renews_and_fetches_changed_data(self):
        self.assertEqual(self.broker().status_code, 200)
        self.case.scenario["token_expired"] = True
        self.case.scenario["items"][0]["login"]["password"] = "synthetic-renewed-secret"
        self.case.save()
        reply = self.broker()
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json()["password"], "synthetic-renewed-secret")
        identities = [json.loads(line) for line in (self.case.root / "identity-calls.jsonl").read_text().splitlines()]
        self.assertEqual([c["command"] for c in identities], ["login", "sync"])

    def test_wrong_bridge_capability_never_logs_in_or_falls_back(self):
        self.case.scenario["bridge_capability"] = {"bridge": "stock"}
        self.case.save()
        self.assert_failure(self.broker())
        self.assertEqual([c["args"] for c in self.case.calls()], [["--reader-capabilities"]])
        self.assertEqual(list(self.case.config.scratch_dir.iterdir()), [])

    def test_old_sync_only_bridge_refuses_before_login_and_stays_latched(self):
        self.case.scenario["bridge_capability"] = {**core.BRIDGE_CAPABILITY, "bridge": "restricted-sync-v1"}
        self.case.save()
        self.assert_failure(self.broker())
        self.assertEqual([c["args"] for c in self.case.calls()], [["--reader-capabilities"]])
        self.case.scenario.pop("bridge_capability")
        self.case.save()
        self.assert_failure(self.broker())
        self.assertEqual(len(self.case.calls()), 1)
        self.assertEqual(list(self.case.config.scratch_dir.iterdir()), [])

    def test_login_or_unlock_refusal_latches_all_hub_read_routes(self):
        for stage in ("login", "unlock"):
            with self.subTest(stage=stage):
                self.case.reader.invalidate()
                self.case.scenario["fail"] = stage
                self.case.save()
                self.assert_failure(self.broker())
                self.assertEqual(self.case.calls()[-1]["args"][0], stage)
                self.case.scenario.pop("fail")
                self.case.save()
                before = len(self.case.calls())
                for path in ("/vault-secret", "/vault-attachment"):
                    self.assert_failure(self.broker(path))
                for name, arguments in (("vault_list", {}), ("vault_get", {"reference": fixture.REF})):
                    self.assert_failure(self.mcp(name, arguments))
                self.assertEqual(len(self.case.calls()), before)
                self.assertEqual(list(self.case.config.scratch_dir.iterdir()), [])

    def test_direct_rest_metadata_and_authentication_separation(self):
        for path, value in (("/metadata/list", {}), ("/metadata/get", {"reference": fixture.REF})):
            self.assertEqual(self.direct.post(path, headers=auth(META_TOKEN), json=value).status_code, 200)
            self.assertEqual(self.direct.post(path, headers=auth(BROKER_TOKEN), json=value).status_code, 401)
        for path in ("/secret", "/attachment"):
            for token in (None, CLIENT, WORK_CLIENT, META_TOKEN, "wrong"):
                headers = HEADERS if token is None else auth(token)
                self.assertEqual(self.direct.post(path, headers=headers, json={"reference": fixture.REF}).status_code, 401)
            self.assertEqual(self.direct.get(path, headers=auth(BROKER_TOKEN)).status_code, 405)
        for method in ("get", "delete"):
            self.assertEqual(getattr(self.direct, method)("/vaultwarden", headers=auth(META_TOKEN)).status_code, 405)
            self.assertEqual(getattr(self.hub, method)("/vaultwarden", headers=auth(CLIENT)).status_code, 405)
        self.assertEqual(self.direct.post("/vaultwarden", headers=auth(BROKER_TOKEN), json=INIT).status_code, 401)
        self.assertEqual(self.direct.post("/secret", headers={"X-Hub-Secret": BROKER_TOKEN}, json={}).status_code, 401)
        self.assertEqual(self.direct.get("/healthz").status_code, 200)
        self.assertEqual(self.direct.post("/healthz").status_code, 401)

    def test_cross_environment_auth_and_forwarded_headers(self):
        for path in ("/vaultwarden", "/vault-secret", "/vault-attachment"):
            for token in (WORK_CLIENT, "legacy.synthetic-client", "wrong"):
                self.assertEqual(self.hub.post(path, headers=auth(token), json=INIT).status_code, 401)
        self.assertEqual(self.transport.seen, [])
        headers = {**auth(CLIENT), "X-Hub-Secret": "caller-supplied-secret", "BW_PASSWORD": "caller-password",
                   "Cookie": "private-cookie", "Mcp-Protocol-Version": "2025-06-18"}
        self.hub.post("/vaultwarden", headers=headers, json=INIT)
        path, forwarded = self.transport.seen[-1]
        self.assertEqual(path, "/vaultwarden")
        self.assertEqual(forwarded["authorization"], "Bearer " + META_TOKEN)
        self.assertEqual(forwarded["mcp-protocol-version"], "2025-06-18")
        self.assertFalse({"x-hub-secret", "bw_password", "cookie"}.intersection(forwarded))
        self.broker()
        self.assertEqual(self.transport.seen[-1][1]["authorization"], "Bearer " + BROKER_TOKEN)
        before = len(self.case.calls())
        self.assert_failure(self.hub.post("/vault-secret", headers=headers, json={
                            "reference": fixture.REF, "password": "caller-password"}))
        self.assertEqual(len(self.case.calls()), before)

    def test_ambiguity_out_of_scope_and_reference_errors_never_fall_back(self):
        outside = fixture.item(fixture.OTHER_ITEM)
        outside["collectionIds"] = [fixture.OTHER_COLLECTION]
        self.case.scenario["items"].append(outside)
        self.case.save()
        references = [{"collection_id": fixture.COLLECTION, "item_id": fixture.OTHER_ITEM},
                      {"collection_id": fixture.OTHER_COLLECTION, "item_id": fixture.ITEM},
                      {"collection_id": fixture.COLLECTION, "title": "absent"}, "/homelab/demo", {}]
        for reference in references:
            self.assert_failure(self.mcp("vault_get", {"reference": reference}))
            self.assert_failure(self.broker(reference=reference))
            self.assert_failure(self.broker("/vault-attachment", reference))
        outside["collectionIds"] = [fixture.COLLECTION]
        self.case.save()
        ambiguous = {"collection_id": fixture.COLLECTION, "title": "demo"}
        for path in ("/vault-secret", "/vault-attachment"):
            self.assert_failure(self.broker(path, ambiguous))
        self.assert_failure(self.mcp("vault_get", {"reference": ambiguous}))
        self.assertEqual(self.broker().status_code, 200)
        self.assertEqual(sum(c["args"][0] == "login" for c in self.case.calls()), 1)
        self.assertFalse(any(c["args"][0] == "get" for c in self.case.calls()))

    def test_missing_revoked_extra_grants_and_roles_block_all_routes(self):
        original = list(self.case.scenario["collections"])
        for grants in ([], [*original, {"id": fixture.OTHER_COLLECTION, "organizationId": fixture.ORG}],
                       [{"id": fixture.COLLECTION, "organizationId": fixture.OTHER_ORG}]):
            for operation in (lambda: self.mcp("vault_list", {}), lambda: self.mcp("vault_get", {"reference": fixture.REF}),
                              self.broker, lambda: self.broker("/vault-attachment")):
                self.case.reader.invalidate()
                self.case.scenario["collections"] = original
                self.case.save()
                self.assertEqual(self.broker().status_code, 200)
                self.case.scenario["collections"] = grants
                self.case.save()
                before = len(self.case.calls())
                self.assert_failure(operation())
                new = self.case.calls()[before:]
                self.assertEqual(sum(c["args"][0] == "reader-sync" for c in new), 1)
                self.assertFalse(any(c["args"] == ["list", "items"] or c["args"][0] == "get" for c in new))
                self.assertEqual(list(self.case.config.scratch_dir.iterdir()), [])
        self.case.reader.invalidate()
        self.case.scenario["collections"] = original
        self.case.scenario["organizations"][0]["type"] = 0
        self.case.save()
        self.assert_failure(self.broker())

    def test_refresh_failure_after_success_blocks_metadata_and_brokers(self):
        for scenario in ({"fail": "sync"}, {"stale_sync": True}, {"status_after": {"userId": fixture.OTHER_ITEM}},
                         {"status_locked": {"userId": fixture.OTHER_ITEM}}):
            for operation in (lambda: self.mcp("vault_list", {}), lambda: self.mcp("vault_get", {"reference": fixture.REF}),
                              self.broker, lambda: self.broker("/vault-attachment")):
                self.case.reader.invalidate()
                for key in ("fail", "stale_sync", "status_after", "status_locked"):
                    self.case.scenario.pop(key, None)
                self.case.save()
                self.assertEqual(self.broker().status_code, 200)
                if "status_locked" in scenario:
                    self.case.reader.invalidate()
                self.case.scenario.update(scenario)
                self.case.save()
                self.assert_failure(operation())
                before = len(self.case.calls())
                self.assert_failure(self.broker())
                self.assertEqual(len(self.case.calls()), before)

    def test_attachment_ownership_and_unsafe_paths(self):
        for attachment in ("../outside", "/tmp/demo.txt", "other.txt", "demo", "b" * 32):
            self.assert_failure(self.hub.post("/vault-attachment", headers=auth(CLIENT),
                                json={"reference": fixture.REF, "attachment": attachment}))
        self.assertFalse(any(c["args"][0] == "get" for c in self.case.calls()))

    def test_only_metadata_tools_and_disabled_writes(self):
        response = self.hub.post("/vaultwarden", headers=auth(CLIENT), json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual({t["name"] for t in response.json()["result"]["tools"]}, {"vault_list", "vault_get"})
        for name in ("vault_add", "vault_update", "vault_trash", "vault_attach", "vault_attachment", "secret", "execute"):
            self.assert_failure(self.mcp(name, {"reference": fixture.REF}))
        for path in ("/create", "/update", "/trash", "/upload", "/execute", "/serve", "/attachment/upload"):
            self.assertEqual(self.direct.post(path, headers=auth(BROKER_TOKEN), json={}).status_code, 401)
            self.assertEqual(self.hub.post(path, headers=auth(CLIENT), json={}).status_code, 404)
        self.assertEqual(self.case.calls(), [])

    def test_request_response_and_query_bounds(self):
        for client, path, token in ((self.hub, "/vault-secret", CLIENT), (self.direct, "/secret", BROKER_TOKEN),
                                    (self.hub, "/vaultwarden", CLIENT), (self.direct, "/vaultwarden", META_TOKEN)):
            self.assertEqual(client.post(path, content=b"x" * 16385, headers=auth(token)).status_code, 413)
            self.assertEqual(client.post(path + "?password=caller-secret", json={}, headers=auth(token)).status_code, 400)
        self.assertEqual(self.case.calls(), [])
        for value in (b"invalid JSON", b"[]", b'{"reference":{},"reference":{}}', b'{"path":"/homelab/demo"}'):
            self.assert_failure(self.hub.post("/vault-secret", content=value, headers=auth(CLIENT)))
        with patch.object(adapter, "RESPONSE_LIMIT", 10):
            self.assert_failure(self.broker())
        with patch.object(upstream, "READER_RESPONSE_LIMIT", 10):
            reply = self.broker()
            self.assert_failure(reply)
            self.assertIn("response limit exceeded", reply.text)

    def test_connector_unavailable_and_auth_failure_are_sanitized_no_fallback(self):
        self.case.config.executable.unlink()
        self.assert_failure(self.broker())
        self.assert_failure(self.mcp("vault_get", {"reference": fixture.REF}))
        self.credentials["upstreams"]["vault-secret"]["token"] = "synthetic-incorrect-broker-token-000001"
        self.credentials["upstreams"]["vault-attachment"]["token"] = "synthetic-incorrect-broker-token-000001"
        self.save_hub()
        with TestClient(self.build_hub()) as client:
            response = client.post("/vault-secret", headers=auth(CLIENT), json={"reference": fixture.REF})
            self.assertEqual(response.status_code, 502)
            self.assertNotIn("synthetic-", response.text)

    def test_chunked_and_slow_request_bodies_are_bounded_before_cli(self):
        async def run():
            for app, path, token in ((self.connector, "/secret", BROKER_TOKEN), (self.build_hub(), "/vault-secret", CLIENT)):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://fixture.example.test") as client:
                    async def chunks():
                        yield b"x" * 8192
                        yield b"x" * 8193

                    response = await client.post(path, headers=auth(token), content=chunks())
                    self.assertEqual(response.status_code, 413)

                    async def slow():
                        await anyio.sleep(1)
                        yield b"{}"

                    with patch.object(adapter, "BODY_TIMEOUT", 0.05), patch.object(upstream, "READER_BODY_TIMEOUT", 0.05):
                        response = await client.post(path, headers=auth(token), content=slow())
                        self.assertIn(response.status_code, (408, 502))
        anyio.run(run)
        self.assertEqual(self.case.calls(), [])

    def test_request_admission_has_no_unbounded_body_wait_queue(self):
        async def run():
            for app, path, token in ((self.connector, "/secret", BROKER_TOKEN), (self.build_hub(), "/vault-secret", CLIENT)):
                started = []
                release = anyio.Event()

                async def body():
                    started.append(True)
                    await release.wait()
                    yield b"{}"

                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://fixture.example.test") as client:
                    replies = []

                    async def admitted():
                        replies.append(await client.post(path, headers=auth(token), content=body()))

                    async with anyio.create_task_group() as group:
                        for _ in range(8):
                            group.start_soon(admitted)
                        with anyio.fail_after(2):
                            while len(started) < 8:
                                await anyio.sleep(0.01)
                        for _ in range(5):
                            reply = await client.post(path, headers=auth(token), json={})
                            self.assertEqual(reply.status_code, 503)
                        release.set()
                    self.assertEqual(len(replies), 8)
        anyio.run(run)
        self.assertEqual(self.case.calls(), [])

    def test_hub_bounds_and_sanitizes_untrusted_upstream_failures(self):
        for status in (401, 403, 302, 400, 503, 200):
            async def response(request, status=status):
                return httpx2.Response(status, text="synthetic-upstream-secret", headers={"location": "https://other.example.test"})

            backend = Upstream("vault-secret", "http://reader.example.test/secret", lambda: {"Authorization": "Bearer " + BROKER_TOKEN},
                               transport=httpx2.MockTransport(response), restricted=True)
            app = mcp_hub.build_app({"fixture": hashlib.sha256(CLIENT.encode()).hexdigest()}, modules=(),
                                    ups=[("/vault-secret", backend)], environment="homelab")
            with TestClient(app) as client, patch.object(upstream, "READER_RESPONSE_LIMIT", 5):
                reply = client.post("/vault-secret", headers=auth(CLIENT), json={"reference": fixture.REF})
                self.assert_failure(reply)
        async def compressed(request):
            return httpx2.Response(200, content=gzip.compress(b"synthetic-upstream-secret"), headers={"content-encoding": "gzip"})

        backend = Upstream("vault-secret", "http://reader.example.test/secret", lambda: {"Authorization": "Bearer " + BROKER_TOKEN},
                           transport=httpx2.MockTransport(compressed), restricted=True)
        app = mcp_hub.build_app({"fixture": hashlib.sha256(CLIENT.encode()).hexdigest()}, modules=(),
                                ups=[("/vault-secret", backend)], environment="homelab")
        with TestClient(app) as client:
            reply = client.post("/vault-secret", headers=auth(CLIENT), json={"reference": fixture.REF})
            self.assert_failure(reply)
            self.assertIn("unsupported encoding", reply.text)

        async def unavailable(request):
            raise httpx2.ConnectError("synthetic-upstream-secret", request=request)

        backend = Upstream("vault-secret", "http://reader.example.test/secret", lambda: {"Authorization": "Bearer " + BROKER_TOKEN},
                           transport=httpx2.MockTransport(unavailable), restricted=True)
        app = mcp_hub.build_app({"fixture": hashlib.sha256(CLIENT.encode()).hexdigest()}, modules=(),
                                ups=[("/vault-secret", backend)], environment="homelab")
        with TestClient(app) as client:
            self.assert_failure(client.post("/vault-secret", headers=auth(CLIENT), json={"reference": fixture.REF}))

    def test_private_config_missing_invalid_duplicate_keys_and_file_modes(self):
        for value in ({}, {**self.private, "environment": "work"}, {**self.private, "extra": "bad"},
                      {**self.private, "reader": {**self.private["reader"], "credentials_dir": "relative"}},
                      {**self.private, "reader": {**self.private["reader"], "collection_ids": []}}):
            private_file(self.config_file, json.dumps(value))
            with self.assertRaisesRegex(ValueError, "private configuration"):
                adapter.configured_app(self.config_file)
        private_file(self.config_file, json.dumps(self.private))
        self.config_file.chmod(0o644)
        with self.assertRaises(ValueError):
            adapter.load_config(self.config_file)
        self.config_file.chmod(0o600)
        private_file(self.root / "broker-token", META_TOKEN)
        with self.assertRaises(ValueError):
            adapter.load_config(self.config_file)
        for token in (BROKER_TOKEN + "\n", WORK_CLIENT, "", "short"):
            private_file(self.root / "broker-token", token)
            with self.assertRaises(ValueError):
                adapter.load_config(self.config_file)
        private_file(self.root / "broker-token", BROKER_TOKEN)
        self.config_file.write_text('{"environment":"homelab","environment":"work"}')
        with self.assertRaises(ValueError):
            adapter.load_config(self.config_file)
        with self.assertRaises(ValueError):
            adapter.load_config(self.root / "missing.json")
        self.assertEqual(self.case.calls(), [])

    def test_homelab_opt_in_work_disabled_and_upstream_contract_validation(self):
        for value in ({"environment": "homelab", "services": ["vaultwarden"]},
                      {**self.services, "environment": "work"}, {**self.services, "vault_reader": "legacy"},
                      {"environment": "homelab", "services": ["drive"], "vault_reader": "restricted-v1"}):
            with self.assertRaises(ValueError):
                hub_services.validate_services(value)
        original = json.loads(json.dumps(self.credentials))
        for change in ({"protocol": "legacy"}, {"url": "http://reader.example.test/vault-secret"},
                       {"url": "http://other.example.test/secret"}, {"token": META_TOKEN}, {"token": "short"}):
            self.credentials = json.loads(json.dumps(original))
            self.credentials["upstreams"]["vault-secret"].update(change)
            self.save_hub()
            with self.assertRaises(ValueError):
                self.build_hub()
        self.services["environment"] = "work"
        self.save_hub()
        with self.assertRaises(ValueError):
            mcp_hub.instance_app("work", self.services_file, self.credentials_file)

    def test_client_generation_agrees_and_removes_stale_and_broker_entries(self):
        for register, key in ((hub_clients.pi, "mcpServers"), (hub_clients.claude, "mcpServers"),
                              (hub_clients.opencode, "mcp")):
            old = register({}, "https://legacy.example.test", "SYNTHETIC_TOKEN")
            old[key]["unrelated"] = {"url": "https://unrelated.example.test"}
            old[key]["vault-secret"] = {"url": "https://legacy.example.test/vault-secret"}
            old[key]["vault-attachment"] = {"url": "https://legacy.example.test/vault-attachment"}
            got = register(old, "https://homelab.example.test", "SYNTHETIC_TOKEN", services=self.services)
            self.assertEqual(set(got[key]), {"vaultwarden", "unrelated"})
            self.assertEqual(got[key]["vaultwarden"]["url"], "https://homelab.example.test/vaultwarden")
            work = register(got, "https://work.example.test", "SYNTHETIC_TOKEN",
                            services={"environment": "work", "services": ["drive"]})
            self.assertEqual(set(work[key]), {"drive", "unrelated"})
        home = self.root / "codex"
        home.mkdir()
        (home / "config.toml").write_text('[mcp_servers.drive]\nurl="https://legacy.example.test/drive"\n'
                                         '[mcp_servers.vault-secret]\nurl="https://legacy.example.test/vault-secret"\n'
                                         '[mcp_servers.vault-attachment]\nurl="https://legacy.example.test/vault-attachment"\n')
        calls = []
        hub_clients.codex(str(home), "https://homelab.example.test", "SYNTHETIC_TOKEN",
                          run=lambda cmd, **kwargs: calls.append(cmd), services=self.services)
        self.assertIn(["codex", "mcp", "remove", "drive"], calls)
        self.assertIn(["codex", "mcp", "remove", "vault-secret"], calls)
        self.assertIn(["codex", "mcp", "remove", "vault-attachment"], calls)
        self.assertEqual({cmd[3] for cmd in calls if cmd[2] == "add"}, {"vaultwarden"})

    def test_concurrent_http_requests_do_not_create_more_cli_workers(self):
        self.case.scenario["hang"] = "sync"
        self.case.save()
        with patch.object(core, "COMMAND_TIMEOUT", 1.5), ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(self.broker)
            end = time.monotonic() + 3
            while not any(c["args"][0] == "reader-sync" for c in self.case.calls()) and time.monotonic() < end:
                time.sleep(0.01)
            self.assertTrue(any(c["args"][0] == "reader-sync" for c in self.case.calls()))
            for _ in range(10):
                self.assert_failure(self.broker())
            self.assert_failure(pending.result())
        self.assertEqual(sum(c["args"][0] == "login" for c in self.case.calls()), 1)
        self.assertEqual(sum(c["args"][0] == "reader-sync" for c in self.case.calls()), 1)
        self.assertEqual(list(self.case.config.scratch_dir.iterdir()), [])

    def test_connector_deadline_does_not_release_worker_capacity(self):
        self.case.scenario["hang"] = "sync"
        self.case.save()
        with patch.object(adapter, "REQUEST_TIMEOUT", 0.5):
            self.assert_failure(self.broker())
        self.assertTrue(any(c["args"][0] == "reader-sync" for c in self.case.calls()))
        for _ in range(11):
            self.assert_failure(self.broker())
        self.assertEqual(sum(c["args"][0] == "login" for c in self.case.calls()), 1)
        self.assertEqual(sum(c["args"][0] == "reader-sync" for c in self.case.calls()), 1)
        self.case.reader.close()
        self.assertEqual(list(self.case.config.scratch_dir.iterdir()), [])

    def test_raw_cancelled_hub_request_keeps_worker_capacity(self):
        self.case.scenario["hang"] = "sync"
        self.case.save()

        async def run():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.build_hub()), base_url="http://fixture.example.test") as client:
                task = asyncio.create_task(client.post("/vault-secret", headers=auth(CLIENT), json={"reference": fixture.REF}))
                end = time.monotonic() + 3
                while not any(c["args"][0] == "reader-sync" for c in self.case.calls()) and time.monotonic() < end:
                    await asyncio.sleep(0.01)
                self.assertTrue(any(c["args"][0] == "reader-sync" for c in self.case.calls()))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                for _ in range(11):
                    self.assert_failure(await client.post("/vault-secret", headers=auth(CLIENT), json={"reference": fixture.REF}))
                await anyio.to_thread.run_sync(self.case.reader.close)

        asyncio.run(run())
        self.assertEqual(sum(c["args"][0] == "reader-sync" for c in self.case.calls()), 1)
        self.assertEqual(list(self.case.config.scratch_dir.iterdir()), [])

    def test_cancelled_operation_keeps_capacity_until_child_ends(self):
        self.case.scenario["hang"] = "sync"
        self.case.save()
        operations = adapter.ReaderOperations(self.case.reader)
        self.addCleanup(operations.close)

        async def run():
            scope_holder = []

            async def cancelled():
                with anyio.CancelScope() as scope:
                    scope_holder.append(scope)
                    with patch.object(core, "COMMAND_TIMEOUT", 0.5):
                        with self.assertRaises(RuntimeError):
                            await operations.run(self.case.reader.secret, fixture.REF)

            async with anyio.create_task_group() as group:
                group.start_soon(cancelled)
                while not any(c["args"][0] == "reader-sync" for c in self.case.calls()):
                    await anyio.sleep(0.01)
                scope_holder[0].cancel()
                for _ in range(5):
                    with self.assertRaises(RuntimeError):
                        await operations.run(self.case.reader.secret, fixture.REF)
            await anyio.to_thread.run_sync(operations.close)

        anyio.run(run)
        self.assertEqual(sum(c["args"][0] == "reader-sync" for c in self.case.calls()), 1)
        self.assertEqual(list(self.case.config.scratch_dir.iterdir()), [])

    def test_raw_asyncio_cancellation_does_not_release_worker_early(self):
        self.case.scenario["hang"] = "sync"
        self.case.save()
        operations = adapter.ReaderOperations(self.case.reader)
        self.addCleanup(operations.close)

        async def run():
            with patch.object(core, "COMMAND_TIMEOUT", 1):
                task = asyncio.create_task(operations.run(self.case.reader.secret, fixture.REF))
                end = time.monotonic() + 3
                while not any(c["args"][0] == "reader-sync" for c in self.case.calls()) and time.monotonic() < end:
                    await asyncio.sleep(0.01)
                self.assertTrue(any(c["args"][0] == "reader-sync" for c in self.case.calls()))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                for _ in range(11):
                    with self.assertRaises(RuntimeError):
                        await operations.run(self.case.reader.secret, fixture.REF)
                await anyio.to_thread.run_sync(operations.close)

        asyncio.run(run())
        self.assertEqual(sum(c["args"][0] == "reader-sync" for c in self.case.calls()), 1)
        self.assertEqual(sum(c["args"][0] == "login" for c in self.case.calls()), 1)
        self.assertEqual(list(self.case.config.scratch_dir.iterdir()), [])

    def test_entrypoints_disable_access_logs_only_for_restricted_reader(self):
        with patch.object(mcp_hub, "configured_app", return_value=self.build_hub()), patch("uvicorn.run") as run:
            mcp_hub.main()
            self.assertFalse(run.call_args.kwargs["access_log"])
        legacy = mcp_hub.build_app({"fixture": hashlib.sha256(b"legacy.synthetic-client").hexdigest()}, modules=(), ups=[])
        with patch.object(mcp_hub, "configured_app", return_value=legacy), patch("uvicorn.run") as run:
            mcp_hub.main()
            self.assertTrue(run.call_args.kwargs["access_log"])
        with patch.dict(os.environ, {"VAULT_READER_CONFIG": str(self.config_file)}, clear=True), patch("uvicorn.run") as run:
            adapter.main()
            self.assertFalse(run.call_args.kwargs["access_log"])

    def test_sensitive_cli_errors_do_not_enter_logs(self):
        disabled = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs(level=logging.DEBUG) as captured:
                self.case.scenario["fail"] = "sync"
                self.case.save()
                self.assert_failure(self.broker())
                self.assert_failure(self.mcp("vault_get", {"reference": fixture.REF}))
        finally:
            logging.disable(disabled)
        text = "\n".join(captured.output)
        for forbidden in ("synthetic-upstream-secret", "synthetic-password-secret", "synthetic-field-secret",
                          "synthetic-master-password", BROKER_TOKEN, META_TOKEN):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
