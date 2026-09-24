#!/usr/bin/env python3
"""Long-lived streamable-HTTP MCP hub: one process, one bearer token, one path
per MCP server. Today: /gmail (gmail_mcp.py). Each entry in SERVERS mounts
its own MCPServer behind the shared auth wrapper; ../README.md lists the
other steps (ConfigMap, env, clients) for adding one.

Env: MCP_HUB_TOKEN (Secret mcp-hub-token) plus whatever the mounted servers
need. GET /healthz is the only unauthenticated route. Transport is stateless
JSON (no SSE), so a pod restart or Traefik in front costs nothing but a retry.

Self-test: --selftest runs each server's selftest, then boots the ASGI app
with a fake token and checks the auth wrapper and the MCP handshake in-process.
"""

import contextlib
import hmac
import os
import sys

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route

import gmail_mcp

SERVERS = [("/gmail", gmail_mcp.server)]
PORT = int(os.environ.get("PORT", "8000"))


class BearerAuth:
    def __init__(self, app, token):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/healthz":
            header = Headers(scope=scope).get("authorization", "")
            # bytes: compare_digest raises on non-ASCII str, which would 500 instead of 401.
            ok = header.startswith("Bearer ") and hmac.compare_digest(header[7:].encode(), self.token.encode())
            if not ok:
                await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app(token, servers=SERVERS):
    if not token:
        raise SystemExit("MCP_HUB_TOKEN must be set (Secret mcp-hub-token)")
    mounted = [(path, make()) for path, make in servers]
    routes = [Route("/healthz", lambda request: PlainTextResponse("ok"))]
    for path, server in mounted:
        # host=0.0.0.0 disables the SDK's localhost-only Host check; Traefik
        # and the in-cluster Service both reach us with other Host headers.
        routes += server.streamable_http_app(
            streamable_http_path=path, host="0.0.0.0", stateless_http=True, json_response=True
        ).routes

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with contextlib.AsyncExitStack() as stack:
            for _, server in mounted:
                await stack.enter_async_context(server.session_manager.run())
            yield

    return Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(BearerAuth, token=token)])


def _selftest():
    import json
    from starlette.testclient import TestClient

    gmail_mcp._selftest()
    for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"):
        os.environ.setdefault(k, "selftest")
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "selftest", "version": "0"}}}
    accept = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with TestClient(build_app("t0k3n")) as c:
        assert c.get("/healthz").status_code == 200
        assert c.post("/gmail", json=init, headers=accept).status_code == 401
        assert c.post("/gmail", json=init, headers={**accept, "Authorization": "Bearer wrong"}).status_code == 401
        assert c.post("/gmail", json=init, headers={**accept, "Authorization": "Bearer t\u00f6k".encode("latin-1")}).status_code == 401
        r = c.post("/gmail", json=init, headers={**accept, "Authorization": "Bearer t0k3n"})
        assert r.status_code == 200, (r.status_code, r.text[:300])
        assert r.json()["result"]["serverInfo"]["name"] == "gmail", r.text[:300]
        r = c.post("/gmail", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                   headers={**accept, "Authorization": "Bearer t0k3n"})
        tools = sorted(t["name"] for t in r.json()["result"]["tools"])
        assert tools == ["gmail_attachment", "gmail_draft", "gmail_get", "gmail_labels", "gmail_modify",
                         "gmail_search", "gmail_send", "gmail_send_draft", "gmail_thread", "gmail_trash"], tools
        # Expected failures must reach the model as is_error text, not the SDK's generic "Error executing tool".
        r = c.post("/gmail", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                   "params": {"name": "gmail_modify", "arguments": {"message_ids": []}}},
                   headers={**accept, "Authorization": "Bearer t0k3n"})
        result = r.json()["result"]
        assert result["isError"] and "message_ids must not be empty" in result["content"][0]["text"], result
        assert c.post("/nope", json=init, headers={**accept, "Authorization": "Bearer t0k3n"}).status_code == 404
    print("hub selftest OK", json.dumps(tools))


def main():
    if "--selftest" in sys.argv:
        _selftest()
        return
    import uvicorn

    uvicorn.run(build_app(os.environ.get("MCP_HUB_TOKEN", "")), host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
