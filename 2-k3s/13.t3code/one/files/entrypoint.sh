#!/usr/bin/env bash
# Runs inside the t3code pod with image-installed CLIs in /tools. This script
# seeds the persisted HOME on the PVC, migrates legacy provider settings,
# and starts T3 without opening a browser.
#
# Required env: ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, HOME (PVC mount).
# Optional env: GITHUB_TOKEN (git push over https), T3_PROJECT_REPO,
# T3_PROJECT_DIR (checkout path; the merged t3code server keeps $HOME/epaflix).
set -euo pipefail

T3_PROJECT_REPO=${T3_PROJECT_REPO:-https://github.com/SpyrosPsarras/epaflix.git}
PROJECT_DIR=${T3_PROJECT_DIR:-$HOME/projects/$(basename "$T3_PROJECT_REPO" .git)}
export PATH=/tools/node_modules/.bin:$PATH

# github.com-only credential helper (files/git-credential-github.sh), set
# before the clone so a private remote works on first boot. The token stays
# in the process env; the helper prints it only to git, only for github.com.
mkdir -p "$(dirname "$PROJECT_DIR")"
git config --global credential.helper /scripts/git-credential-github.sh

# git@github.com: remotes (the Davidhorn checkouts). ssh expands ~ from
# /etc/passwd (/home/node), not $HOME, so the pinned key and host keys go there.
if [[ -r /run/t3-github-ssh/identity ]]; then
  SSH_DIR=$(getent passwd "$(id -u)" | cut -d: -f6)/.ssh
  mkdir -m 0700 "$SSH_DIR" 2>/dev/null || chmod 0700 "$SSH_DIR"
  cat >"$SSH_DIR/config" <<'EOF'
Host github.com
  User git
  IdentityFile /run/t3-github-ssh/identity
  IdentitiesOnly yes
  UserKnownHostsFile /run/t3-github-ssh/known_hosts
  StrictHostKeyChecking yes
EOF
  chmod 0600 "$SSH_DIR/config"
fi

# Same remote in both environments so T3 groups them as one project.
if [[ ! -d $PROJECT_DIR/.git ]]; then
  git clone -q "$T3_PROJECT_REPO" "$PROJECT_DIR"
fi

# OpenCode: cliproxy provider with env references, written once.
OC_DIR=$HOME/.config/opencode
mkdir -p "$OC_DIR/plugins"
# Replace read-only copies from earlier starts and keep the destination writable.
install -m 0644 /scripts/cliproxy-models.js "$OC_DIR/plugins/cliproxy-models.js"
install -m 0644 /scripts/jev-shadow.js "$OC_DIR/plugins/jev-shadow.js"
install -m 0644 /scripts/jev-auto.js "$OC_DIR/plugins/jev-auto.js"
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
  "enabled_providers": ["cliproxy"]
}
EOF
  chmod 0600 "$OC_DIR/opencode.json"
fi

# MCP hub (2-k3s/23.mcp-hub): every agent MCP server behind one gateway and
# this pod's hub token. Register every hub path for OpenCode, Claude and Codex
# in every home; the token stays an env reference, never a literal on the PVC.
# hub_clients.py also replaces the stdio keepass/searxng bridges and the
# hosted Notion entries the hub superseded. The claudeAgent instances run with
# CLAUDE_CONFIG_DIR=<home>/.claude (<home>/.claude/.claude.json); a plain
# `claude` in a shell reads <home>/.claude.json. Both get the servers.
if [[ -n ${MCP_HUB_TOKEN:-} && -n ${MCP_HUB_URL:-} ]]; then
  for h in "$HOME" /home/t3env-*; do
    [[ -d $h ]] || continue
    python3 /scripts/hub_clients.py opencode "$h/.config/opencode/opencode.json" "$MCP_HUB_URL" \
      'Bearer {env:MCP_HUB_TOKEN}' || echo "t3env: hub opencode config failed in $h" >&2
    for cfg in "$h/.claude/.claude.json" "$h/.claude.json"; do
      python3 /scripts/hub_clients.py claude "$cfg" "$MCP_HUB_URL" 'Bearer ${MCP_HUB_TOKEN}' || \
        echo "t3env: hub claude config failed for $cfg" >&2
    done
    python3 /scripts/hub_clients.py codex "$h/.codex" "$MCP_HUB_URL" MCP_HUB_TOKEN || \
      echo "t3env: hub codex config failed in $h" >&2
  done
fi

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
      "enabled": true
    },
    "codex": {
      "driver": "codex",
      "displayName": "Codex (via cliproxy)",
      "enabled": true,
      "config": {
        "launchArgs": "-c model_providers.cliproxy.name=\"cliproxy\" -c model_providers.cliproxy.base_url=\"${ANTHROPIC_BASE_URL}/v1\" -c model_providers.cliproxy.env_key=\"ANTHROPIC_AUTH_TOKEN\" -c model_providers.cliproxy.wire_api=\"responses\" -c model_provider=\"cliproxy\"",
        "customModels": ["gpt-5.3-codex", "codex-auto-review"]
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
configs += [v.setdefault("config", {}) for v in settings.get("providerInstances", {}).values()
            if v.get("driver") == "opencode"]
changed = False
for config in configs:
    if config.get("serverUrl") == "http://127.0.0.1:4096":
        del config["serverUrl"]
        changed = True
for config in configs[1:]:
    if config.get("binaryPath") != "/scripts/opencode-fast-version.sh":
        config["binaryPath"] = "/scripts/opencode-fast-version.sh"
        changed = True
if changed:
    with tempfile.NamedTemporaryFile(mode="w", dir=os.path.dirname(path), delete=False) as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    os.replace(f.name, path)
    print("t3env: migrated OpenCode to T3-managed servers")
PY

echo "t3env: $(t3 --version) claude=$(claude --version 2>/dev/null | head -1) opencode=$(opencode --version 2>/dev/null | head -1) codex=$(codex --version 2>/dev/null | head -1)"
python3 /scripts/private-config.py install /private-agent-config/bundle.json
# Migrated env homes carry skill links that pointed at /home/t3. Re-link them
# in place so the source-specific provider instances still find instructions.
for extra in /home/t3env-*; do
  [[ -d $extra/.t3 ]] && python3 /scripts/private-config.py install /private-agent-config/bundle.json --home "$extra"
done
# `serve` forces project bootstrap off; `start --no-browser` honors the flag.
exec t3 start --no-browser --host 0.0.0.0 --port 3773 --base-dir "$T3_HOME" \
  --auto-bootstrap-project-from-cwd "$PROJECT_DIR"
