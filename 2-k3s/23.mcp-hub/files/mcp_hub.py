#!/usr/bin/env python3
"""The MCP hub: one long-lived streamable-HTTP gateway, one path per MCP server
(vocabulary in ../README.md). A path is either a module served in-process
(MODULES) or an upstream the hub forwards to with a credential only it holds
(upstreams()). ../README.md lists the other steps for adding one.

Every client has its own hub token; clients.json maps client name to the
SHA-256 of its token (../tools/add-client.py writes it), and any listed token
opens every path. GET /healthz is the only unauthenticated route. Modules
use stateless JSON transport, so a pod restart costs a client one retry.

Env: CLIENTS_FILE (default /app/clients.json) plus whatever the mounted
servers need: GMAIL_*, SEARXNG_URL, KEEPASS_URL + KEEPASS_HUB_SECRET,
KUBERNETES_MCP_URL, NOTION_MCP_URL.

Self-test: --selftest runs each module's selftest, then boots the ASGI app
in-process against fake upstreams and checks auth, the MCP handshake and
the forwarding rules.
"""

import contextlib
import hashlib
import hmac
import json
import os
import sys

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route

import gmail_mcp
import searxng_mcp
import upstream
from upstream import Upstream, UpstreamError

MODULES = [("/gmail", gmail_mcp.server), ("/searxng", searxng_mcp.server)]
PORT = int(os.environ.get("PORT", "8000"))


def upstreams():
    import notion_grant

    def keepass_secret():
        secret = os.environ.get("KEEPASS_HUB_SECRET", "")
        if not secret:
            raise UpstreamError("KEEPASS_HUB_SECRET is not set (Secret mcp-hub-keepass)")
        return {"x-hub-secret": secret}

    grant = notion_grant.Grant(notion_grant.SecretStore("mcp-hub", "mcp-hub-notion-grant"))
    return [
        ("/keepass", Upstream("keepass", os.environ.get(
            "KEEPASS_URL", "http://keepass.syncthing.svc.cluster.local:8000/keepass"), keepass_secret)),
        ("/kubernetes", Upstream("kubernetes", os.environ.get(
            "KUBERNETES_MCP_URL", "http://kubernetes-mcp.mcp-hub.svc.cluster.local:8080/mcp"))),
        ("/notion", Upstream("notion", os.environ.get("NOTION_MCP_URL", "https://mcp.notion.com/mcp"),
                             grant.headers, on_unauthorized=grant.invalidate)),
    ]


def load_clients(path):
    """{client name: sha256 hex of its hub token}."""
    with open(path) as f:
        clients = json.load(f)
    for name, digest in clients.items():
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise SystemExit(f"{path}: client {name!r} needs a lowercase sha256 hex digest")
    return clients


class BearerAuth:
    def __init__(self, app, clients):
        self.app, self.digests = app, [d.encode() for d in clients.values()]

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/healthz":
            header = Headers(scope=scope).get("authorization", "")
            # Hash first: equal-length compare, and non-ASCII input cannot raise (500) instead of 401.
            got = hashlib.sha256(header[7:].encode(errors="replace")).hexdigest().encode()
            # A list, not a generator: compare against every client, no early exit.
            ok = header.startswith("Bearer ") and any([hmac.compare_digest(got, d) for d in self.digests])
            if not ok:
                await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app(clients, modules=MODULES, ups=None):
    if not clients:
        raise SystemExit("no clients: CLIENTS_FILE lists none (run 2-k3s/23.mcp-hub/tools/add-client.py)")
    ups = upstreams() if ups is None else ups
    mounted = [(path, make()) for path, make in modules]
    routes = [Route("/healthz", lambda request: PlainTextResponse("ok"))]
    for path, server in mounted:
        # host=0.0.0.0 disables the SDK's localhost-only Host check; Traefik
        # and the in-cluster Service both reach us with other Host headers.
        routes += server.streamable_http_app(
            streamable_http_path=path, host="0.0.0.0", stateless_http=True, json_response=True
        ).routes
    routes += [upstream.route(path, up) for path, up in ups]

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with contextlib.AsyncExitStack() as stack:
            for _, server in mounted:
                await stack.enter_async_context(server.session_manager.run())
            for _, up in ups:
                stack.push_async_callback(up.aclose)
            yield

    return Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(BearerAuth, clients=clients)])


