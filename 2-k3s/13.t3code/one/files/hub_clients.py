#!/usr/bin/env python3
"""Register every hub path in one home's agent configs. Shared by
tools/add-client.py (PCs) and the t3code pod entrypoint (a byte copy in
13.t3code/one/files, kept in sync by 13.t3code/one/tools/sync-shared.sh).

  hub_clients.py opencode <opencode.json> <hub url> <Authorization value>
  hub_clients.py claude   <.claude.json>  <hub url> <Authorization value | helper:CMD>
  hub_clients.py codex    <CODEX_HOME>    <hub url> <token env var>
  hub_clients.py pi       <mcp.json>      <hub url> <token env var>

Idempotent: a file is rewritten only when an entry differs. Existing entries
with a hub server's name are replaced (they were the stdio or hosted copies
the hub supersedes), and so is any entry still pointing at a removed stdio
bridge (LEGACY). Entries and <server>_* permissions of RETIRED servers are
removed. Everything else in the file is left alone, including a user's
"enabled": false for existing OpenCode remote entries and permissions already
set. Pi replaces hub entries whole, including their user-set keys.
"""

import json
import os
import subprocess
import sys
import tempfile

SERVERS = {"gmail": "/gmail", "searxng": "/searxng", "notion": "/notion", "vaultwarden": "/vaultwarden",
           "kubernetes-epaflix": "/kubernetes", "drive": "/drive"}
# Its instructions tell the agent to consult it before every task; trialled in OpenCode only.
OPENCODE_ONLY = {"jev": "/jev"}
# These tools ask in OpenCode (<server>_<tool>) and Pi (mcp__<server>__<tool>).
# The kubernetes and drive names are those of the images pinned in
# 23.mcp-hub/kubernetes-mcp.yaml and workspace-mcp.yaml; recheck them when bumping.
ASK = {
    "gmail": ["gmail_send", "gmail_send_draft", "gmail_trash"],
    "drive": ["create_drive_file", "create_drive_folder", "copy_drive_file", "import_to_google_doc",
              "import_to_google_sheets", "import_to_google_slides", "update_drive_file",
              "manage_drive_access", "set_drive_file_permissions"],
    "vaultwarden": ["vault_add", "vault_update", "vault_trash", "vault_attach"],
    "kubernetes-epaflix": ["pods_delete", "pods_exec", "pods_run", "resources_create_or_update",
                           "resources_delete", "resources_scale", "helm_install", "helm_uninstall"],
}
LEGACY = ("keepass-remote.sh", "searxng-mcp")
# Hub servers that were retired: their entries and permissions are removed.
RETIRED = ("keepass",)


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
    for name in RETIRED:
        mcp.pop(name, None)
    for name, path in {**SERVERS, **OPENCODE_ONLY}.items():
        old = mcp.get(name, {})
        mcp[name] = {"type": "remote", "url": hub + path, "enabled": old.get("enabled", True) if
                     old.get("type") == "remote" else True, "timeout": 30000,
                     "headers": {"Authorization": authorization}}
    perms = config.setdefault("permission", {})
    if isinstance(perms, str):  # "allow"-everything shorthand: expand so per-tool rules can follow
        perms = config["permission"] = {"*": perms}
    for key in [k for k in perms if k.startswith(tuple(r + "_" for r in RETIRED))]:
        del perms[key]
    for server, tools in ASK.items():
        for tool in tools:
            perms.setdefault(f"{server}_{tool}", "ask")
    return config


def claude(config, hub, authorization):
    hub = hub.rstrip("/")
    servers = config.setdefault("mcpServers", {})
    for name in [n for n, e in servers.items() if n not in SERVERS and isinstance(e, dict) and _legacy(e)]:
        del servers[name]
    for name in RETIRED:
        servers.pop(name, None)
    for name, path in SERVERS.items():
        entry = {"type": "http", "url": hub + path}
        if authorization.startswith("helper:"):
            entry["headersHelper"] = authorization[len("helper:"):]
        else:
            entry["headers"] = {"Authorization": authorization}
        servers[name] = entry
    return config


