#!/usr/bin/env python3
"""Read-write MCP server over the Syncthing-synced KeePass KDBX.

Env: KEEPASS_DB (path to the KDBX), KEEPASS_PASSPHRASE, and for --http
KEEPASS_HUB_SECRET. All come from the syncthing/keepass Deployment
(../keepass.yaml). --http serves streamable HTTP at /keepass on PORT (8000)
for the MCP hub (2-k3s/23.mcp-hub), which sends the shared secret as
X-Hub-Secret; without --http it speaks stdio. Self-test: --selftest creates a
throwaway vault, exercises every tool and the HTTP wrapper, and never touches
KEEPASS_DB.

Writes hold an flock on "<DB>.lock" and land via atomic replace, so concurrent
MCP sessions in this pod cannot interleave a load-modify-save. Writes from
other hosts still race through Syncthing; avoid editing the vault on two
devices at the same time.
"""

import base64
import contextlib
import fcntl
import json
import os
import sys
import tempfile

DB = os.environ.get("KEEPASS_DB", "")
PASSPHRASE = os.environ.get("KEEPASS_PASSPHRASE", "")

FIELDS = ("username", "url", "notes")


def _entry_path(entry):
    parts = entry.path or []
    return "/" + "/".join(p if isinstance(p, str) and p else "" for p in parts)


def _entry_summary(entry):
    out = {"path": _entry_path(entry), "title": entry.title, "expired": entry.expired}
    for field in FIELDS:
        out[field] = getattr(entry, field)
    out["attachments"] = sorted(a.filename for a in entry.attachments)
    return out


def _open():
    from pykeepass import PyKeePass

    return PyKeePass(DB, password=PASSPHRASE)


