#!/usr/bin/env python3
"""Read-write MCP server over Spyros's Vaultwarden vault.

Talks to `bw serve` (Bitwarden CLI's local REST API), which the bw container
of the vaultwarden-mcp pod (../vaultwarden-mcp.yaml) runs logged in and
unlocked on 127.0.0.1:8087. Env: BW_SERVE_URL, and for --http
VAULTWARDEN_HUB_SECRET. --http serves streamable HTTP at /vaultwarden on PORT
(8000) for the MCP hub, which sends the shared secret as X-Hub-Secret; without
--http it speaks stdio. --selftest runs every tool against an in-process fake
`bw serve` and never touches a real vault.

Same 7 tools as the KeePass server it replaces, except that vault_get never
returns a password or a custom field value. They leave only through POST /secret {"path": ...}
(same X-Hub-Secret), which the hub serves as /vault-secret for vault-run.py.
An item's path is
/<folder name>/<item name>, or /<item name> without a folder; folder names may
contain "/" (Personal/Git), so a path splits on its last "/".
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BW = os.environ.get("BW_SERVE_URL", "http://127.0.0.1:8087").rstrip("/")
SYNC_EVERY = 60  # seconds; reads pull edits made in Bitwarden Desktop at most this often
_last_sync = 0.0


def _bw(method, path, body=None, file=None, raw=False):
    """Call bw serve; return the envelope's data, or the raw bytes when raw.

    file=(filename, bytes) sends a multipart upload in field "file".
    """
    headers, data = {}, None
    if body is not None:
        headers["Content-Type"], data = "application/json", json.dumps(body).encode()
    if file is not None:
        boundary = uuid.uuid4().hex
        name = file[0]
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        data = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + file[1] + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(BW + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = r.read()
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("message", "")
        except ValueError:
            msg = ""
        raise RuntimeError(f"bw serve {method} {path.split('?')[0]}: HTTP {e.code} {msg}".rstrip()) from None
    except OSError as e:
        raise RuntimeError(f"bw serve unreachable at {BW} ({e}); is the vault unlocked?") from None
    if raw:
        return out
    env = json.loads(out or b"{}")
    if not env.get("success", True):
        raise RuntimeError(f"bw serve {method} {path.split('?')[0]}: {env.get('message', 'failed')}")
    return env.get("data")


def _sync(force=False):
    global _last_sync
    if force or time.monotonic() - _last_sync >= SYNC_EVERY:
        _bw("POST", "/sync")
        _last_sync = time.monotonic()


def _folders():
    return {f["id"]: f["name"] for f in _bw("GET", "/list/object/folders")["data"] if f.get("id")}


def _path(folder, name):
    return "/" + (f"{folder}/" if folder else "") + name


def _items(fresh=False):
    """Live items with their paths, in bw's order (first match wins on duplicate paths)."""
    _sync(fresh)
    folders = _folders()
    out = []
    for it in _bw("GET", "/list/object/items")["data"]:
        folder = folders.get(it.get("folderId"))
        out.append((_path(folder, it["name"]), it))
    return out


def _split(path):
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        raise ValueError("path must name an entry, e.g. /Folder/Name")
    return "/".join(parts[:-1]), parts[-1]


def _find(path, fresh=False):
    """First item at path. Writes pass fresh=True so they never PUT back a stale copy."""
    folder, name = _split(path)
    needle = _path(folder, name)
    for p, it in _items(fresh):
        if p == needle:
            return p, it
    raise ValueError(f"no entry at path '{path}' (list entries to see valid paths)")


def _summary(p, it):
    login = it.get("login") or {}
    uris = login.get("uris") or []
    return {"path": p, "title": it["name"], "expired": False, "username": login.get("username"),
            "url": uris[0].get("uri") if uris else None, "notes": it.get("notes"),
            "attachments": sorted(a["fileName"] for a in it.get("attachments") or [])}


def _set_props(it, props):
    fields = it.get("fields") or []
    for key, value in (props or {}).items():
        old = next((f for f in fields if f.get("name") == key), None)
        fields = [f for f in fields if f.get("name") != key]
        if value is not None:
            fields.append({"name": key, "value": value, "type": old.get("type", 0) if old else 0})
    it["fields"] = fields


