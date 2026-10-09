"""Run a command with one Vaultwarden item in its environment.

Usage: python3 /scripts/vault-run.py <vault path> <command> [args...]

The item comes from the hub's /vault-secret route (2-k3s/23.mcp-hub), the only
place a password leaves the vault: the vault_get MCP tool never returns one.
The command gets VAULT_PASSWORD and VAULT_USERNAME when the item has them, and
each non-empty custom field as VAULT_FIELD_<NAME> (name upper-cased,
non-alphanumerics to "_"; two fields mapping to one name is an error).
Agents use this instead of putting a secret in a command: tool arguments and
results are saved in session files.

Hub: MCP_HUB_URL and MCP_HUB_TOKEN (t3code pod), else https://mcp.epaflix.com
and the token in ~/.config/opencode/mcp-hub.key (a LAN PC).

  python3 /scripts/vault-run.py '/jira api token' \
    sh -c 'curl -su "me@example.com:$VAULT_PASSWORD" https://example.atlassian.net/rest/api/2/myself'
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request


class NotFound(Exception):
    pass


def fetch(path):
    token = os.environ.get("MCP_HUB_TOKEN") or open(os.path.expanduser("~/.config/opencode/mcp-hub.key")).read().strip()
    request = urllib.request.Request(
        os.environ.get("MCP_HUB_URL", "https://mcp.epaflix.com").rstrip("/") + "/vault-secret",
        data=json.dumps({"path": path}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound from None
        raise


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    # Fixed messages only: hub and urllib error text can echo the token or the item.
    try:
        item = fetch(argv[0])
        password, username = item["password"], item.get("username")
        fields = {}
        for name, value in (item.get("fields") or {}).items():
            if isinstance(value, str) and value:
                key = "VAULT_FIELD_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()
                if key in fields:
                    sys.exit(f"vault-run: {argv[0]!r} has two custom fields that both map to ${key}; rename one")
                fields[key] = value
    except NotFound:
        sys.exit(f"vault-run: no vault item at {argv[0]!r}; check the path with vault_list")
    except Exception as e:  # noqa: BLE001 - any message here may carry the response
        sys.exit(f"vault-run: hub request failed ({type(e).__name__})")
    has_password = isinstance(password, str) and bool(password)
    if not has_password and not fields:
        sys.exit(f"vault-run: {argv[0]!r} has no password and no custom fields")
    env = {k: v for k, v in os.environ.items() if not k.startswith("VAULT_FIELD_")}
    for k in ("VAULT_PASSWORD", "VAULT_USERNAME"):
        env.pop(k, None)  # never leak one from an outer vault-run
    if has_password:
        env["VAULT_PASSWORD"] = password
    if isinstance(username, str) and username:
        env["VAULT_USERNAME"] = username
    env.update(fields)
    try:
        os.execvpe(argv[1], argv[1:], env)
    except OSError as e:
        sys.exit(f"vault-run: cannot run {argv[1]!r} ({type(e).__name__})")


if __name__ == "__main__":
    main(sys.argv[1:])
