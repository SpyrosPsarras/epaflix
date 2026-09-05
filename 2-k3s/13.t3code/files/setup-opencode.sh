#!/usr/bin/env bash
# Installs OpenCode on the t3code guest, writes its cliproxy provider config
# (models = Spyros' Zed set), and enables the OpenCode driver in T3 settings.
set -euo pipefail

. "$(dirname "$0")/../versions.env"
npm i -g "opencode-ai@$OPENCODE_VERSION" >/dev/null 2>&1
echo "opencode installed: $(opencode --version 2>/dev/null | head -1)"

mkdir -p /home/spyros/.config/opencode
chown spyros:spyros /home/spyros/.config/opencode

sudo -iu spyros python3 /tmp/oc-config.py
chmod 600 /home/spyros/.config/opencode/opencode.json
chown spyros:spyros /home/spyros/.config/opencode/opencode.json

# Enable the opencode driver in the T3 server settings (hot-reload picks it up).
python3 - <<'PY'
import json
p = "/home/spyros/.t3/userdata/settings.json"
s = json.load(open(p))
s.setdefault("providers", {})
oc = s["providers"].setdefault("opencode", {})
oc["enabled"] = True
json.dump(s, open(p, "w"), indent=2)
print("opencode driver enabled; instances:", len(s["providerInstances"]))
PY
echo DONE