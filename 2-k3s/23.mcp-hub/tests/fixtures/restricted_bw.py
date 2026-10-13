#!/usr/bin/env python3
"""Offline CLI fixture using the pinned client's plain-output contracts."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path(__file__).parent
scenario = json.loads((root / "scenario.json").read_text())
args = sys.argv[1:]
home = Path(os.environ["HOME"])
state_file = home / "state.json"
state = json.loads(state_file.read_text()) if state_file.exists() else {}
record = {"args": args, "home": str(home), "mode": home.stat().st_mode & 0o777,
          "env_keys": sorted(os.environ), "appdata": os.environ.get("BITWARDENCLI_APPDATA_DIR"),
          "session": os.environ.get("BW_SESSION") == "synthetic-session", "pid": os.getpid()}
with (root / "calls.jsonl").open("a") as calls:
    calls.write(json.dumps(record) + "\n")
command = "sync" if args[0] == "reader-sync" else args[0]
if scenario.get("orphan") == command:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (root / "descendant.pid").write_text(str(child.pid))
if scenario.get("silent_hang") == command:
    os.close(1)
    os.close(2)
    time.sleep(10)
if scenario.get("hang") == command:
    time.sleep(10)
if scenario.get("overflow") == command:
    sys.stdout.buffer.write(b"x" * (9 * 1024 * 1024))
    sys.exit()
if scenario.get("stderr_overflow") == command:
    sys.stderr.buffer.write(b"x" * (65 * 1024))
    sys.exit()
if scenario.get("fail") == command:
    print("synthetic-upstream-secret", file=sys.stderr)
    sys.exit(1)


def output(value):
    print(json.dumps(value))


def identity_login():
    path = root / "identity-calls.jsonl"
    with path.open("a") as calls:
        calls.write(json.dumps({"command": command, "home": str(home)}) + "\n")
    if len(path.read_text().splitlines()) > scenario.get("identity_login_limit", 10):
        print("synthetic-identity-rate-limit-error", file=sys.stderr)
        sys.exit(1)


if command == "--reader-capabilities":
    assert args == ["--reader-capabilities"]
    output(scenario.get("bridge_capability", {"bridge": "restricted-sync-v2", "client_version": "2026.8.0",
           "bundle_sha256": "688a23275876477fa5eddd42a1a61a25f7168b3cd164bd634235531904cc87f6"}))
    sys.exit()
elif command == "config":
    assert args == ["config", "server", "https://vault.homelab.example.test"]
    assert not state_file.exists()
    state["server"] = args[2]
elif command == "login":
    identity_login()
    count = sum(json.loads(line)["args"][0] == "login" for line in (root / "calls.jsonl").read_text().splitlines())
    if count > scenario.get("login_limit", 1000):
        sys.exit(1)
    assert args == ["login", "--apikey"]
    assert os.environ["BW_CLIENTID"] == "user." + scenario.get("approved_user_id", "00000000-0000-4000-8000-000000000001")
    assert os.environ["BW_CLIENTSECRET"] == "synthetic-api-secret"
    state["login"] = True
    state["token_expires_at"] = time.time() + 3600
    state["lastSync"] = "2020-01-01T00:00:00.000Z"
    print("You are logged in!")
elif command == "unlock":
    assert args == ["unlock", "--passwordenv", "BW_PASSWORD", "--raw"]
    if "expected_password" in scenario:
        assert os.environ["BW_PASSWORD"] == scenario["expected_password"]
    else:
        assert os.environ["BW_PASSWORD"] == "synthetic-master-password"
    assert state["login"]
    print("synthetic-session" if not scenario.get("empty_session") else "")
elif command == "status":
    assert "BW_CLIENTID" not in os.environ and "BW_CLIENTSECRET" not in os.environ
    assert "BW_PASSWORD" not in os.environ
    unlocked = "BW_SESSION" in os.environ
    if unlocked:
        assert os.environ["BW_SESSION"] == "synthetic-session"
    status = {"serverUrl": state["server"], "lastSync": state["lastSync"],
              "userEmail": "automation@homelab.example.test", "userId": scenario["user_id"],
              "status": "unlocked" if unlocked else "locked"}
    stage = "status_locked" if not unlocked else "status_after" if state.get("last_command") == "sync" else "status_before"
    status.update(scenario.get(stage, {}))
    output(status)
elif command == "sync":
    assert args == ["reader-sync"]
    if scenario.get("token_expired") or state["token_expires_at"] - time.time() < 300:
        identity_login()
        state["token_expires_at"] = time.time() + 3600
    assert os.environ["BW_SESSION"] == "synthetic-session"
    state["synced"] = True
    if not scenario.get("stale_sync"):
        state["lastSync"] = datetime.now(timezone.utc).isoformat()
    print("Syncing complete.")
elif command == "list":
    assert state["synced"]
    assert len(args) == 2
    assert args[1] in ("organizations", "collections", "items")
    if scenario.get("malformed") == args[1]:
        print("synthetic-invalid-json-secret")
    else:
        output(scenario[args[1]])
elif command == "get":
    assert args[1] == "attachment" and args[3] == "--itemid" and args[5] == "--raw"
    assert len(args) == 6
    item = next(it for it in scenario["items"] if it["id"] == args[4])
    assert any(a["id"] == args[2] for a in item["attachments"])
    sys.stdout.buffer.write(b"synthetic-attachment-bytes\0\xff")
else:
    raise AssertionError("unsupported fixture command")
state["last_command"] = command
state_file.write_text(json.dumps(state))
