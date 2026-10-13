import contextlib
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID

COMMAND_TIMEOUT = 30
OPERATION_TIMEOUT = 120
OUTPUT_LIMIT = 8 * 1024 * 1024
ERROR_LIMIT = 64 * 1024
BRIDGE_CAPABILITY = {"bridge": "restricted-sync-v2", "client_version": "2026.8.0",
                     "bundle_sha256": "688a23275876477fa5eddd42a1a61a25f7168b3cd164bd634235531904cc87f6"}


def _id(value):
    try:
        valid = isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("expected a canonical UUID")
    return value


def _text(value, limit=512):
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 or ord(c) == 127 for c in value)


@dataclass(frozen=True)
class ReaderConfig:
    server_url: str
    user_id: str
    organization_id: str
    collection_ids: tuple[str, ...]
    credentials_dir: Path
    scratch_dir: Path
    executable: Path

    def __post_init__(self):
        if not _text(self.server_url, 2048) or any(ord(c) <= 32 or ord(c) >= 127 or c == "\\" for c in self.server_url):
            raise ValueError("an explicit HTTPS server URL is required")
        try:
            url = urlsplit(self.server_url)
            port = url.port
        except ValueError:
            raise ValueError("invalid server URL") from None
        if url.scheme != "https" or not url.hostname or port == 0 or url.username is not None or (
                url.password is not None or url.query or url.fragment or self.server_url.endswith("/")):
            raise ValueError("an explicit HTTPS server URL without credentials or trailing slash is required")
        _id(self.user_id)
        _id(self.organization_id)
        if not isinstance(self.collection_ids, tuple) or not self.collection_ids or len(self.collection_ids) > 128:
            raise ValueError("approved collection IDs must be a nonempty unique tuple")
        for value in self.collection_ids:
            _id(value)
        if len(set(self.collection_ids)) != len(self.collection_ids):
            raise ValueError("approved collection IDs must be unique")
        for path in (self.credentials_dir, self.scratch_dir, self.executable):
            if not isinstance(path, Path) or not path.is_absolute():
                raise ValueError("connector paths must be explicit and absolute")


def private_bytes(path, limit):
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("private file path must be absolute")
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("private file must be owned and inaccessible to other users")
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("private file size limit exceeded")
    return data