def _list_tool(prefix: str = ""):
    want = prefix.strip("/")
    return [_summary(p, it) for p, it in _items() if p.lstrip("/").startswith(want)]


def _fields(it):
    return {f["name"]: f.get("value") for f in it.get("fields") or [] if f.get("name")}


def _get_tool(path: str):
    """No password and no custom field values: an API secret often sits in a field, and tool output lands in the
    model's context and session files. vault-run.py gets both from /secret."""
    p, it = _find(path)
    out = _summary(p, it)
    out["custom_fields"] = sorted(_fields(it))
    out["has_password"] = bool((it.get("login") or {}).get("password"))
    return out


def _secret(path: str):
    """For POST /secret only (vault-run.py via the hub's /vault-secret), never an MCP tool."""
    _, it = _find(path)
    names = [f["name"] for f in it.get("fields") or [] if f.get("name")]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"'{path}' has more than one custom field named {dupes}; rename them")
    login = it.get("login") or {}
    return {"password": login.get("password"), "username": login.get("username"), "fields": _fields(it)}


def _add_tool(path: str, username: str = "", password: str = "", url: str = "",
              notes: str = "", props: dict | None = None):
    folder, name = _split(path)
    folder_id = None
    if folder:
        folder_id = next((i for i, n in _folders().items() if n == folder), None)
        if folder_id is None:
            folder_id = _bw("POST", "/object/folder", {"name": folder})["id"]
    it = {"type": 1, "name": name, "notes": notes or None, "folderId": folder_id, "organizationId": None,
          "collectionIds": None, "reprompt": 0, "favorite": False,
          "fields": [], "login": {"username": username or None, "password": password or None,
                                  "uris": [{"match": None, "uri": url}] if url else []}}
    _set_props(it, props)
    created = _bw("POST", "/object/item", it)
    return _summary(_path(folder, name), created)


def _update_tool(path: str, title: str | None = None, username: str | None = None,
                 password: str | None = None, url: str | None = None,
                 notes: str | None = None, props: dict | None = None):
    p, it = _find(path, fresh=True)
    if (username, password, url) != (None, None, None) and it.get("type", 1) != 1:
        raise ValueError(f"'{path}' is not a login item; username, password and url cannot be set on it")
    login = it["login"] = it.get("login") or {}
    if title is not None:
        if "/" in title:
            raise ValueError("title must not contain '/' (it would make the item unreachable by path)")
        it["name"] = title
        p = p.rsplit("/", 1)[0] + "/" + title
    if username is not None:
        login["username"] = username
    if password is not None:
        login["password"] = password
    if url is not None:
        login["uris"] = [{"match": None, "uri": url}] if url else []
    if notes is not None:
        it["notes"] = notes
    _set_props(it, props)
    return _summary(p, _bw("PUT", f"/object/item/{it['id']}", it))


def _trash_tool(path: str):
    _, it = _find(path, fresh=True)
    _bw("DELETE", f"/object/item/{it['id']}")
    return {"trashed": path.strip("/")}


def _attach_tool(path: str, filename: str, content_b64: str):
    data = base64.b64decode(content_b64)
    if not data:
        raise ValueError("content_b64 is empty")
    if not filename or any(c in filename for c in '\r\n"/\\\0'):
        raise ValueError("filename must be non-empty and contain no quotes, slashes or control characters")
    _, it = _find(path, fresh=True)
    old = [a["id"] for a in it.get("attachments") or [] if a["fileName"] == filename]
    _bw("POST", f"/attachment?itemid={it['id']}", file=(filename, data))  # upload first: a failure keeps the old copy
    for aid in old:
        _bw("DELETE", f"/object/attachment/{aid}?itemid={it['id']}")
    return {"attached": filename, "entry": path.strip("/"), "bytes": len(data)}


