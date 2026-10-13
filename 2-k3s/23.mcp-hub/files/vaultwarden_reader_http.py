import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import threading

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from vaultwarden_reader import ReaderConfig, RestrictedVaultReader, private_bytes

BODY_LIMIT = 16384
RESPONSE_LIMIT = 12 * 1024 * 1024
BODY_TIMEOUT = 10
REQUEST_TIMEOUT = 125
METADATA_PATHS = {"/vaultwarden", "/metadata/list", "/metadata/get"}
BROKER_PATHS = {"/secret", "/attachment"}


def token_value(value):
    if not isinstance(value, str) or not 32 <= len(value) <= 4096 or any(
            ord(c) <= 32 or ord(c) >= 127 for c in value) or value.startswith(("homelab.", "work.")):
        raise ValueError("connector tokens require independent visible ASCII values of at least 32 characters")
    return value


def strict_json(data):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(data, object_pairs_hook=pairs)


def load_config(path):
    try:
        value = strict_json(private_bytes(Path(path), 65536))
        if not isinstance(value, dict) or set(value) != {
                "environment", "reader", "metadata_token_file", "broker_token_file"} or value["environment"] != "homelab":
            raise ValueError
        spec = value["reader"]
        if not isinstance(spec, dict) or set(spec) != {
                "server_url", "user_id", "organization_id", "collection_ids", "credentials_dir", "scratch_dir", "executable"}:
            raise ValueError
        if not isinstance(spec["collection_ids"], list):
            raise ValueError
        config = ReaderConfig(spec["server_url"], spec["user_id"], spec["organization_id"],
                              tuple(spec["collection_ids"]), Path(spec["credentials_dir"]),
                              Path(spec["scratch_dir"]), Path(spec["executable"]))
        tokens = [token_value(private_bytes(Path(value[key]), 4096).decode("utf-8"))
                  for key in ("metadata_token_file", "broker_token_file")]
        if hmac.compare_digest(*tokens):
            raise ValueError
        return config, tokens[0], tokens[1]
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError("restricted connector private configuration unavailable or invalid") from None


class ConnectorGuard:
    def __init__(self, app, metadata_token, broker_token):
        self.app = app
        self.metadata = hashlib.sha256(token_value(metadata_token).encode()).digest()
        self.broker = hashlib.sha256(token_value(broker_token).encode()).digest()
        if hmac.compare_digest(self.metadata, self.broker):
            raise ValueError("metadata and broker credentials must be separate")
        self.requests = threading.BoundedSemaphore(8)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if path == "/healthz" and scope["method"] == "GET" and not scope.get("query_string"):
            await PlainTextResponse("ok")(scope, receive, send)
            return
        headers = Headers(scope=scope)
        authorization = headers.getlist("authorization")
        target = self.metadata if path in METADATA_PATHS else self.broker if path in BROKER_PATHS else None
        header = authorization[0] if len(authorization) == 1 else ""
        got = hashlib.sha256(header[7:].encode(errors="replace")).digest()
        if target is None or not header.startswith("Bearer ") or not hmac.compare_digest(got, target):
            await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
            return
        if scope["method"] != "POST":
            await PlainTextResponse("method not allowed", status_code=405,
                                    headers={"allow": "POST"})(scope, receive, send)
            return
        if scope.get("query_string"):
            await PlainTextResponse("query parameters are not supported", status_code=400)(scope, receive, send)
            return
        if not self.requests.acquire(blocking=False):
            await PlainTextResponse("restricted connector busy", status_code=503)(scope, receive, send)
            return
        try:
            body = bytearray()
            try:
                with anyio.fail_after(BODY_TIMEOUT):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        if len(body) + len(chunk) > BODY_LIMIT:
                            await PlainTextResponse("request body too large", status_code=413)(scope, receive, send)
                            return
                        body.extend(chunk)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                await PlainTextResponse("request body timed out", status_code=408)(scope, receive, send)
                return
            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            messages = []
            total = 0

            async def bounded_send(message):
                nonlocal total
                total += len(message.get("body", b""))
                if total > RESPONSE_LIMIT:
                    raise ValueError("response limit")
                messages.append(message)

            try:
                with anyio.fail_after(REQUEST_TIMEOUT):
                    await self.app(scope, replay, bounded_send)
            except Exception:
                await PlainTextResponse("restricted connector unavailable", status_code=502,
                                        headers={"cache-control": "no-store"})(scope, receive, send)
                return
            for message in messages:
                if message["type"] == "http.response.start":
                    message["headers"] = [(k, v) for k, v in message["headers"] if k.lower() != b"cache-control"]
                    message["headers"].append((b"cache-control", b"no-store"))
                await send(message)
        finally:
            self.requests.release()


