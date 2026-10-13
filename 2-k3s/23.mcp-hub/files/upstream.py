#!/usr/bin/env python3
"""Forward one hub path to an upstream MCP server (see ../README.md).

The client's hub token never leaves the hub: the forwarded request carries
only the MCP headers plus the upstream credential from `credential()`.
Responses stream back unchanged, so SSE replies work too. An upstream that
answers 401/403, is unreachable, or whose credential cannot be produced comes
back as a JSON-RPC error with the request id, so the message reaches the
model or `mcp list` instead of a bare 502 (and a 401 from upstream never
reaches the client, which would otherwise start its own OAuth dance).
"""

import json
import threading

from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

FORWARD = ("accept", "content-type", "mcp-session-id", "mcp-protocol-version", "last-event-id")
RETURN = ("content-type", "mcp-session-id", "cache-control")
READER_BODY_LIMIT = 16384
READER_RESPONSE_LIMIT = 12 * 1024 * 1024
READER_BODY_TIMEOUT = 10
READER_REQUEST_TIMEOUT = 125


class UpstreamError(Exception):
    """Expected upstream failure; the text is shown to the client."""


class Upstream:
    def __init__(self, name, url, credential=lambda: {}, on_unauthorized=None, transport=None, restricted=False):
        """credential() -> extra headers (may block; runs in a thread). on_unauthorized() is called once
        after an upstream 401 before a single retry, e.g. to force a token refresh."""
        self.name, self.url, self.credential, self.on_unauthorized = name, url, credential, on_unauthorized
        self.transport = transport
        self.restricted = restricted
        self.requests = threading.BoundedSemaphore(8)
        self._client = None

    def client(self):
        import httpx2

        if self._client is None:
            self._client = httpx2.AsyncClient(transport=self.transport, timeout=httpx2.Timeout(120, connect=10),
                                              headers={"user-agent": "epaflix-mcp-hub/1"})
        return self._client

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()


def _error(body, message, status=502):
    try:
        req = json.loads(body) if body else None
    except ValueError:
        req = None
    if isinstance(req, dict) and "id" in req:
        return JSONResponse({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32000, "message": message}})
    return Response(message, status_code=status, media_type="text/plain")


def restricted_route(path, upstream):
    import anyio

    def failed(body, message, status=502):
        response = _error(body, message, status)
        response.headers["cache-control"] = "no-store"
        return response

    async def forward(request):
        if request.url.query:
            return Response("query parameters are not supported", status_code=400)
        if not upstream.requests.acquire(blocking=False):
            return Response("restricted reader busy", status_code=503)
        body = bytearray()
        try:
            with anyio.fail_after(READER_BODY_TIMEOUT):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > READER_BODY_LIMIT:
                        return Response("request body too large", status_code=413)
                    body.extend(chunk)
            with anyio.fail_after(READER_REQUEST_TIMEOUT):
                extra = await anyio.to_thread.run_sync(upstream.credential)
                headers = {k: v for k, v in request.headers.items() if k in FORWARD} | {"accept-encoding": "identity", **extra}
                async with upstream.client().stream(request.method, upstream.url, headers=headers, content=bytes(body)) as resp:
                    if resp.status_code in (401, 403) or 300 <= resp.status_code < 400:
                        return failed(body, "restricted reader rejected connector authentication")
                    if resp.status_code >= 400:
                        return failed(body, "restricted reader unavailable or reference rejected", status=resp.status_code)
                    if resp.headers.get("content-encoding", "identity").lower() != "identity":
                        return failed(body, "restricted reader returned unsupported encoding")
                    result = bytearray()
                    async for chunk in resp.aiter_raw(chunk_size=65536):
                        if len(result) + len(chunk) > READER_RESPONSE_LIMIT:
                            return failed(body, "restricted reader response limit exceeded")
                        result.extend(chunk)
                    out = {k: v for k, v in resp.headers.items() if k in RETURN}
                    out["cache-control"] = "no-store"
                    return Response(bytes(result), status_code=resp.status_code, headers=out)
        except Exception:
            return failed(body, "restricted reader unavailable")
        finally:
            upstream.requests.release()

    methods = ["GET", "POST", "DELETE"] if path == "/vaultwarden" else ["POST"]
    return Route(path, forward, methods=methods)


def route(path, upstream):
    import anyio
    import httpx2

    if upstream.restricted:
        return restricted_route(path, upstream)

    async def forward(request):
        body = await request.body()
        # identity: the raw bytes are streamed back without a Content-Encoding header.
        headers = {k: v for k, v in request.headers.items() if k in FORWARD} | {"accept-encoding": "identity"}
        for attempt in (0, 1):
            try:
                extra = await anyio.to_thread.run_sync(upstream.credential)
            except UpstreamError as e:
                return _error(body, f"{upstream.name}: {e}")
            except Exception as e:  # never a bare 500: the client would show no reason at all
                return _error(body, f"{upstream.name}: credential unavailable: {type(e).__name__}: {e}")
            req = upstream.client().build_request(request.method, upstream.url, headers={**headers, **extra},
                                                  content=body or None)
            try:
                resp = await upstream.client().send(req, stream=True)
            except httpx2.HTTPError as e:
                return _error(body, f"{upstream.name} unreachable: {type(e).__name__}: {e}")
            if resp.status_code in (401, 403):
                detail = (await resp.aread()).decode(errors="replace")[:300]
                await resp.aclose()
                if attempt == 0 and upstream.on_unauthorized:
                    await anyio.to_thread.run_sync(upstream.on_unauthorized)
                    continue
                return _error(body, f"{upstream.name} rejected the hub's credential (HTTP {resp.status_code}): {detail}")
            break

        async def stream():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()

        out = {k: v for k, v in resp.headers.items() if k in RETURN}
        return StreamingResponse(stream(), status_code=resp.status_code, headers=out)

    return Route(path, forward, methods=["GET", "POST", "DELETE"])
