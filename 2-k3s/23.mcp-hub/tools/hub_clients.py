#!/usr/bin/env python3
"""Register every hub path in one home's agent configs. Shared by
tools/add-client.py (PCs) and the t3code pod entrypoint (a byte copy in
13.t3code/one/files, kept in sync by 13.t3code/one/tools/sync-shared.sh).

  hub_clients.py opencode <opencode.json> <hub url> <Authorization value>
  hub_clients.py claude   <.claude.json>  <hub url> <Authorization value | helper:CMD>
  hub_clients.py codex    <CODEX_HOME>    <hub url> <token env var>

Idempotent: a file is rewritten only when an entry differs. Existing entries
with a hub server's name are replaced (they were the stdio or hosted copies
the hub supersedes), and so is any entry still pointing at a removed stdio
bridge (LEGACY). Everything else in the file is left alone, including a
user's "enabled": false and any permission already set.
"""

import json
import os
import subprocess
import sys
import tempfile

SERVERS = {"gmail": "/gmail", "searxng": "/searxng", "notion": "/notion", "keepass": "/keepass",
           "kubernetes-epaflix": "/kubernetes"}
# Irreversible or vault/cluster-changing tools prompt in OpenCode (<server>_<tool>).
# The kubernetes names are kubernetes-mcp-server's at the tag pinned in
# 23.mcp-hub/kubernetes-mcp.yaml; recheck them when bumping it.
ASK = {
    "gmail": ["gmail_send", "gmail_send_draft", "gmail_trash"],
    "keepass": ["vault_add", "vault_update", "vault_trash", "vault_attach"],
    "kubernetes-epaflix": ["pods_delete", "pods_exec", "pods_run", "resources_create_or_update",
                           "resources_delete", "resources_scale", "helm_install", "helm_uninstall"],
}
LEGACY = ("keepass-remote.sh", "searxng-mcp")


def _legacy(entry):
    blob = json.dumps(entry.get("command", "")) + json.dumps(entry.get("args", ""))
    return any(marker in blob for marker in LEGACY)


def _write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".hub-clients-")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _load(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def opencode(config, hub, authorization):
    hub = hub.rstrip("/")
    mcp = config.setdefault("mcp", {})
    for name in [n for n, e in mcp.items() if n not in SERVERS and isinstance(e, dict) and _legacy(e)]:
        del mcp[name]
    for name, path in SERVERS.items():
        old = mcp.get(name, {})
        mcp[name] = {"type": "remote", "url": hub + path, "enabled": old.get("enabled", True) if
                     old.get("type") == "remote" else True, "timeout": 30000,
                     "headers": {"Authorization": authorization}}
    perms = config.setdefault("permission", {})
    if isinstance(perms, str):  # "allow"-everything shorthand: expand so per-tool rules can follow
        perms = config["permission"] = {"*": perms}
    for server, tools in ASK.items():
        for tool in tools:
            perms.setdefault(f"{server}_{tool}", "ask")
    return config


def claude(config, hub, authorization):
    hub = hub.rstrip("/")
    servers = config.setdefault("mcpServers", {})
    for name in [n for n, e in servers.items() if n not in SERVERS and isinstance(e, dict) and _legacy(e)]:
        del servers[name]
    for name, path in SERVERS.items():
        entry = {"type": "http", "url": hub + path}
        if authorization.startswith("helper:"):
            entry["headersHelper"] = authorization[len("helper:"):]
        else:
            entry["headers"] = {"Authorization": authorization}
        servers[name] = entry
    return config


def codex(codex_home, hub, env_var, run=subprocess.run):
    """Codex has no TOML writer we can rely on, so go through its CLI, only for entries that differ."""
    import tomllib

    hub = hub.rstrip("/")
    path = os.path.join(codex_home, "config.toml")
    current = {}
    if os.path.exists(path):
        with open(path, "rb") as f:
            current = tomllib.load(f).get("mcp_servers", {})
    env = {**os.environ, "CODEX_HOME": codex_home}
    os.makedirs(codex_home, exist_ok=True)
    changed = []
    for name in [n for n, e in current.items() if n not in SERVERS and _legacy(e)]:
        run(["codex", "mcp", "remove", name], env=env, check=True, capture_output=True)
        changed.append(name)
    for name, p in SERVERS.items():
        e = current.get(name)
        if e and e.get("url") == hub + p and e.get("bearer_token_env_var") == env_var and "command" not in e:
            continue
        if e is not None:
            run(["codex", "mcp", "remove", name], env=env, check=True, capture_output=True)
        run(["codex", "mcp", "add", name, "--url", hub + p, "--bearer-token-env-var", env_var],
            env=env, check=True, capture_output=True)
        changed.append(name)
    return changed


def main(argv):
    if len(argv) != 5 or argv[1] not in ("opencode", "claude", "codex"):
        sys.exit(__doc__)
    kind, target, hub, auth = argv[1:]
    if kind == "codex":
        changed = codex(target, hub, auth)
    else:
        before = _load(target)
        after = (opencode if kind == "opencode" else claude)(json.loads(json.dumps(before)), hub, auth)
        changed = after != before
        if changed:
            _write_json(target, after)
    if changed:
        print(f"hub_clients: updated {kind} config {target}")


def _selftest():
    oc = {"mcp": {"searxng": {"type": "local", "command": ["/scripts/searxng-mcp.py"]},
                  "gmail": {"type": "remote", "url": "x", "enabled": False},
                  "bridge": {"type": "local", "command": ["bash", "/scripts/keepass-remote.sh"]},
                  "mine": {"type": "local", "command": ["foo"]}},
          "permission": "allow"}
    out = opencode(oc, "https://hub/", "Bearer {env:T}")
    assert set(out["mcp"]) == set(SERVERS) | {"mine"}, out["mcp"]
    assert out["mcp"]["searxng"]["url"] == "https://hub/searxng" and out["mcp"]["searxng"]["enabled"] is True
    assert out["mcp"]["gmail"]["enabled"] is False, "a user's disable of a remote entry survives"
    assert out["permission"]["*"] == "allow" and out["permission"]["keepass_vault_trash"] == "ask"
    assert opencode(json.loads(json.dumps(out)), "https://hub", "Bearer {env:T}") == out, "idempotent"
    cl = claude({"mcpServers": {"keepass": {"command": "/scripts/keepass-remote.sh"},
                                "old": {"command": "bash", "args": ["/scripts/keepass-remote.sh"]}}, "x": 1},
                "http://h", "helper:cat key")
    assert set(cl["mcpServers"]) == set(SERVERS) and cl["x"] == 1, cl
    assert cl["mcpServers"]["kubernetes-epaflix"] == {"type": "http", "url": "http://h/kubernetes",
                                                      "headersHelper": "cat key"}, cl
    assert claude({}, "http://h", "Bearer ${T}")["mcpServers"]["gmail"]["headers"] == {"Authorization": "Bearer ${T}"}

    calls = []
    with tempfile.TemporaryDirectory() as home:
        with open(os.path.join(home, "config.toml"), "w") as f:
            f.write('[mcp_servers.keepass]\ncommand = "bash"\nargs = ["/scripts/keepass-remote.sh"]\n'
                    '[mcp_servers.gmail]\nurl = "http://h/gmail"\nbearer_token_env_var = "T"\n')
        changed = codex(home, "http://h/", "T", run=lambda cmd, **kw: calls.append(cmd[2:4]))
    assert "gmail" not in changed and ["remove", "keepass"] in calls and ["add", "keepass"] in calls, calls
    print("hub_clients selftest OK")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        _selftest()
    else:
        main(sys.argv)