def pi(config, hub, env_var):
    servers = config.setdefault("mcpServers", {})
    for name in [n for n, e in servers.items() if n not in SERVERS and isinstance(e, dict) and _legacy(e)]:
        del servers[name]
    for name in RETIRED:
        servers.pop(name, None)
    for name, path in SERVERS.items():
        servers[name] = {"url": hub.rstrip("/") + path,
                         "headers": {"Authorization": "Bearer ${" + env_var + "}"}}
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
    for name in [n for n, e in current.items() if n not in SERVERS and n not in RETIRED and _legacy(e)]:
        run(["codex", "mcp", "remove", name], env=env, check=True, capture_output=True)
        changed.append(name)
    for name in [n for n in RETIRED if n in current]:
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
    if len(argv) != 5 or argv[1] not in ("opencode", "claude", "codex", "pi"):
        sys.exit(__doc__)
    kind, target, hub, auth = argv[1:]
    if kind == "codex":
        changed = codex(target, hub, auth)
    else:
        before = _load(target)
        after = {"opencode": opencode, "claude": claude, "pi": pi}[kind](json.loads(json.dumps(before)), hub, auth)
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
    oc["mcp"]["keepass"] = {"type": "remote", "url": "https://hub/keepass"}
    oc["permission"] = {"*": "allow", "keepass_vault_trash": "ask", "keepass_vault_add": "ask"}
    out = opencode(oc, "https://hub/", "Bearer {env:T}")
    assert set(out["mcp"]) == set(SERVERS) | set(OPENCODE_ONLY) | {"mine"}, out["mcp"]
    assert out["mcp"]["jev"]["url"] == "https://hub/jev", out["mcp"]["jev"]
    assert out["mcp"]["searxng"]["url"] == "https://hub/searxng" and out["mcp"]["searxng"]["enabled"] is True
    assert out["mcp"]["gmail"]["enabled"] is False, "a user's disable of a remote entry survives"
    assert out["permission"]["*"] == "allow" and "keepass" not in out["mcp"]
    assert not [k for k in out["permission"] if k.startswith("keepass_")], out["permission"]
    assert out["permission"]["vaultwarden_vault_trash"] == "ask" and out["mcp"]["vaultwarden"]["url"] == "https://hub/vaultwarden"
    assert opencode(json.loads(json.dumps(out)), "https://hub", "Bearer {env:T}") == out, "idempotent"
    cl = claude({"mcpServers": {"keepass": {"type": "http", "url": "http://h/keepass"},
                                "old": {"command": "bash", "args": ["/scripts/keepass-remote.sh"]}}, "x": 1},
                "http://h", "helper:cat key")
    assert set(cl["mcpServers"]) == set(SERVERS) and cl["x"] == 1, cl
    assert cl["mcpServers"]["kubernetes-epaflix"] == {"type": "http", "url": "http://h/kubernetes",
                                                      "headersHelper": "cat key"}, cl
    assert claude({}, "http://h", "Bearer ${T}")["mcpServers"]["gmail"]["headers"] == {"Authorization": "Bearer ${T}"}

    p = pi({"mcpServers": {"mine": {"url": "https://mine"}, "keepass": {},
                           "old": {"command": "keepass-remote.sh"}}}, "https://hub/", "MCP_HUB_TOKEN")
    assert set(p["mcpServers"]) == set(SERVERS) | {"mine"}, p
    assert p["mcpServers"]["vaultwarden"] == {"url": "https://hub/vaultwarden",
        "headers": {"Authorization": "Bearer ${MCP_HUB_TOKEN}"}}, p
    assert pi(json.loads(json.dumps(p)), "https://hub", "MCP_HUB_TOKEN") == p

    calls = []
    with tempfile.TemporaryDirectory() as home:
        with open(os.path.join(home, "config.toml"), "w") as f:
            f.write('[mcp_servers.keepass]\nurl = "http://h/keepass"\nbearer_token_env_var = "T"\n'
                    '[mcp_servers.gmail]\nurl = "http://h/gmail"\nbearer_token_env_var = "T"\n')
        changed = codex(home, "http://h/", "T", run=lambda cmd, **kw: calls.append(cmd[2:4]))
    assert "gmail" not in changed and ["remove", "keepass"] in calls and ["add", "keepass"] not in calls, calls
    print("hub_clients selftest OK")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        _selftest()
    else:
        main(sys.argv)