def _selftest():
    import httpx2
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response, StreamingResponse
    from starlette.testclient import TestClient

    import notion_grant

    gmail_mcp._selftest()
    notion_grant._selftest()
    for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"):
        os.environ.setdefault(k, "selftest")
    clients = {"laptop": hashlib.sha256(b"t0k3n").hexdigest(), "t3code": hashlib.sha256(b"p0d").hexdigest()}
    # Format only: deleting a client's line (revocation) must not fail the pod's startup selftest.
    load_clients(os.path.join(os.path.dirname(os.path.abspath(__file__)), "clients.json"))

    # Fake upstream MCP server: echoes what it received, or misbehaves on demand.
    seen = []

    async def fake(request: Request):
        seen.append(dict(request.headers))
        body = await request.body()
        req = json.loads(body) if body else {}
        if request.headers.get("authorization") == "Bearer stale":
            return Response("expired", status_code=401, headers={"www-authenticate": "Bearer resource_metadata=x"})
        if req.get("method") == "stream":
            async def events():
                yield b"event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":7,\"result\":{}}\n\n"
            return StreamingResponse(events(), media_type="text/event-stream", headers={"mcp-session-id": "s1"})
        return JSONResponse({"jsonrpc": "2.0", "id": req.get("id"), "result": {"method": req.get("method")}},
                            headers={"mcp-session-id": "s1", "x-internal": "no"})

    fake_app = Starlette(routes=[Route("/mcp", fake, methods=["GET", "POST", "DELETE"])])
    transport = httpx2.ASGITransport(app=fake_app)
    tokens = ["stale"]

    def refresh():
        tokens[0] = "fresh"

    def broken():
        raise UpstreamError("no grant yet")

    def crash():
        raise OSError("api server timeout")

    ups = [("/plain", Upstream("plain", "http://up/mcp", lambda: {"x-hub-secret": "s3"}, transport=transport)),
           ("/oauth", Upstream("oauth", "http://up/mcp", lambda: {"authorization": f"Bearer {tokens[0]}"},
                               on_unauthorized=refresh, transport=transport)),
           ("/broken", Upstream("broken", "http://up/mcp", broken, transport=transport)),
           ("/crash", Upstream("crash", "http://up/mcp", crash, transport=transport)),
           ("/down", Upstream("down", "http://127.0.0.1:9/mcp"))]

    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "selftest", "version": "0"}}}
    accept = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    auth = {**accept, "Authorization": "Bearer t0k3n"}
    with TestClient(build_app(clients, ups=ups)) as c:
        assert c.get("/healthz").status_code == 200
        assert c.post("/gmail", json=init, headers=accept).status_code == 401
        assert c.post("/gmail", json=init, headers={**accept, "Authorization": "Bearer wrong"}).status_code == 401
        assert c.post("/gmail", json=init, headers={**accept, "Authorization": "t0k3n"}).status_code == 401
        assert c.post("/gmail", json=init, headers={**accept, "Authorization": "Bearer t\u00f6k".encode("latin-1")}).status_code == 401
        assert c.post("/plain", json=init, headers={**accept, "Authorization": "Bearer nope"}).status_code == 401
        assert not seen, "an unauthenticated request must not reach an upstream"
        for token in ("t0k3n", "p0d"):
            r = c.post("/gmail", json=init, headers={**accept, "Authorization": f"Bearer {token}"})
            assert r.status_code == 200, (r.status_code, r.text[:300])
            assert r.json()["result"]["serverInfo"]["name"] == "gmail", r.text[:300]

        def tools(path):
            r = c.post(path, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=auth)
            return sorted(t["name"] for t in r.json()["result"]["tools"])

        assert tools("/gmail") == ["gmail_attachment", "gmail_draft", "gmail_get", "gmail_labels", "gmail_modify",
                                   "gmail_search", "gmail_send", "gmail_send_draft", "gmail_thread",
                                   "gmail_trash"], tools("/gmail")
        assert tools("/searxng") == ["searxng_search"], tools("/searxng")
        r = c.post("/searxng", json=init, headers=auth)
        assert "searxng_search for all web searches" in r.json()["result"]["instructions"], r.text[:300]

        def call(path, name, arguments):
            r = c.post(path, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                   "params": {"name": name, "arguments": arguments}}, headers=auth)
            return r.json()["result"]

        # Expected failures must reach the model as is_error text, not the SDK's generic "Error executing tool".
        result = call("/gmail", "gmail_modify", {"message_ids": []})
        assert result["isError"] and "message_ids must not be empty" in result["content"][0]["text"], result
        result = call("/searxng", "searxng_search", {"query": " "})
        assert result["isError"] and "searxng error: query must not be empty" in result["content"][0]["text"], result

        # Forwarding: the hub token stays home, the upstream credential and MCP headers go out.
        r = c.post("/plain", json=init, headers={**auth, "Mcp-Session-Id": "s1", "Mcp-Protocol-Version": "2025-06-18",
                                                  "Cookie": "c=1"})
        assert r.status_code == 200 and r.json()["result"] == {"method": "initialize"}, r.text[:300]
        assert r.headers["mcp-session-id"] == "s1" and "x-internal" not in r.headers, r.headers
        h = seen[-1]
        assert h["x-hub-secret"] == "s3" and "authorization" not in h and "cookie" not in h, h
        assert h["mcp-session-id"] == "s1" and h["mcp-protocol-version"] == "2025-06-18", h
        r = c.post("/plain", json={"jsonrpc": "2.0", "id": 7, "method": "stream"}, headers=auth)
        assert r.headers["content-type"].startswith("text/event-stream") and '"id":7' in r.text, (r.headers, r.text)
        assert c.delete("/plain", headers=auth).status_code == 200

        # Upstream 401: refresh once and retry; the client never sees the upstream's 401.
        r = c.post("/oauth", json=init, headers=auth)
        assert r.status_code == 200 and r.json()["result"] == {"method": "initialize"}, r.text[:300]
        assert seen[-1]["authorization"] == "Bearer fresh" and seen[-2]["authorization"] == "Bearer stale"
        tokens[0] = "stale"
        ups[1][1].on_unauthorized = None
        r = c.post("/oauth", json=init, headers=auth)
        assert r.status_code == 200 and "rejected the hub's credential (HTTP 401)" in r.json()["error"]["message"], r.text
        assert "www-authenticate" not in r.headers
        r = c.post("/broken", json=init, headers=auth)
        assert r.json() == {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "broken: no grant yet"}}, r.text
        r = c.post("/crash", json=init, headers=auth)
        assert "credential unavailable: OSError: api server timeout" in r.json()["error"]["message"], r.text
        r = c.post("/down", json=init, headers=auth)
        assert r.json()["error"]["message"].startswith("down unreachable"), r.text
        assert c.get("/down", headers=auth).status_code == 502
        assert c.post("/nope", json=init, headers=auth).status_code == 404
    print("hub selftest OK")


def main():
    if "--selftest" in sys.argv:
        _selftest()
        return
    import uvicorn

    clients = load_clients(os.environ.get("CLIENTS_FILE", "/app/clients.json"))
    uvicorn.run(build_app(clients), host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
