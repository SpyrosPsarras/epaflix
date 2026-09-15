#!/usr/bin/env bash
# Installs the SearXNG search guard (epaflix#1038) on the Pi harness host.
# Run as the user that runs pi. Idempotent; re-run after edits to
# search-guard.py. Does three things:
#   1. installs the guard to ~/.local/bin and a systemd --user unit
#   2. repoints searxngBaseUrl at the guard in every web-search.json it finds
#      and adds 127.0.0.1/32 to ssrf.allowRanges (pi-web-access blocks
#      loopback endpoints unless explicitly allowed)
#   3. verifies the guard end to end
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
GUARD_URL="http://127.0.0.1:8893"
# systemctl --user needs the session bus; su/sudo strips XDG_RUNTIME_DIR.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

install -m 0755 "$DIR/search-guard.py" "$HOME/.local/bin/search-guard.py"
echo "installed $HOME/.local/bin/search-guard.py"

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/search-guard.service" <<'EOF'
[Unit]
Description=SearXNG JSON guard for pi web search (epaflix#1038)
After=network-online.target

[Service]
ExecStart=%h/.local/bin/search-guard.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now search-guard.service
# Without lingering the guard stops at logout and web search dies with it.
if ! loginctl enable-linger 2>/dev/null; then
  echo "warning: could not enable lingering; the guard stops on logout" >&2
fi
echo "search-guard.service enabled"

python3 - "$GUARD_URL" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

url = sys.argv[1]
candidates = [
    Path.home() / ".pi/web-search.json",
    Path.home() / ".pi/agent/web-search.json",
]
profiles = Path.home() / ".pi/profiles"
if profiles.is_dir():
    candidates += sorted(profiles.glob("*/web-search.json"))

touched = []
for path in candidates:
    if not path.exists():
        continue
    config = json.loads(path.read_text())
    if not isinstance(config, dict):
        raise SystemExit("%s is not a JSON object; refusing to edit" % path)
    config["searxngBaseUrl"] = url
    ranges = config.setdefault("ssrf", {}).setdefault("allowRanges", [])
    if "127.0.0.1/32" not in ranges:
        ranges.append("127.0.0.1/32")
    with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, delete=False) as out:
        json.dump(config, out, indent=2)
        out.write("\n")
    os.replace(out.name, path)
    touched.append(path)

if not touched:
    raise SystemExit("no web-search.json found under ~/.pi; is pi-web-access "
                     "installed on this machine? The guard is running but the "
                     "harness still points at the upstream directly.")
for path in touched:
    print("repointed %s -> %s" % (path, url))
PY

sleep 1
if curl -fsS --max-time 40 \
    "$GUARD_URL/search?q=search-guard-selftest&format=json" | python3 -c 'import json,sys; json.load(sys.stdin)'; then
  echo "verified: guard returned JSON for a live query"
else
  echo "warning: the guard answered non-JSON or is unreachable; web search" \
       "will now fail loudly instead of quietly. Check:" \
       "journalctl --user -u search-guard -n 20" >&2
fi
