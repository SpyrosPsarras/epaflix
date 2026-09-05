#!/usr/bin/env python3
"""Read-only MCP server over the Syncthing-synced KeePass KDBX.

Env: KEEPASS_DB (path to the KDBX), KEEPASS_PASSPHRASE. Both come from
/etc/t3code/t3code.env via the keepass-mcp wrapper. Self-test: --selftest
creates a throwaway vault, exercises both tools, and never touches KEEPASS_DB.
"""

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
    return out


def _open():
    from pykeepass import PyKeePass

    return PyKeePass(DB, password=PASSPHRASE)


def _all_entries(kp):
    return [e for e in kp.entries if "Recycle Bin" not in e.path]


def _find(kp, path):
    needle = "/" + path.strip("/")
    matches = [e for e in _all_entries(kp) if _entry_path(e) == needle]
    if not matches:
        raise ValueError(f"no entry at path '{path}' (list entries to see valid paths)")
    return matches[0]


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
        print("selftest OK")
    finally:
        os.unlink(path)


def main():
    if "--selftest" in sys.argv:
        _selftest()
        return

    from mcp.server.mcpserver import MCPServer

    if not DB or not PASSPHRASE:
        sys.exit("KEEPASS_DB and KEEPASS_PASSPHRASE must be set (keepass-mcp wrapper)")

    mcp = MCPServer("keepass", instructions="Read-only access to the personal KeePass vault.")

    @mcp.tool()
    def vault_list(prefix: str = "") -> str:
        """List vault entries below the given group path prefix (no passwords). Empty prefix lists all. Returns a JSON array."""
        return json.dumps(_list_tool(prefix))

    @mcp.tool()
    def vault_get(path: str, include_password: bool = True) -> str:
        """Fetch one entry by its full vault path (as returned by vault_list, e.g. /Group/Title). Returns a JSON object."""
        return json.dumps(_get_tool(path, include_password))

    mcp.run()


if __name__ == "__main__":
    main()
