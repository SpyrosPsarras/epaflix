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
git config --global user.name "Spyros Psarras"
git config --global user.email 13405649+SpyrosPsarras@users.noreply.github.com

# SSH client config for the agents; the sshd container runs it too.
bash /scripts/ssh-config.sh

# Homelab SSH keys from Vaultwarden (files/vault-ssh-agent.py). T3 and its
# agents get only the agent socket; the vault login stays in /run/t3-vaultwarden
# and bw's data dir in the memory-backed /run/t3-bw, both denied by
# cc-safety-net (ssh-policy.py). Keys load once per container start.
if [[ -s /run/t3-vaultwarden/password ]]; then
  if python3 /scripts/vault-ssh-agent.py /run/t3-vaultwarden /tmp/t3-ssh-agent/agent.sock /run/t3-bw; then
    export SSH_AUTH_SOCK=/tmp/t3-ssh-agent/agent.sock
  else
    echo "t3env: vault SSH keys not loaded; SSH falls back to key files" >&2
  fi
else
  echo "t3env: no Vaultwarden login in t3code-vaultwarden; vault SSH keys skipped" >&2
fi

# Same remote in both environments so T3 groups them as one project.
if [[ ! -d $PROJECT_DIR/.git ]]; then
  git clone -q "$T3_PROJECT_REPO" "$PROJECT_DIR"
fi

# Permit vault attachment names while protecting SSH files in both HOME locations.
python3 /scripts/ssh-policy.py "$HOME/.cc-safety-net/policy.json" \
  "$HOME" "$(getent passwd "$(id -u)" | cut -d: -f6)" /root

# MCP hub (2-k3s/23.mcp-hub): every agent MCP server behind one gateway and
# this pod's hub token. Register every hub path for Pi, Claude and Codex
# in the active home; the token stays an env reference, never a literal on the PVC.
# hub_clients.py also replaces the stdio keepass/searxng bridges and the
# hosted Notion entries the hub superseded, and drops retired hub servers
# (keepass). The claudeAgent instances run with
# CLAUDE_CONFIG_DIR=<home>/.claude (<home>/.claude/.claude.json); a plain
# `claude` in a shell reads <home>/.claude.json. Both get the servers.
if [[ -n ${MCP_HUB_TOKEN:-} && -n ${MCP_HUB_URL:-} ]]; then
  for h in "$HOME"; do
    [[ -d $h ]] || continue
    python3 /scripts/hub_clients.py pi "$h/.pi/agent/mcp.json" "$MCP_HUB_URL" MCP_HUB_TOKEN || \
      echo "t3env: hub pi config failed in $h" >&2
    for cfg in "$h/.claude/.claude.json" "$h/.claude.json"; do
      python3 /scripts/hub_clients.py claude "$cfg" "$MCP_HUB_URL" 'Bearer ${MCP_HUB_TOKEN}' || \
        echo "t3env: hub claude config failed for $cfg" >&2
    done
    python3 /scripts/hub_clients.py codex "$h/.codex" "$MCP_HUB_URL" MCP_HUB_TOKEN || \
      echo "t3env: hub codex config failed in $h" >&2
  done
fi

T3_HOME=$HOME/.t3
python3 /scripts/t3-pi-settings.py "$T3_HOME" "${ANTHROPIC_BASE_URL:-}"

echo "t3env: $(t3 --version) claude=$(claude --version 2>/dev/null | head -1) pi=$(/scripts/pi.sh --version 2>/dev/null | head -1) codex=$(codex --version 2>/dev/null | head -1)"
python3 /scripts/private-config.py install /private-agent-config/bundle.json
SAFETY_ENTRY=$(/usr/bin/env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin \
  /usr/bin/python3 -I -S /scripts/cc-safety-net-install.py | tail -n 1)
python3 /scripts/pi-setup.py write "$HOME" "$SAFETY_ENTRY"
/scripts/pi.sh list | python3 /scripts/pi-setup.py check-packages "$HOME"
# Codex has no extra-instructions list: it reads ~/.codex/AGENTS.override.md
# instead of AGENTS.md when present, so give it the bundle plus the homelab SSH how-to.
# Written whole or not at all: a partial override would hide the bundle.
if ! { cat "$HOME/.codex/AGENTS.md" /scripts/homelab-ssh.md >"$HOME/.codex/AGENTS.override.md.tmp" &&
        mv -f "$HOME/.codex/AGENTS.override.md.tmp" "$HOME/.codex/AGENTS.override.md"; } 2>/dev/null; then
  rm -f "$HOME/.codex/AGENTS.override.md" "$HOME/.codex/AGENTS.override.md.tmp" || true
  echo "t3env: codex homelab SSH instructions not written; Codex uses the bundle alone" >&2
fi
# The T3 app's SSH launcher reuses the server named in server-runtime.json and
# otherwise starts its own on the next free port, whose tunnel PermitOpen
# refuses. T3 servers delete that file when they stop and `t3 project` deletes
# it when a call fails, so it is a file mount (statefulset.yaml): unlink and
# rename fail there, and this in-place write names this server ($$ survives the
# exec below) for the container's lifetime. T3 refuses to start while the file
# names a live process, so it stays empty until T3 answers; sshd waits for it.
: >"$T3_HOME/userdata/server-runtime.json"
(
  until curl -fs --max-time 5 -o /dev/null http://127.0.0.1:3773/; do sleep 1; done
  printf '{"version":1,"pid":%s,"host":"0.0.0.0","port":3773,"origin":"http://127.0.0.1:3773","startedAt":"%s"}\n' \
    "$$" "$(date -u +%FT%T.000Z)" >"$T3_HOME/userdata/server-runtime.json" ||
    echo "t3env: server-runtime.json not written; ssh t3code stays down" >&2
) &
# The launcher's pid files outlive the container; a stale pid would make it
# kill whichever process now has that number.
rm -f "$T3_HOME"/ssh-launch/*/pid "$T3_HOME"/ssh-launch/*/port "$T3_HOME"/ssh-launch/*/managed
# `serve` forces project bootstrap off; `start --no-browser` honors the flag.
exec t3 start --no-browser --host 0.0.0.0 --port 3773 --base-dir "$T3_HOME" \
  --auto-bootstrap-project-from-cwd "$PROJECT_DIR"