def _attachment_tool(path: str, filename: str):
    _, it = _find(path)
    for a in it.get("attachments") or []:
        if a["fileName"] == filename:
            data = _bw("GET", f"/object/attachment/{a['id']}?itemid={it['id']}", raw=True)
            return {"filename": filename, "entry": path.strip("/"), "content_b64": base64.b64encode(data).decode()}
    raise ValueError(f"no attachment '{filename}' on '{path}' (see 'attachments' in vault_get)")


# --- selftest -------------------------------------------------------------

class _FakeBw:
    """In-memory stand-in for `bw serve`, same envelope and routes."""

    def __init__(self):
        self.items, self.folders, self.files, self.syncs = {}, {}, {}, 0
        self.fail_uploads = False

    def folder(self, name):
        fid = str(uuid.uuid4())
        self.folders[fid] = {"object": "folder", "id": fid, "name": name}
        return fid

    def item(self, name, folder=None, password="", fields=None):
        iid = str(uuid.uuid4())
        self.items[iid] = {"object": "item", "id": iid, "type": 1, "name": name, "notes": None,
                           "folderId": folder, "fields": fields or [], "attachments": [],
                           "login": {"username": None, "password": password, "uris": []}}
        return iid

    def serve(self):
        import http.server
        import threading
        import urllib.parse

        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, data=None, raw=None):
                body = raw if raw is not None else json.dumps(
                    {"success": code == 200, **({"data": data} if code == 200 else {"message": str(data)})}).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _route(self, method):
                u = urllib.parse.urlparse(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                p = u.path
                if method == "POST" and p == "/sync":
                    fake.syncs += 1
                    return self._send(200, {"object": "message", "title": "Syncing complete."})
                if method == "GET" and p == "/list/object/items":
                    return self._send(200, {"object": "list", "data": list(fake.items.values())})
                if method == "GET" and p == "/list/object/folders":
                    return self._send(200, {"object": "list", "data": list(fake.folders.values())})
                if method == "POST" and p == "/object/folder":
                    fid = fake.folder(json.loads(body)["name"])
                    return self._send(200, fake.folders[fid])
                if method == "POST" and p == "/object/item":
                    it = json.loads(body)
                    it.update(id=str(uuid.uuid4()), object="item", attachments=[])
                    fake.items[it["id"]] = it
                    return self._send(200, it)
                if p.startswith("/object/item/"):
                    iid = p.rsplit("/", 1)[1]
                    if iid not in fake.items:
                        return self._send(404, "Not found.")
                    if method == "PUT":
                        it = json.loads(body)
                        it["attachments"] = fake.items[iid]["attachments"]
                        fake.items[iid] = it
                        return self._send(200, it)
                    if method == "DELETE":
                        del fake.items[iid]
                        return self._send(200)
                if method == "POST" and p == "/attachment":
                    if fake.fail_uploads:
                        return self._send(500, "upload failed")
                    boundary = self.headers["Content-Type"].split("boundary=")[1].encode()
                    part = body.split(b"--" + boundary)[1]
                    head, data = part.split(b"\r\n\r\n", 1)
                    name = head.split(b'filename="')[1].split(b'"')[0].decode()
                    aid = str(uuid.uuid4())
                    fake.files[aid] = data[:-2]  # strip trailing CRLF
                    fake.items[q["itemid"]]["attachments"].append({"id": aid, "fileName": name})
                    return self._send(200, fake.items[q["itemid"]])
                if p.startswith("/object/attachment/"):
                    aid, it = p.rsplit("/", 1)[1], fake.items[q["itemid"]]
                    if method == "GET":
                        return self._send(200, raw=fake.files[aid])
                    if method == "DELETE":
                        it["attachments"] = [a for a in it["attachments"] if a["id"] != aid]
                        del fake.files[aid]
                        return self._send(200)
                return self._send(404, f"no route {method} {p}")

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_PUT(self):
                self._route("PUT")

            def do_DELETE(self):
                self._route("DELETE")

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv


def _raises(exc, fn, *args, contains=""):
    try:
        fn(*args)
    except exc as e:
        assert contains in str(e), f"{e!r} lacks {contains!r}"
        return
    raise AssertionError(f"{fn.__name__}{args} did not raise {exc.__name__}")


def _selftest():
    global BW, _last_sync
    fake = _FakeBw()
    srv = fake.serve()
    BW = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        personal = fake.folder("Personal")
        fake.item("github.com", personal, "p1", [{"name": "k1", "value": "v1", "type": 0}])
        fake.item("rootitem", None, "r")

        # test_sync_rate_limited
        _last_sync = 0.0
        _list_tool()
        _list_tool()
        assert fake.syncs == 1, fake.syncs

        # test_list_get_roundtrip
        assert len(_list_tool()) == 2, _list_tool()
        got = _get_tool("/Personal/github.com")
        assert "password" not in got and got["has_password"] and got["custom_fields"] == ["k1"], got
        assert "p1" not in json.dumps(got) and "v1" not in json.dumps(got), got
        assert got["expired"] is False and got["attachments"] == [], got
        assert _secret("/Personal/github.com") == {"password": "p1", "username": None, "fields": {"k1": "v1"}}
        assert _get_tool("/rootitem")["path"] == "/rootitem"
        assert [e["path"] for e in _list_tool("Personal")] == ["/Personal/github.com"]
        _raises(ValueError, _get_tool, "/nope", contains="list entries to see valid paths")
        fake.item("twice", personal, "p", [{"name": "k", "value": "dup-1", "type": 0},
                                           {"name": "k", "value": "dup-2", "type": 0}])
        try:
            _secret("/Personal/twice")
            raise AssertionError("duplicate field names must fail")
        except ValueError as e:
            assert "more than one custom field" in str(e) and "dup-" not in str(e), e
        assert _get_tool("/Personal/twice")["custom_fields"] == ["k"]

        # test_nested_folder_path
        added = _add_tool("/Personal/Git/new", username="u", password="x", url="https://g", notes="n",
                          props={"a": "b"})
        assert added["path"] == "/Personal/Git/new", added
        assert "Personal/Git" in [f["name"] for f in fake.folders.values()]
        got = _get_tool("/Personal/Git/new")
        assert (got["username"], _secret("/Personal/Git/new")["password"], got["url"], got["notes"]) == (
            "u", "x", "https://g", "n"), got
        assert got["custom_fields"] == ["a"] and _secret("/Personal/Git/new")["fields"] == {"a": "b"}, got
        _raises(ValueError, _add_tool, "/", contains="path must name an entry")

        # test_duplicate_path_reads_first
        dup = fake.folder("Dup")
        first = fake.item("x", dup, "a")
        second = fake.item("x", dup, "b")
        assert _secret("/Dup/x")["password"] == "a"
        _update_tool("/Dup/x", password="c")
        assert fake.items[first]["login"]["password"] == "c" and fake.items[second]["login"]["password"] == "b"

        # update: props delete/set, no-op delete, rename
        _update_tool("/Personal/github.com", props={"k1": None, "k2": "v2"})
        assert _secret("/Personal/github.com")["fields"] == {"k2": "v2"}
        _update_tool("/Personal/github.com", props={"absent": None})
        _update_tool("/Personal/github.com", title="gh", notes="")
        assert _get_tool("/Personal/gh")["notes"] == ""

        # attachments
        payload = base64.b64encode(b"key-bytes").decode()
        _attach_tool("/Personal/gh", "id_test", payload)
        _attach_tool("/Personal/gh", "id_test", payload)  # replaces, does not duplicate
        assert _get_tool("/Personal/gh")["attachments"] == ["id_test"]
        assert len(fake.files) == 1, fake.files
        assert base64.b64decode(_attachment_tool("/Personal/gh", "id_test")["content_b64"]) == b"key-bytes"
        _raises(ValueError, _attach_tool, "/Personal/gh", "e", "", contains="empty")
        _raises(ValueError, _attachment_tool, "/Personal/gh", "nope", contains="no attachment")

        _raises(ValueError, _attach_tool, "/Personal/gh", "a\r\nb", payload, contains="filename")
        # A failed upload must keep the old attachment (KeePass replaced atomically).
        fake.fail_uploads = True
        _raises(RuntimeError, _attach_tool, "/Personal/gh", "id_test", payload)
        fake.fail_uploads = False
        assert _get_tool("/Personal/gh")["attachments"] == ["id_test"]

        # writes re-sync first, so an edit made in the apps a moment ago is not overwritten
        before = fake.syncs
        _update_tool("/Personal/gh", notes="n2")
        assert fake.syncs == before + 1, (before, fake.syncs)

        # non-login items, titles with "/", hidden fields
        fake.items[fake.item("note", personal)]["type"] = 2
        _raises(ValueError, _update_tool, "/Personal/note", None, None, "pw", contains="not a login")
        _update_tool("/Personal/note", notes="ok")
        _raises(ValueError, _update_tool, "/Personal/gh", "a/b", contains="'/'")
        hid = fake.item("hidden", personal, fields=[{"name": "otp", "value": "1", "type": 1}])
        _update_tool("/Personal/hidden", props={"otp": "2"})
        assert fake.items[hid]["fields"] == [{"name": "otp", "value": "2", "type": 1}], fake.items[hid]["fields"]

        # trash
        assert _trash_tool("/Personal/gh") == {"trashed": "Personal/gh"}
        _raises(ValueError, _get_tool, "/Personal/gh")

        # POST /secret is the only way out for a password
        from starlette.testclient import TestClient

        with TestClient(http_app("s3cret")) as c:
            r = c.post("/secret", json={"path": "/rootitem"}, headers={"X-Hub-Secret": "s3cret"})
            assert r.status_code == 200 and r.json() == {"password": "r", "username": None, "fields": {}}, r.text
            r = c.post("/secret", json={"path": "/nope"}, headers={"X-Hub-Secret": "s3cret"})
            assert r.status_code == 404 and "no entry" in r.text, r.text

        # test_bw_unreachable_error
        srv.shutdown()
        srv.server_close()
        _last_sync = 0.0
        _raises(RuntimeError, _list_tool, contains="bw serve")

        _http_selftest()
        print("selftest OK")
    finally:
        BW = os.environ.get("BW_SERVE_URL", "http://127.0.0.1:8087").rstrip("/")


# --- MCP server -------------------------------------------------------------

def _run(fn, *args):
    """JSON-encode a result; expected failures become ToolError so their text reaches the model."""
    from mcp.server.mcpserver.exceptions import ToolError

    try:
        return json.dumps(fn(*args))
    except (ValueError, RuntimeError, OSError) as e:
        raise ToolError(f"{type(e).__name__}: {e}") from None


def server():
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("vaultwarden", instructions="Read-write access to the personal Vaultwarden vault (vaultwarden.epaflix.com). Paths are /<folder>/<item name>. Edits in Bitwarden apps show up here within a minute.")

    @mcp.tool()
    def vault_list(prefix: str = "") -> str:
        """List vault items below the given folder path prefix (no passwords). Empty prefix lists all. Returns a JSON array."""
        return _run(_list_tool, prefix)

    @mcp.tool()
    def vault_get(path: str) -> str:
        """Fetch one item by its full vault path (as returned by vault_list, e.g. /Folder/Name). Returns a JSON object without the password and with custom field names only, no values. To use them in a command, run `python3 /scripts/vault-run.py <path> <command>` (on a PC: 2-k3s/13.t3code/one/files/vault-run.py in the epaflix checkout); the command gets the password as $VAULT_PASSWORD and field "api token" as $VAULT_FIELD_API_TOKEN."""
        return _run(_get_tool, path)

    @mcp.tool()
    def vault_add(path: str, username: str = "", password: str = "", url: str = "",
                  notes: str = "", props: dict | None = None) -> str:
        """Create a login item at the full vault path (e.g. /Folder/Name); a missing folder is created. props sets custom text fields. Returns the new item summary."""
        return _run(_add_tool, path, username, password, url, notes, props)

    @mcp.tool()
    def vault_update(path: str, title: str | None = None, username: str | None = None,
                     password: str | None = None, url: str | None = None,
                     notes: str | None = None, props: dict | None = None) -> str:
        """Update fields of the item at the full vault path. Fields left as null are unchanged; empty string clears. props sets custom fields (null value deletes one). Returns the item summary."""
        return _run(_update_tool, path, title, username, password, url, notes, props)

    @mcp.tool()
    def vault_trash(path: str) -> str:
        """Move the item at the full vault path to the trash (recoverable in Bitwarden for 30 days). Returns a confirmation object."""
        return _run(_trash_tool, path)

    @mcp.tool()
    def vault_attach(path: str, filename: str, content_b64: str) -> str:
        """Store a base64-encoded file (e.g. an SSH private key) as an attachment on the item at the full vault path. Replaces an existing attachment with the same name. Returns a confirmation object."""
        return _run(_attach_tool, path, filename, content_b64)

    @mcp.tool()
    def vault_attachment(path: str, filename: str) -> str:
        """Fetch an attachment by name from the item at the full vault path. Returns {filename, content_b64}. Use vault_get to list attachment names."""
        return _run(_attachment_tool, path, filename)

    return mcp


class HubSecret:
    """Only the MCP hub may call: it sends X-Hub-Secret (Secret mcp-hub-vaultwarden). /healthz is open."""

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
        sys.exit("VAULTWARDEN_HUB_SECRET must be set (see vaultwarden-mcp.yaml)")
    import anyio
    from starlette.responses import JSONResponse

    async def get_secret(request):
        try:
            path = (await request.json())["path"]
            return JSONResponse(await anyio.to_thread.run_sync(_secret, path))
        except (ValueError, KeyError, TypeError) as e:  # bad body or unknown path; text never holds a secret
            return PlainTextResponse(f"{type(e).__name__}: {e}", status_code=404)
        except RuntimeError as e:
            return PlainTextResponse(str(e), status_code=502)

    mcp = server()
    routes = [Route("/healthz", lambda request: PlainTextResponse("ok")), Route("/secret", get_secret, methods=["POST"])]
    routes += mcp.streamable_http_app(streamable_http_path="/vaultwarden", host="0.0.0.0",
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
        assert c.post("/vaultwarden", json=init, headers=accept).status_code == 401
        assert c.post("/vaultwarden", json=init, headers={**accept, "X-Hub-Secret": "nope"}).status_code == 401
        ok = {**accept, "X-Hub-Secret": "s3cret"}
        assert c.post("/vaultwarden", json=init, headers=ok).json()["result"]["serverInfo"]["name"] == "vaultwarden"
        r = c.post("/vaultwarden", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                         "params": {"name": "vault_list", "arguments": {}}}, headers=ok)
        result = r.json()["result"]
        # bw serve is down at this point of the selftest: the error must name it.
        assert result["isError"] and "bw serve" in result["content"][0]["text"], result
        names = [t["name"] for t in c.post("/vaultwarden", json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                                           headers=ok).json()["result"]["tools"]]
        assert "vault_get" in names and not any("secret" in n for n in names), names
        schema = next(t for t in c.post("/vaultwarden", json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                                        headers=ok).json()["result"]["tools"] if t["name"] == "vault_get")
        assert "include_password" not in json.dumps(schema), schema
        assert c.post("/secret", json={"path": "/x"}).status_code == 401
        assert c.post("/secret", json={"path": "/x"}, headers={"X-Hub-Secret": "nope"}).status_code == 401
        assert c.post("/secret", json={"path": "/x"}, headers={"X-Hub-Secret": "s3cret"}).status_code == 502
        assert c.post("/secret", json={}, headers={"X-Hub-Secret": "s3cret"}).status_code == 404
        assert c.get("/secret", headers={"X-Hub-Secret": "s3cret"}).status_code == 405
    print("http selftest OK")


def main():
    if "--selftest" in sys.argv:
        _selftest()
        return
    if "--http" in sys.argv:
        import uvicorn

        uvicorn.run(http_app(os.environ.get("VAULTWARDEN_HUB_SECRET", "")), host="0.0.0.0",
                    port=int(os.environ.get("PORT", "8000")), log_level="info")
    else:
        server().run()


if __name__ == "__main__":
    main()