@contextlib.contextmanager
def _writing():
    """Load the vault under an exclusive lock, mutate, save atomically.

    Refuses to save if the file changed on disk while we held it (e.g. a
    synced edit from another device landed mid-write); retry instead.
    """
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(DB) or ".", prefix=".keepass-", suffix=".tmp")
    os.close(fd)
    lock = DB + ".lock"
    with open(lock, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            kp = _open()
            before = os.stat(DB)
            yield kp
            after = os.stat(DB)
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise RuntimeError(
                    "vault file changed on disk during this write; nothing was saved, retry"
                )
            kp.save(tmp)
            os.chmod(tmp, before.st_mode & 0o777)
            meta = os.stat(tmp)
            if (before.st_uid, before.st_gid) != (meta.st_uid, meta.st_gid):
                os.chown(tmp, before.st_uid, before.st_gid)
            os.replace(tmp, DB)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
            if os.path.exists(tmp):
                os.unlink(tmp)


def _all_entries(kp):
    return [e for e in kp.entries if "Recycle Bin" not in e.path]


def _find(kp, path):
    needle = "/" + path.strip("/")
    matches = [e for e in _all_entries(kp) if _entry_path(e) == needle]
    if not matches:
        raise ValueError(f"no entry at path '{path}' (list entries to see valid paths)")
    return matches[0]


def _child_group(group, name):
    return next((g for g in group.subgroups if g.name == name), None)


def _resolve_group(kp, group_path):
    """Walk to the group at group_path, creating missing groups along the way."""
    group = kp.root_group
    for name in [p for p in group_path.strip("/").split("/") if p]:
        child = _child_group(group, name)
        if child is None:
            group = kp.add_group(group, name)
        else:
            group = child
    return group


def _split_path(path):
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        raise ValueError("path must name an entry, e.g. /Group/Title")
    if "Recycle Bin" in parts:
        raise ValueError("entries inside the Recycle Bin are managed by vault_trash and KeePassXC")
    return "/".join(parts[:-1]), parts[-1]


def _list_tool(prefix: str = ""):
    kp = _open()
    return [
        _entry_summary(e)
        for e in _all_entries(kp)
        if _entry_path(e).lstrip("/").startswith(prefix.strip("/"))
    ]


def _get_tool(path: str, include_password: bool = True):
    kp = _open()
    entry = _find(kp, path)
    out = _entry_summary(entry)
    out["custom_properties"] = dict(entry.custom_properties)
    if include_password and not entry.expired:
        out["password"] = entry.password
    elif entry.expired:
        out["password"] = None
        out["note"] = "entry is expired; password withheld. Update the entry's expiry in KeePassXC, then retry."
    return out


def _add_tool(path: str, username: str = "", password: str = "", url: str = "",
              notes: str = "", props: dict | None = None):
    group_path, title = _split_path(path)
    with _writing() as kp:
        group = _resolve_group(kp, group_path)
        entry = kp.add_entry(group, title, username, password, url=url, notes=notes)
        for key, value in (props or {}).items():
            entry.set_custom_property(key, value)
        return _entry_summary(entry)


def _update_tool(path: str, title: str | None = None, username: str | None = None,
                 password: str | None = None, url: str | None = None,
                 notes: str | None = None, props: dict | None = None):
    with _writing() as kp:
        entry = _find(kp, path)
        if title is not None:
            entry.title = title
        if username is not None:
            entry.username = username
        if password is not None:
            entry.password = password
        if url is not None:
            entry.url = url
        if notes is not None:
            entry.notes = notes
        for key, value in (props or {}).items():
            if value is None:
                if key in entry.custom_properties:
                    entry.delete_custom_property(key)
            else:
                entry.set_custom_property(key, value)
        return _entry_summary(entry)


def _trash_tool(path: str):
    with _writing() as kp:
        entry = _find(kp, path)
        kp.trash_entry(entry)
        return {"trashed": path.strip("/")}


def _attach_tool(path: str, filename: str, content_b64: str):
    data = base64.b64decode(content_b64)
    if not data:
        raise ValueError("content_b64 is empty")
    with _writing() as kp:
        entry = _find(kp, path)
        for a in list(entry.attachments):
            if a.filename == filename:
                kp.delete_binary(a.id)  # removes the entry reference and the blob
        entry.add_attachment(kp.add_binary(data), filename)
        return {"attached": filename, "entry": path.strip("/"), "bytes": len(data)}


def _attachment_tool(path: str, filename: str):
    kp = _open()
    entry = _find(kp, path)
    for a in entry.attachments:
        if a.filename == filename:
            return {"filename": filename, "entry": path.strip("/"), "content_b64": base64.b64encode(a.data).decode()}
    raise ValueError(f"no attachment '{filename}' on '{path}' (see 'attachments' in vault_get)")


def _selftest():
    from datetime import datetime, timedelta, timezone
    from pykeepass import create_database

    fd, path = tempfile.mkstemp(suffix=".kdbx")
    os.close(fd)
    try:
        kp = create_database(path, password="selftest-pass")
        kp.add_entry(kp.root_group, "test entry", "user1", "secret-value", url="https://x.example")
        kp.add_entry(
            kp.root_group, "expired entry", "user2", "old-secret",
            expiry_time=datetime.now(timezone.utc) - timedelta(days=1),
        )
        kp.save()

        global DB, PASSPHRASE
        DB, PASSPHRASE = path, "selftest-pass"

        listed = _list_tool()
        assert len(listed) == 2, listed
        got = _get_tool("/test entry")
        assert got["password"] == "secret-value", got
        expired = _get_tool("/expired entry")
        assert expired["expired"] is True and expired["password"] is None, expired
        try:
            _get_tool("/nope")
            raise AssertionError("missing path must raise")
        except ValueError:
            pass

        _add_tool("/SSH/new key", username="agent", password="k", url="ssh://x", notes="n",
                  props={"k1": "v1"})
        added = _get_tool("/SSH/new key")
        assert added["username"] == "agent" and added["custom_properties"]["k1"] == "v1", added

        _update_tool("/SSH/new key", password="k2", notes=None, props={"k1": None, "k2": "v2"})
        updated = _get_tool("/SSH/new key")
        assert updated["password"] == "k2" and "k1" not in updated["custom_properties"], updated
        assert updated["custom_properties"]["k2"] == "v2", updated
        _update_tool("/SSH/new key", props={"absent-key": None})  # no-op, must not raise

        try:
            _add_tool("/Recycle Bin/nope")
            raise AssertionError("Recycle Bin path must be rejected")
        except ValueError:
            pass

        payload = base64.b64encode(b"-----BEGIN OPENSSH PRIVATE KEY-----").decode()
        _attach_tool("/SSH/new key", "id_test", payload)
        _attach_tool("/SSH/new key", "id_test", payload)  # replace same-name attachment
        fetched = _attachment_tool("/SSH/new key", "id_test")
        assert base64.b64decode(fetched["content_b64"]) == b"-----BEGIN OPENSSH PRIVATE KEY-----", fetched
        assert _get_tool("/SSH/new key")["attachments"] == ["id_test"]
        binaries_before = len(_open().binaries)
        _attach_tool("/SSH/new key", "id_test", payload)
        assert len(_open().binaries) == binaries_before, "replace must not strand old blobs"

        _trash_tool("/SSH/new key")
        assert _list_tool("/SSH") == [], _list_tool("/SSH")

        _http_selftest()
        print("selftest OK")
    finally:
        os.unlink(path)
        for extra in (path + ".lock",):
            if os.path.exists(extra):
                os.unlink(extra)


def _run(fn, *args):
    """JSON-encode a result; expected failures become ToolError so their text reaches the model (the SDK hides any other exception text)."""
    from mcp.server.mcpserver.exceptions import ToolError
    from pykeepass import exceptions as kpx

    expected = (ValueError, RuntimeError, OSError, kpx.CredentialsError, kpx.HeaderChecksumError,
                kpx.PayloadChecksumError, kpx.BinaryError, kpx.UnableToSendToRecycleBin)
    try:
        return json.dumps(fn(*args))
    except expected as e:  # wrong passphrase, half-synced KDBX, ...
        raise ToolError(f"{type(e).__name__}: {e}") from None


def server():
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("keepass", instructions="Read-write access to the personal KeePass vault. Writes are serialized in the keepass pod; do not edit the vault on two devices at once.")

    @mcp.tool()
    def vault_list(prefix: str = "") -> str:
        """List vault entries below the given group path prefix (no passwords). Empty prefix lists all. Returns a JSON array."""
        return _run(_list_tool, prefix)

    @mcp.tool()
    def vault_get(path: str, include_password: bool = True) -> str:
        """Fetch one entry by its full vault path (as returned by vault_list, e.g. /Group/Title). Returns a JSON object."""
        return _run(_get_tool, path, include_password)

    @mcp.tool()
    def vault_add(path: str, username: str = "", password: str = "", url: str = "",
                  notes: str = "", props: dict | None = None) -> str:
        """Create an entry at the full vault path (e.g. /Group/Title); missing groups are created. props sets custom string properties. Returns the new entry summary."""
        return _run(_add_tool, path, username, password, url, notes, props)

    @mcp.tool()
    def vault_update(path: str, title: str | None = None, username: str | None = None,
                     password: str | None = None, url: str | None = None,
                     notes: str | None = None, props: dict | None = None) -> str:
        """Update fields of the entry at the full vault path. Fields left as null are unchanged; empty string clears. props sets custom properties (null value deletes one). Returns the entry summary."""
        return _run(_update_tool, path, title, username, password, url, notes, props)

    @mcp.tool()
    def vault_trash(path: str) -> str:
        """Move the entry at the full vault path to the recycle bin (recoverable in KeePassXC). Returns a confirmation object."""
        return _run(_trash_tool, path)

    @mcp.tool()
    def vault_attach(path: str, filename: str, content_b64: str) -> str:
        """Store a base64-encoded file (e.g. an SSH private key) as an attachment on the entry at the full vault path. Replaces an existing attachment with the same name. Returns a confirmation object."""
        return _run(_attach_tool, path, filename, content_b64)

    @mcp.tool()
    def vault_attachment(path: str, filename: str) -> str:
        """Fetch an attachment by name from the entry at the full vault path. Returns {filename, content_b64}. Use vault_get to list attachment names."""
        return _run(_attachment_tool, path, filename)

    return mcp


class HubSecret:
    """Only the MCP hub may call: it sends X-Hub-Secret (Secret keepass-hub-secret). /healthz is open."""

    def __init__(self, app, secret):
        self.app, self.secret = app, secret.encode()

    async def __call__(self, scope, receive, send):
        import hmac

        from starlette.datastructures import Headers
        from starlette.responses import PlainTextResponse

        if scope["type"] == "http" and scope["path"] != "/healthz":
            got = Headers(scope=scope).get("x-hub-secret", "").encode(errors="replace")
            if not hmac.compare_digest(got, self.secret):
                await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def http_app(secret):
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    if not secret:
        sys.exit("KEEPASS_HUB_SECRET must be set (see keepass.yaml)")
    mcp = server()
    routes = [Route("/healthz", lambda request: PlainTextResponse("ok"))]
    routes += mcp.streamable_http_app(streamable_http_path="/keepass", host="0.0.0.0",
                                      stateless_http=True, json_response=True).routes
    return Starlette(routes=routes, lifespan=lambda app: mcp.session_manager.run(),
                     middleware=[Middleware(HubSecret, secret=secret)])


def _http_selftest():
    from starlette.testclient import TestClient

    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "selftest", "version": "0"}}}
    accept = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with TestClient(http_app("s3cret")) as c:
        assert c.get("/healthz").status_code == 200
        assert c.post("/keepass", json=init, headers=accept).status_code == 401
        assert c.post("/keepass", json=init, headers={**accept, "X-Hub-Secret": "nope"}).status_code == 401
        ok = {**accept, "X-Hub-Secret": "s3cret"}
        assert c.post("/keepass", json=init, headers=ok).json()["result"]["serverInfo"]["name"] == "keepass"
        r = c.post("/keepass", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                     "params": {"name": "vault_get", "arguments": {"path": "/nope"}}}, headers=ok)
        result = r.json()["result"]
        assert result["isError"] and "no entry at path" in result["content"][0]["text"], result
    print("http selftest OK")


def main():
    if "--selftest" in sys.argv:
        _selftest()
        return

    if not DB or not PASSPHRASE:
        sys.exit("KEEPASS_DB and KEEPASS_PASSPHRASE must be set (see keepass.yaml)")
    if "--http" in sys.argv:
        import uvicorn

        uvicorn.run(http_app(os.environ.get("KEEPASS_HUB_SECRET", "")), host="0.0.0.0",
                    port=int(os.environ.get("PORT", "8000")), log_level="info")
    else:
        server().run()


if __name__ == "__main__":
    main()
