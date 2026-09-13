#!/usr/bin/env bash
# Runs inside the t3env pod as the main container. The init container has
# already installed the locked CLIs into /tools. This script
# seeds the persisted HOME on the PVC (idempotent, never overwrites user
# state) and starts T3 without opening a browser.
#
# Required env: ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, HOME (PVC mount).
# Optional env: GITHUB_TOKEN (git push over https), T3_PROJECT_REPO.
set -euo pipefail

T3_PROJECT_REPO=${T3_PROJECT_REPO:-https://github.com/SpyrosPsarras/epaflix.git}
PROJECT_DIR=$HOME/projects/$(basename "$T3_PROJECT_REPO" .git)
export PATH=/tools/node_modules/.bin:$PATH

# github.com-only credential helper (files/git-credential-github.sh), set
# before the clone so a private remote works on first boot. The token stays
# in the process env; the helper prints it only to git, only for github.com.
mkdir -p "$HOME/projects"
git config --global credential.helper /scripts/git-credential-github.sh

# Same remote in both environments so T3 groups them as one project.
if [[ ! -d $PROJECT_DIR/.git ]]; then
  git clone -q "$T3_PROJECT_REPO" "$PROJECT_DIR"
fi

# OpenCode: cliproxy provider with env references (mirrors files/oc-config.py
# minus the keepass MCP and skills path, which do not exist in the pod).
OC_DIR=$HOME/.config/opencode
mkdir -p "$OC_DIR/plugins"
# Replace read-only copies from earlier starts and keep the destination writable.
install -m 0644 /scripts/cliproxy-models.js "$OC_DIR/plugins/cliproxy-models.js"
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

# T3 provider instances: same layout as files/t3-write-provider-settings.py,
# written once so later edits from the UI survive restarts.
T3_HOME=$HOME/.t3
SETTINGS=$T3_HOME/userdata/settings.json
if [[ ! -f $SETTINGS ]]; then
  mkdir -p "$T3_HOME/userdata"
  cat >"$SETTINGS" <<EOF
{
  "providers": { "opencode": { "enabled": true, "serverUrl": "http://127.0.0.1:4096" } },
  "providerInstances": {
    "opencode": {
      "driver": "opencode",
      "displayName": "OpenCode (via cliproxy)",
      "enabled": true,
      "config": { "serverUrl": "http://127.0.0.1:4096" }
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

echo "t3env: $(t3 --version) claude=$(claude --version 2>/dev/null | head -1) opencode=$(opencode --version 2>/dev/null | head -1) codex=$(codex --version 2>/dev/null | head -1)"
# `serve` forces project bootstrap off; `start --no-browser` honors the flag.
# Keep OpenCode running: its CLI cold start can exceed T3's fixed 4s probe.
# Stop both children if either exits; Kubernetes restarts the container.
pids=()
stop() { kill -TERM "${pids[@]}" 2>/dev/null || :; }
trap 'stop; wait; exit 143' TERM INT
opencode serve --hostname 127.0.0.1 --port 4096 &
pids+=("$!")
ready=false
for ((attempt = 0; attempt < 60; attempt++)); do
  if curl -fsS --max-time 1 http://127.0.0.1:4096/global/health >/dev/null 2>&1; then
    ready=true
    break
  fi
  kill -0 "${pids[0]}" 2>/dev/null || break
  sleep 1
done
if [[ $ready != true ]]; then
  echo "OpenCode server did not become healthy" >&2
  stop
  wait
  exit 1
fi
t3 start --no-browser --host 0.0.0.0 --port 3773 --base-dir "$T3_HOME" \
  --auto-bootstrap-project-from-cwd "$PROJECT_DIR" &
pids+=("$!")
set +e
wait -n "${pids[@]}"
rc=$?
stop
wait
exit "$rc"
