#!/usr/bin/env bash
# CI smoke entrypoint (tools/smoke.sh); the pod runs ../one/files/entrypoint.sh.
set -euo pipefail

T3_PROJECT_REPO=${T3_PROJECT_REPO:-https://github.com/SpyrosPsarras/epaflix.git}
PROJECT_DIR=$HOME/projects/$(basename "$T3_PROJECT_REPO" .git)
export PATH=/tools/node_modules/.bin:$PATH

mkdir -p "$HOME/projects"
git config --global credential.helper /scripts/git-credential-github.sh
if [[ ! -d $PROJECT_DIR/.git ]]; then
  git clone -q "$T3_PROJECT_REPO" "$PROJECT_DIR"
fi

if [[ -n ${MCP_HUB_TOKEN:-} && -n ${MCP_HUB_URL:-} ]]; then
  python3 /scripts/hub_clients.py pi "$HOME/.pi/agent/mcp.json" "$MCP_HUB_URL" MCP_HUB_TOKEN || \
    echo "t3env: hub pi config failed" >&2
fi

T3_HOME=$HOME/.t3
python3 /scripts/t3-pi-settings.py "$T3_HOME" "${ANTHROPIC_BASE_URL:-}"
python3 /scripts/private-config.py install /private-agent-config/bundle.json
SAFETY_ENTRY=$(/usr/bin/env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin \
  /usr/bin/python3 -I -S /scripts/cc-safety-net-install.py | tail -n 1)
python3 /scripts/pi-setup.py write "$HOME" "$SAFETY_ENTRY"
/scripts/pi.sh list | python3 /scripts/pi-setup.py check-packages "$HOME"
if [[ -n ${JEV_OPENROUTER_KEY_FILE:-} && -s $JEV_OPENROUTER_KEY_FILE ]]; then
  if ! python3 /scripts/pi-setup.py jev-config "$JEV_OPENROUTER_KEY_FILE" \
    /run/jev-guard/config.json >/dev/null; then
    echo "t3env: Jev guard configuration unavailable; startup continues" >&2
  fi
fi

echo "t3env: $(t3 --version) claude=$(claude --version 2>/dev/null | head -1) pi=$(/scripts/pi.sh --version 2>/dev/null | head -1) codex=$(codex --version 2>/dev/null | head -1)"
exec t3 start --no-browser --host 0.0.0.0 --port 3773 --base-dir "$T3_HOME" \
  --auto-bootstrap-project-from-cwd "$PROJECT_DIR"
