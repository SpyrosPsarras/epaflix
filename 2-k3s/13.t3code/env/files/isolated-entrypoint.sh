#!/usr/bin/env bash
set -euo pipefail
inputs=/run/t3-inputs
[[ ${T3_RUNTIME_VARIANT:-} == "${MCP_HUB_INSTANCE:-}" ]]
[[ $MCP_HUB_INSTANCE == homelab || $MCP_HUB_INSTANCE == work ]]
[[ $HOME == /home/t3 && ${T3CODE_HOME:-} == "$HOME/.t3" ]]
export PATH=/tools/node_modules/.bin:$PATH
export KUBECONFIG=$inputs/kubeconfig MCP_HUB_SERVICES_FILE=$inputs/services.json
if [[ $MCP_HUB_INSTANCE == homelab ]]; then
  export MCP_HUB_SERVICES_FILE=$HOME/.config/t3-runtime/services.json
fi
python3 /scripts/isolated-config.py prepare "$inputs" "$HOME" "$MCP_HUB_INSTANCE"
read_config() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$inputs/runtime.json" "$1"; }
ANTHROPIC_BASE_URL=$(read_config modelUrl)
MCP_HUB_URL=$(read_config hubUrl)
export ANTHROPIC_BASE_URL MCP_HUB_URL
repo=$(read_config projectUrl)
project=$HOME/projects/$MCP_HUB_INSTANCE
export GIT_TERMINAL_PROMPT=0
git config --global credential.helper ''
if [[ $(read_config gitAuth) == github-token ]]; then
  git config --global credential.helper /scripts/git-credential-github.sh
fi
mkdir -p "$HOME/projects"
if [[ ! -d $project/.git ]]; then git clone -q "$repo" "$project"; fi
[[ $(git -C "$project" remote get-url origin) == "$repo" ]]
SAFETY_ENTRY=$(/usr/bin/env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin \
  /usr/bin/python3 -I -S /scripts/cc-safety-net-install.py | tail -n 1)
python3 /scripts/pi-setup.py write "$HOME" "$SAFETY_ENTRY"
[[ -s $HOME/.pi/agent/pi-cliproxyapi-provider/config.json ]]
python3 /scripts/isolated-config.py apply "$inputs" "$HOME" "$MCP_HUB_INSTANCE"
/scripts/pi.sh list | python3 /scripts/pi-setup.py check-packages "$HOME"
if [[ $MCP_HUB_INSTANCE == homelab ]]; then
  bash /scripts/ssh-config.sh
  bash /scripts/ssh-agent-ready.sh
fi
cat "$HOME/.codex/AGENTS.md" /scripts/homelab-ssh.md >"$HOME/.codex/AGENTS.override.md"
: >"$T3CODE_HOME/userdata/server-runtime.json"
(
  until curl -fs --max-time 5 -o /dev/null http://127.0.0.1:3773/health; do sleep 1; done
  printf '{"version":1,"pid":%s,"host":"0.0.0.0","port":3773,"origin":"http://127.0.0.1:3773","startedAt":"%s"}\n' \
    "$$" "$(date -u +%FT%T.000Z)" >"$T3CODE_HOME/userdata/server-runtime.json"
) &
rm -f "$T3CODE_HOME"/ssh-launch/*/pid "$T3CODE_HOME"/ssh-launch/*/port "$T3CODE_HOME"/ssh-launch/*/managed
exec t3 start --no-browser --host 0.0.0.0 --port 3773 --base-dir "$T3CODE_HOME" \
  --auto-bootstrap-project-from-cwd "$project"
