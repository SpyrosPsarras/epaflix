#!/bin/sh
set -eu

log() { echo "vpn-agent-entrypoint: $*"; }

RC=/root/.goldcrest.rc
if [ -n "${AIRVPN_USERNAME:-}" ] && [ -n "${AIRVPN_PASSWORD:-}" ]; then
  {
    echo ""
    echo "# --- appended by agent-entrypoint.sh, do not edit by hand ---"
    printf 'air-user              %s\n' "$AIRVPN_USERNAME"
    printf 'air-password          %s\n' "$AIRVPN_PASSWORD"
  } >> "$RC"
  chmod 0600 "$RC"
else
  log "no AIRVPN_USERNAME/AIRVPN_PASSWORD - the agent can read status but every"
  log "--air-connect will hit the credential prompt loop and time out"
fi

while true; do
  python3 -u /agent.py || log "agent exited rc=$? - restarting in ${VPN_AGENT_RESTART_DELAY:-60}s"
  sleep "${VPN_AGENT_RESTART_DELAY:-60}"
done