class RestrictedVaultReader:
    def __init__(self, config: ReaderConfig):
        if not isinstance(config, ReaderConfig):
            raise ValueError("restricted reader configuration is required")
        self.config = config
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._home = None
        self._env = None
        self._credentials_digest = None
        self._faulted = False

    def _discard(self):
        self._env = None
        if self._home is not None:
            self._home.cleanup()
            self._home = None

    def invalidate(self):
        with self._lock:
            self._discard()
            self._credentials_digest = None
            self._faulted = False

    def close(self):
        self._closed.set()
        with self._lock:
            self._discard()

    def _command(self, args, env, deadline, limit=OUTPUT_LIMIT):
        end = min(deadline, time.monotonic() + COMMAND_TIMEOUT)
        if self._closed.is_set():
            raise RuntimeError("restricted vault reader closed")
        if end <= time.monotonic():
            raise RuntimeError("restricted vault operation timed out")
        try:
            with subprocess.Popen([str(self.config.executable), *args], env=env, cwd=env["HOME"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  start_new_session=True) as child:
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(child.stdout, selectors.EVENT_READ, (bytearray(), limit))
                        selector.register(child.stderr, selectors.EVENT_READ, (bytearray(), ERROR_LIMIT))
                        output = None
                        while selector.get_map():
                            remaining = end - time.monotonic()
                            if self._closed.is_set():
                                raise RuntimeError("restricted vault reader closed")
                            if remaining <= 0:
                                raise RuntimeError("restricted vault command timed out")
                            for key, _ in selector.select(min(remaining, 0.1)):
                                data = os.read(key.fileobj.fileno(), 65536)
                                buffer, maximum = key.data
                                if not data:
                                    if key.fileobj is child.stdout:
                                        output = bytes(buffer)
                                    selector.unregister(key.fileobj)
                                else:
                                    if len(buffer) + len(data) > maximum:
                                        raise RuntimeError("restricted vault command output limit exceeded")
                                    buffer.extend(data)
                        while True:
                            remaining = end - time.monotonic()
                            if self._closed.is_set():
                                raise RuntimeError("restricted vault reader closed")
                            if remaining <= 0:
                                raise RuntimeError("restricted vault command timed out")
                            try:
                                result = child.wait(timeout=min(remaining, 0.1))
                            except subprocess.TimeoutExpired:
                                continue
                            if result:
                                raise RuntimeError("restricted vault command failed")
                            return output
                finally:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError("restricted vault command unavailable") from None

    def _json(self, args, env, deadline):
        try:
            return json.loads(self._command(args, env, deadline))
        except (ValueError, TypeError):
            raise RuntimeError("restricted vault returned invalid JSON") from None

    def _credential(self, name):
        try:
            data = private_bytes(self.config.credentials_dir / name, 16384)
            value = data.decode("utf-8")
            if len(data) > 16384 or not value or "\0" in value:
                raise ValueError
            return value
        except (OSError, ValueError):
            raise RuntimeError("restricted vault credentials unavailable") from None

    def _status(self, env, deadline, expected="unlocked"):
        value = self._json(["status"], env, deadline)
        if not isinstance(value, dict) or value.get("status") != expected or (
                value.get("userId") != self.config.user_id or value.get("serverUrl") != self.config.server_url):
            raise RuntimeError("restricted vault identity or lock status mismatch")
        return value

    @contextlib.contextmanager
    def _snapshot(self):
        deadline = time.monotonic() + OPERATION_TIMEOUT
        if not self._lock.acquire(timeout=max(0, min(1, deadline - time.monotonic()))):
            raise RuntimeError("restricted vault reader busy")
        try:
            if self._closed.is_set():
                raise RuntimeError("restricted vault reader closed")
            credentials = {name: self._credential(name) for name in ("client-id", "client-secret", "password")}
            if credentials["client-id"] != "user." + self.config.user_id:
                raise RuntimeError("restricted vault client identity mismatch")
            digest = hashlib.sha256(json.dumps(credentials, sort_keys=True).encode()).digest()
            if digest != self._credentials_digest:
                self._discard()
                self._credentials_digest = digest
                self._faulted = False
            if self._faulted:
                raise RuntimeError("restricted vault session invalidated")
            info = self.config.scratch_dir.lstat()
            if not self.config.scratch_dir.is_dir() or self.config.scratch_dir.is_symlink() or (
                    info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise RuntimeError("restricted vault scratch root must be private")
            if self._env is None:
                self._home = tempfile.TemporaryDirectory(prefix="vault-reader-", dir=self.config.scratch_dir)
                home = self._home.name
                env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "TMPDIR": home,
                       "BITWARDENCLI_APPDATA_DIR": home, "BW_NOINTERACTION": "true"}
                if self._json(["--reader-capabilities"], env, deadline) != BRIDGE_CAPABILITY:
                    raise RuntimeError("restricted vault bridge compatibility mismatch")
                self._command(["config", "server", self.config.server_url], env, deadline)
                self._command(["login", "--apikey"], {**env, "BW_CLIENTID": credentials["client-id"],
                              "BW_CLIENTSECRET": credentials["client-secret"]}, deadline)
                self._status(env, deadline, "locked")
                session = self._command(["unlock", "--passwordenv", "BW_PASSWORD", "--raw"],
                                        {**env, "BW_PASSWORD": credentials["password"]}, deadline, 16384)
                try:
                    session = session.decode().strip()
                    if not _text(session, 16384):
                        raise ValueError
                except ValueError:
                    raise RuntimeError("restricted vault unlock failed") from None
                env["BW_SESSION"] = session
                self._env = env
            env = self._env
            before = self._status(env, deadline)
            started = datetime.now(timezone.utc)
            self._command(["reader-sync"], env, deadline)
            after = self._status(env, deadline)
            try:
                stamp = datetime.fromisoformat(after["lastSync"].replace("Z", "+00:00"))
                if stamp.tzinfo is None or stamp.timestamp() < started.timestamp() - 1 or (
                        stamp.timestamp() > datetime.now(timezone.utc).timestamp() + 1 or
                        after["lastSync"] == before.get("lastSync")):
                    raise ValueError
            except (KeyError, ValueError, TypeError, AttributeError):
                raise RuntimeError("restricted vault refresh not confirmed") from None
            organizations = self._json(["list", "organizations"], env, deadline)
            if not isinstance(organizations, list) or len(organizations) != 1 or (
                    not isinstance(organizations[0], dict) or
                    organizations[0].get("id") != self.config.organization_id or
                    type(organizations[0].get("type")) is not int or organizations[0]["type"] != 2 or
                    type(organizations[0].get("status")) is not int or organizations[0]["status"] != 2 or
                    organizations[0].get("enabled") is not True):
                raise RuntimeError("restricted vault organization membership mismatch")
            collections = self._json(["list", "collections"], env, deadline)
            if not isinstance(collections, list) or len(collections) != len(self.config.collection_ids) or (
                    any(not isinstance(c, dict) or c.get("organizationId") != self.config.organization_id or
                        c.get("id") not in self.config.collection_ids for c in collections) or
                    {c["id"] for c in collections} != set(self.config.collection_ids)):
                raise RuntimeError("restricted vault visible collection grants mismatch")
            items = self._json(["list", "items"], env, deadline)
            if not isinstance(items, list) or any(not isinstance(it, dict) for it in items):
                raise RuntimeError("restricted vault returned invalid items")
            scoped = []
            for item in items:
                assigned = item.get("collectionIds")
                if item.get("organizationId") != self.config.organization_id or not isinstance(assigned, list) or (
                        not assigned or any(c not in self.config.collection_ids for c in assigned) or
                        item.get("deletedDate") or item.get("archivedDate")):
                    continue
                try:
                    _id(item.get("id"))
                    if not _text(item.get("name")) or type(item.get("type")) is not int:
                        raise ValueError
                except ValueError:
                    raise RuntimeError("restricted vault returned invalid item metadata") from None
                scoped.append(item)
            if len({it["id"] for it in scoped}) != len(scoped):
                raise RuntimeError("restricted vault returned duplicate item IDs")
            yield scoped, env, deadline
        except (OSError, RuntimeError) as error:
            self._faulted = True
            self._discard()
            if isinstance(error, OSError):
                raise RuntimeError("restricted vault temporary state unavailable") from None
            raise
        finally:
            self._lock.release()

    def _collection(self, value):
        if value not in self.config.collection_ids:
            raise ValueError("collection is not approved for this reader")

    def _reference(self, reference):
        if not isinstance(reference, dict) or set(reference) not in (
                {"collection_id", "item_id"}, {"collection_id", "title"}):
            raise ValueError("reference requires collection_id and exactly one of item_id or title")
        self._collection(reference["collection_id"])
        if "item_id" in reference:
            _id(reference["item_id"])
        elif not _text(reference["title"]):
            raise ValueError("invalid item title")

    def _find(self, items, reference):
        key, field = ("item_id", "id") if "item_id" in reference else ("title", "name")
        matches = [it for it in items if reference["collection_id"] in it["collectionIds"] and
                   it[field] == reference[key]]
        if not matches:
            raise ValueError("item not found in approved collection")
        if len(matches) != 1:
            raise ValueError("ambiguous item reference; use an item ID")
        return matches[0]

    def _fields(self, item):
        fields = item.get("fields") or []
        if not isinstance(fields, list) or any(not isinstance(f, dict) or not _text(f.get("name")) or
                                              f.get("value") is not None and not isinstance(f["value"], str)
                                              for f in fields):
            raise RuntimeError("restricted vault returned invalid fields")
        return fields

    def _attachments(self, item):
        attachments = item.get("attachments") or []
        if not isinstance(attachments, list) or any(not isinstance(a, dict) or
                not isinstance(a.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", a["id"]) or
                not _text(a.get("fileName")) or a["fileName"] in (".", "..") or
                any(c in a["fileName"] for c in "/\\") for a in attachments):
            raise RuntimeError("restricted vault returned invalid attachments")
        if len({a["id"].lower() for a in attachments}) != len(attachments):
            raise RuntimeError("restricted vault returned duplicate attachment IDs")
        return attachments

    def _metadata(self, item):
        login = item.get("login") or {}
        if not isinstance(login, dict):
            raise RuntimeError("restricted vault returned invalid login")
        return {"references": [{"collection_id": c, "item_id": item["id"]}
                               for c in sorted(set(item["collectionIds"]))],
                "title": item["name"], "type": item["type"], "has_password": bool(login.get("password")),
                "custom_fields": sorted({f["name"] for f in self._fields(item)}),
                "attachments": [{"id": a["id"], "filename": a["fileName"]} for a in self._attachments(item)]}

    def list_items(self, collection_id=None):
        if collection_id is not None:
            self._collection(collection_id)
        with self._snapshot() as (items, _, _):
            return [self._metadata(it) for it in sorted(items, key=lambda it: (it["name"], it["id"]))
                    if collection_id is None or collection_id in it["collectionIds"]]

    def get(self, reference):
        self._reference(reference)
        with self._snapshot() as (items, _, _):
            return self._metadata(self._find(items, reference))

    def secret(self, reference):
        """Broker-only payload. Never expose this method as a model tool."""
        self._reference(reference)
        with self._snapshot() as (items, _, _):
            item = self._find(items, reference)
            fields = self._fields(item)
            if len({f["name"] for f in fields}) != len(fields):
                raise ValueError("duplicate custom field names")
            login = item.get("login") or {}
            if not isinstance(login, dict) or any(login.get(k) is not None and not isinstance(login[k], str)
                                                   for k in ("password", "username")):
                raise RuntimeError("restricted vault returned invalid login")
            return {"password": login.get("password"), "username": login.get("username"),
                    "fields": {f["name"]: f.get("value") for f in fields}}

    def attachment(self, reference, attachment):
        self._reference(reference)
        if not _text(attachment) or attachment in (".", "..") or any(c in attachment for c in "/\\"):
            raise ValueError("invalid attachment identifier or filename")
        with self._snapshot() as (items, env, deadline):
            item = self._find(items, reference)
            attachments = self._attachments(item)
            matches = [a for a in attachments if a["id"] == attachment or a["fileName"] == attachment]
            if len(matches) != 1:
                raise ValueError("attachment not found or ambiguous on validated item")
            selected = matches[0]
            cli_matches = [a for a in attachments if a["id"].lower() == selected["id"].lower() or
                           selected["id"].lower() in a["fileName"].lower()]
            if len(cli_matches) != 1:
                raise ValueError("attachment identifier collides with CLI name matching")
            content = self._command(["get", "attachment", selected["id"], "--itemid", item["id"], "--raw"],
                                    env, deadline)
            return {"filename": selected["fileName"], "content": content}
