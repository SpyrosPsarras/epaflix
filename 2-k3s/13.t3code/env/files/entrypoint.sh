#!/usr/bin/env bash
# CI smoke entrypoint (tools/smoke.sh); the pod runs ../one/files/entrypoint.sh.
set -euo pipefail

hub_config_failure() {
  echo "$*" >&2
  if [[ ${MCP_HUB_INSTANCE+x} || ${MCP_HUB_SERVICES_FILE+x} ]]; then exit 1; fi
}

if [[ ${MCP_HUB_INSTANCE+x} || ${MCP_HUB_SERVICES_FILE+x} ]] &&
   [[ -z ${MCP_HUB_TOKEN:-} || -z ${MCP_HUB_URL:-} ]]; then
  hub_config_failure "t3env: instance hub requires its own MCP_HUB_TOKEN and MCP_HUB_URL"
fi

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
    hub_config_failure "t3env: hub pi config failed"
fi

T3_HOME=$HOME/.t3
python3 /scripts/t3-pi-settings.py "$T3_HOME" "${ANTHROPIC_BASE_URL:-}"
python3 /scripts/private-config.py install /private-agent-config/bundle.json
SAFETY_ENTRY=$(/usr/bin/env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin \
  /usr/bin/python3 -I -S /scripts/cc-safety-net-install.py | tail -n 1)
python3 /scripts/pi-setup.py write "$HOME" "$SAFETY_ENTRY"
/scripts/pi.sh list | python3 /scripts/pi-setup.py check-packages "$HOME"

echo "t3env: $(t3 --version) claude=$(claude --version 2>/dev/null | head -1) pi=$(/scripts/pi.sh --version 2>/dev/null | head -1) codex=$(codex --version 2>/dev/null | head -1)"
exec t3 start --no-browser --host 0.0.0.0 --port 3773 --base-dir "$T3_HOME" \
  --auto-bootstrap-project-from-cwd "$PROJECT_DIR"
