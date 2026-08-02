#!/bin/sh
# Second entrypoint of this image - the vpn-picker agent (#608, spec Components
# 4-7). The default entrypoint runs Bluetit; this one runs the sidecar that
# drives it. Same image because goldcrest and the D-Bus client libraries are
# already here.
#
# Two jobs, both of them small: put the AirVPN credentials somewhere goldcrest
# will read them, then keep agent.py running.
set -eu

log() { echo "vpn-agent-entrypoint: $*"; }

# goldcrest needs credentials for --air-connect (check_airvpn_credentials()).
# Without them it does not fail - it prompts `AirVPN Username: ` forever and
# emits 673 MB in 20 s with no newline in it.
#
# The rc file, not --air-user/--air-password on the command line: this container
# shares a pod with qBittorrent, and a command line is world-readable in /proc.
# goldcrest reads /root/.goldcrest.rc on every call and takes air-user /
# air-password from it. The shipped copy is all comments, so appending is safe,
# and the container filesystem is fresh on every start so this cannot double up.
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

# Supervise rather than let the container exit. A crash-looping container makes
# the whole POD not-ready, and a not-ready qbittorrent pod is dropped from its
# Service endpoints - the WebUI goes dark and every *arr loses its download
# client. An agent bug must never cost that. agent.py already guards its own
# loop; this is the backstop for the failures it cannot catch.
while true; do
  python3 -u /agent.py || log "agent exited rc=$? - restarting in ${VPN_AGENT_RESTART_DELAY:-60}s"
  sleep "${VPN_AGENT_RESTART_DELAY:-60}"
done
