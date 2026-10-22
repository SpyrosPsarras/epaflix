#!/usr/bin/env bash
set -euo pipefail

T3_PROJECT_REPO=${T3_PROJECT_REPO:-https://github.com/SpyrosPsarras/epaflix.git}
PROJECT_DIR=${T3_PROJECT_DIR:-$HOME/projects/$(basename "$T3_PROJECT_REPO" .git)}
export PATH=/tools/node_modules/.bin:$PATH

mkdir -p "$(dirname "$PROJECT_DIR")"
if [[ ! -d $PROJECT_DIR/.git ]]; then
  git clone -q "$T3_PROJECT_REPO" "$PROJECT_DIR"
fi

python3 /scripts/manager-setup.py "$HOME" "$PROJECT_DIR" /scripts/manager-guidance.md /private-agent-config/bundle.json
bash /scripts/ssh-config.sh
python3 /scripts/ssh-policy.py "$HOME/.cc-safety-net/policy.json" \
  "$HOME" "$(getent passwd "$(id -u)" | cut -d: -f6)" /root

T3_HOME=$HOME/.t3
python3 /scripts/t3-pi-settings.py "$T3_HOME" "${ANTHROPIC_BASE_URL:-}"
SAFETY_ENTRY=$(/usr/bin/env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin \
  /usr/bin/python3 -I -S /scripts/cc-safety-net-install.py | tail -n 1)
python3 - "$HOME" "$SAFETY_ENTRY" <<'PY'
import importlib
from pathlib import Path
import sys
sys.path.insert(0, '/scripts')
importlib.import_module('pi-setup').write(Path(sys.argv[1]), sys.argv[2], ssh_path=Path('/dev/null'))
PY
/scripts/pi.sh list | python3 /scripts/pi-setup.py check-packages "$HOME"
echo "t3env: $(t3 --version) claude=$(claude --version 2>/dev/null | head -1) pi=$(/scripts/pi.sh --version 2>/dev/null | head -1) codex=$(codex --version 2>/dev/null | head -1)"

: >"$T3_HOME/userdata/server-runtime.json"
(
  until curl -fs --max-time 5 -o /dev/null http://127.0.0.1:3773/; do sleep 1; done
  printf '{"version":1,"pid":%s,"host":"0.0.0.0","port":3773,"origin":"http://127.0.0.1:3773","startedAt":"%s"}\n' \
    "$$" "$(date -u +%FT%T.000Z)" >"$T3_HOME/userdata/server-runtime.json" ||
    echo "t3env: server-runtime.json not written; ssh t3code stays down" >&2
) &
rm -f "$T3_HOME"/ssh-launch/*/pid "$T3_HOME"/ssh-launch/*/port "$T3_HOME"/ssh-launch/*/managed
exec t3 start --no-browser --host 0.0.0.0 --port 3773 --base-dir "$T3_HOME" \
  --auto-bootstrap-project-from-cwd "$PROJECT_DIR"