class ReaderOperations:
    def __init__(self, reader):
        self.reader = reader
        self.active = threading.BoundedSemaphore(1)
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vault-reader")

    def close(self):
        self.reader.close()
        self.worker.shutdown(wait=True, cancel_futures=True)

    async def run(self, operation, *args):
        if not self.active.acquire(blocking=False):
            raise RuntimeError("restricted connector busy")
        try:
            future = self.worker.submit(operation, *args)
        except RuntimeError:
            self.active.release()
            raise RuntimeError("restricted connector closed") from None
        future.add_done_callback(lambda _: self.active.release())
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        return await asyncio.shield(wrapped)


def server(operations):
    mcp = MCPServer("homelab-vault-reader", instructions=
                    "Approved homelab collection metadata only. Use collection_id with an item_id or exact title. "
                    "Secrets and attachment contents are available only to the authenticated command-time broker.")

    async def result(operation, *args):
        try:
            value = await operations.run(operation, *args)
            text = json.dumps(value, ensure_ascii=True)
            if len(text.encode()) > RESPONSE_LIMIT // 2:
                raise RuntimeError
            return CallToolResult(content=[TextContent(type="text", text=text)])
        except ValueError:
            return CallToolResult(isError=True, content=[TextContent(type="text", text="invalid or unresolved collection reference")])
        except Exception:
            return CallToolResult(isError=True, content=[TextContent(type="text", text="restricted reader unavailable")])

    @mcp.tool()
    async def vault_list(collection_id: str | None = None) -> CallToolResult:
        """List approved item metadata, optionally within one approved collection UUID."""
        return await result(operations.reader.list_items, collection_id)

    @mcp.tool()
    async def vault_get(reference: dict) -> CallToolResult:
        """Get metadata using collection_id and exactly one item_id or exact title."""
        return await result(operations.reader.get, reference)

    return mcp


def http_app(reader, metadata_token, broker_token):
    if not isinstance(reader, RestrictedVaultReader):
        raise ValueError("restricted core is required")
    token_value(metadata_token)
    token_value(broker_token)
    if hmac.compare_digest(metadata_token, broker_token):
        raise ValueError("metadata and broker credentials must be separate")
    operations = ReaderOperations(reader)
    mcp = server(operations)

    async def read(request):
        try:
            value = strict_json(await request.body())
            if not isinstance(value, dict):
                raise ValueError
            path = request.url.path
            if path == "/metadata/list" and set(value) <= {"collection_id"}:
                result = await operations.run(reader.list_items, value.get("collection_id"))
            elif path in ("/metadata/get", "/secret") and set(value) == {"reference"}:
                operation = reader.get if path == "/metadata/get" else reader.secret
                result = await operations.run(operation, value["reference"])
            elif path == "/attachment" and set(value) == {"reference", "attachment"}:
                result = await operations.run(reader.attachment, value["reference"], value["attachment"])
                result = {"filename": result["filename"], "content_b64": base64.b64encode(result["content"]).decode("ascii")}
            else:
                raise ValueError
            return JSONResponse(result)
        except ValueError:
            return PlainTextResponse("invalid or unresolved collection reference", status_code=400)
        except Exception:
            return PlainTextResponse("restricted reader unavailable", status_code=503)

    routes = [Route(path, read, methods=["POST"]) for path in sorted((METADATA_PATHS - {"/vaultwarden"}) | BROKER_PATHS)]
    routes += mcp.streamable_http_app(streamable_http_path="/vaultwarden", host="0.0.0.0",
                                    stateless_http=True, json_response=True).routes

    @contextlib.asynccontextmanager
    async def lifespan(app):
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await anyio.to_thread.run_sync(operations.close)

    return Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(
        ConnectorGuard, metadata_token=metadata_token, broker_token=broker_token)])


def configured_app(path):
    config, metadata, broker = load_config(path)
    return http_app(RestrictedVaultReader(config), metadata, broker)


def main():
    import uvicorn

    path = os.environ.get("VAULT_READER_CONFIG")
    if not path:
        raise ValueError("VAULT_READER_CONFIG is required")
    uvicorn.run(configured_app(path), host="0.0.0.0", port=8000, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
