#!/usr/bin/env python3
"""Mint (or re-register) one client's hub token and wire the client.

  add-client.py <pc-name> [--reuse] [--no-configure]   run ON that PC
  add-client.py t3code                                  the t3code pod

A PC token goes to KEY_FILE (default ~/.config/opencode/mcp-hub.key, mode
600; --reuse keeps the token already there). Then this PC's OpenCode and
Claude Code user configs get every hub path (tools/hub_clients.py); neither
stores the token itself, both read the key file at connect time.

The t3code token goes, sops-encrypted, into 13.t3code/one/mcp-hub-client.enc.yaml
with a new revision annotation, which rolls the pod once t3code is synced.

Either way files/clients.json gets the SHA-256 of the token: commit it and
merge; ArgoCD rolls the hub. Hashes of 256-bit random tokens are safe in git.
Revoke a client by deleting its line. The token is never printed.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile

HUB_URL = "https://mcp.epaflix.com"
ROOT = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                      check=True, cwd=os.path.dirname(os.path.abspath(__file__))).stdout.strip()
HUB = os.path.join(ROOT, "2-k3s/23.mcp-hub")
sys.path.insert(0, os.path.join(HUB, "tools"))
import hub_clients  # noqa: E402
import sops_secret  # noqa: E402


def write_private(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".mcp-hub-")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def t3code_secret(token):
    sops_secret.publish([sops_secret.encrypt("2-k3s/13.t3code/one/mcp-hub-client.enc.yaml", "mcp-hub-client",
                                             "t3code", {"token": token})])


def configure_pc(key_file):
    home = os.path.expanduser("~")
    shown = key_file.replace(home, "~", 1)
    oc = os.path.join(home, ".config/opencode/opencode.json")
    hub_clients.main(["", "opencode", oc, HUB_URL, f"Bearer {{file:{shown}}}"])
    if shutil.which("claude"):
        cfg = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", home), ".claude.json")
        helper = f"printf '{{\"Authorization\":\"Bearer %s\"}}' \"$(cat {key_file})\""
        hub_clients.main(["", "claude", cfg, HUB_URL, "helper:" + helper])


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name")
    p.add_argument("--reuse", action="store_true", help="PC: keep the token already in KEY_FILE")
    p.add_argument("--no-configure", action="store_true", help="PC: only register the token")
    a = p.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", a.name):
        sys.exit("client name: lowercase letters, digits, dashes")
    key_file = os.path.expanduser(os.environ.get("KEY_FILE", "~/.config/opencode/mcp-hub.key"))
    os.umask(0o077)

    if a.name == "t3code":
        token = secrets.token_urlsafe(32)
        t3code_secret(token)
    elif a.reuse:
        with open(key_file) as f:
            token = f.read().strip()
        if not token:
            sys.exit(f"no token in {key_file}")
    else:
        token = secrets.token_urlsafe(32)
        write_private(key_file, token + "\n")
        print(f"wrote {key_file}")

    clients_file = os.path.join(HUB, "files/clients.json")
    clients = json.load(open(clients_file)) if os.path.exists(clients_file) else {}
    clients[a.name] = hashlib.sha256(token.encode()).hexdigest()
    with open(clients_file, "w") as f:
        json.dump(dict(sorted(clients.items())), f, indent=2)
        f.write("\n")
    os.chmod(clients_file, 0o644)
    print(f"registered {a.name} in {os.path.relpath(clients_file, ROOT)}")

    if a.name != "t3code" and not a.no_configure:
        configure_pc(key_file)
    print("Commit and merge; ArgoCD rolls the hub." + (" Then sync t3code in ArgoCD." if a.name == "t3code" else ""))


if __name__ == "__main__":
    main()
