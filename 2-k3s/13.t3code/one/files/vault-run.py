"""Run a command with one Vaultwarden item in its environment.

Usage: python3 /scripts/vault-run.py <vault path> <command> [args...]

The item comes from the hub's vault_get, the same access the agent has through
the vaultwarden MCP tools. The command gets VAULT_PASSWORD, and VAULT_USERNAME
when the item has one. Agents use this instead of pasting a secret into a
command: tool arguments are saved in session files, and jev-guard blocks them.

  python3 /scripts/vault-run.py '/jira api token' \
    sh -c 'curl -su "me@example.com:$VAULT_PASSWORD" https://example.atlassian.net/rest/api/2/myself'
"""
import json
import os
import sys
import urllib.request


class NotFound(Exception):
    pass


def fetch(path):
    request = urllib.request.Request(
        os.environ["MCP_HUB_URL"].rstrip("/") + "/vaultwarden",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "vault_get", "arguments": {"path": path, "include_password": True}}}).encode(),
        headers={"Authorization": f"Bearer {os.environ['MCP_HUB_TOKEN']}", "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)["result"]
    if result.get("isError"):
        raise NotFound
    return json.loads(result["content"][0]["text"])


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    # Fixed messages only: hub and urllib error text can echo the token or the item.
    try:
        item = fetch(argv[0])
        password, username = item["password"], item.get("username")
    except NotFound:
        sys.exit(f"vault-run: vault_get failed for {argv[0]!r}; check the path with vault_list")
    except Exception as e:  # noqa: BLE001 - any message here may carry the response
        sys.exit(f"vault-run: hub request failed ({type(e).__name__})")
    if not isinstance(password, str) or not password:
        sys.exit(f"vault-run: {argv[0]!r} has no password")
    env = {**os.environ, "VAULT_PASSWORD": password}
    env.pop("VAULT_USERNAME", None)  # never leak one from an outer vault-run
    if isinstance(username, str) and username:
        env["VAULT_USERNAME"] = username
    try:
        os.execvpe(argv[1], argv[1:], env)
    except OSError as e:
        sys.exit(f"vault-run: cannot run {argv[1]!r} ({type(e).__name__})")


if __name__ == "__main__":
    main(sys.argv[1:])
