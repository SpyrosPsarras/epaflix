#!/usr/bin/env bash
# CI smoke entrypoint (tools/smoke.sh); the pod runs ../one/files/entrypoint.sh.
# Uses image-installed CLIs in /tools. This script
# seeds the persisted HOME on the PVC, migrates legacy provider settings,
# and starts T3 without opening a browser.
#
# Required env: ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, HOME (PVC mount).
# Optional env: GITHUB_TOKEN (git push over https), T3_PROJECT_REPO.
set -euo pipefail

T3_PROJECT_REPO=${T3_PROJECT_REPO:-https://github.com/SpyrosPsarras/epaflix.git}
PROJECT_DIR=$HOME/projects/$(basename "$T3_PROJECT_REPO" .git)
export PATH=/tools/node_modules/.bin:$PATH

# The pod's OpenCode 1 backup (../one/files/entrypoint.sh), before anything opens the store.
python3 /scripts/opencode-v1-backup.py

# github.com-only credential helper (files/git-credential-github.sh), set
# before the clone so a private remote works on first boot. The token stays
# in the process env; the helper prints it only to git, only for github.com.
mkdir -p "$HOME/projects"
git config --global credential.helper /scripts/git-credential-github.sh

# Same remote in both environments so T3 groups them as one project.
if [[ ! -d $PROJECT_DIR/.git ]]; then
  git clone -q "$T3_PROJECT_REPO" "$PROJECT_DIR"
fi

# OpenCode: cliproxy provider with env references, written once.
OC_DIR=$HOME/.config/opencode
mkdir -p "$OC_DIR/plugins"
# The pod's OpenCode plugin set (../one/files/entrypoint.sh), so the smoke's
# OpenCode 2 parity check runs the same plugins. Replace read-only copies from
# earlier starts and keep the destination writable.
for plugin in cliproxy-models jev-auto jev-guard opencode-compat; do
  install -m 0644 "/scripts/$plugin.js" "$OC_DIR/plugins/$plugin.js"
done
install -m 0644 /scripts/jev-checks.md "$OC_DIR/jev-checks.md"
if [[ ! -f $OC_DIR/opencode.json ]]; then
  cat >"$OC_DIR/opencode.json" <<'EOF'
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "cliproxy": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "CLIProxyAPI",
      "options": {
        "baseURL": "{env:ANTHROPIC_BASE_URL}/v1",
        "apiKey": "{env:ANTHROPIC_AUTH_TOKEN}"
      }
    }
  },
  "model": "cliproxy/or-glm-5.3-flash",
  "enabled_providers": ["cliproxy", "jev-auto"]
}
EOF
  chmod 0600 "$OC_DIR/opencode.json"
fi
/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin /usr/bin/python3 -I -S /scripts/cc-safety-net-install.py \
  "$HOME/.local/share/opencode/cc-safety-net/2.4.11-print-third" --config "$OC_DIR/opencode.json"
# OpenCode 2 has no plugin hook for the provider allowlist: add Jev Auto to an
# older seed, and register superpowers as the pod does.
python3 - "$OC_DIR/opencode.json" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as f:
    config = json.load(f)
before = json.dumps(config, sort_keys=True)
enabled = config.get("enabled_providers")
if isinstance(enabled, list) and "cliproxy" in enabled and "jev-auto" not in enabled:
    enabled.append("jev-auto")
plugins = config.setdefault("plugin", [])
if not any(str(p[0] if isinstance(p, list) else p).startswith("superpowers@") for p in plugins):
    plugins.insert(0, "superpowers@git+https://github.com/obra/superpowers.git")
if json.dumps(config, sort_keys=True) != before:
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
PY
node /scripts/opencode-plugin-check.mjs /tools/node_modules/@opencode/cli/bin/opencode.exe "$OC_DIR" \
  cliproxy-models jev-auto jev-guard opencode-compat cc-safety-net superpowers

# T3 provider instances, written once so later edits from the UI survive
# restarts.
T3_HOME=$HOME/.t3
SETTINGS=$T3_HOME/userdata/settings.json
if [[ ! -f $SETTINGS ]]; then
  mkdir -p "$T3_HOME/userdata"
  cat >"$SETTINGS" <<EOF
{
  "providers": { "opencode": { "enabled": true } },
  "providerInstances": {
    "opencode": {
      "driver": "opencode",
      "displayName": "OpenCode (via cliproxy)",
      "enabled": true
    },
    "claudeAgent": {
      "driver": "claudeAgent",
      "displayName": "Claude (via cliproxy)",
      "enabled": false
    },
    "codex": {
      "driver": "codex",
      "displayName": "Codex (via cliproxy)",
      "enabled": true,
      "config": {
        "launchArgs": "-c model_providers.cliproxy.name=\"cliproxy\" -c model_providers.cliproxy.base_url=\"${ANTHROPIC_BASE_URL}/v1\" -c model_providers.cliproxy.env_key=\"ANTHROPIC_AUTH_TOKEN\" -c model_providers.cliproxy.wire_api=\"responses\" -c model_provider=\"cliproxy\"",
        "customModels": ["codex/codex-auto-review"]
      }
    }
  }
}
EOF
  chmod 0600 "$SETTINGS"
fi

# T3 only injects thread-scoped MCP tools into servers it manages. Migrate
# the endpoint seeded by older images, preserving other settings and URLs.
python3 - "$SETTINGS" <<'PY'
import json, os, sys, tempfile
path = sys.argv[1]
with open(path) as f:
    settings = json.load(f)
configs = [settings.get("providers", {}).get("opencode", {})]
configs += [v.get("config", {}) for v in settings.get("providerInstances", {}).values()
            if v.get("driver") == "opencode"]
changed = False
for config in configs:
    if config.get("serverUrl") == "http://127.0.0.1:4096":
        del config["serverUrl"]
        changed = True
if changed:
    with tempfile.NamedTemporaryFile(mode="w", dir=os.path.dirname(path), delete=False) as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    os.replace(f.name, path)
    print("t3env: migrated OpenCode to T3-managed servers")
PY

echo "t3env: $(t3 --version) claude=$(claude --version 2>/dev/null | head -1) opencode=$(opencode --version 2>/dev/null | head -1) codex=$(codex --version 2>/dev/null | head -1)"
# `serve` forces project bootstrap off; `start --no-browser` honors the flag.
python3 /scripts/private-config.py install /private-agent-config/bundle.json
exec t3 start --no-browser --host 0.0.0.0 --port 3773 --base-dir "$T3_HOME" \
  --auto-bootstrap-project-from-cwd "$PROJECT_DIR"
