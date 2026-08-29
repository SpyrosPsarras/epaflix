#!/bin/sh
# AirVPN Bluetit sidecar entrypoint.
#
# Bluetit forks and returns 0, so this script has to hold the foreground and
# watch the daemon. Watching the daemon is now ALL it does.
#
# It used to also run a degradation probe that recovered with `kill -USR2`.
# REMOVED (#611, root-caused in #627). Two reasons, either one sufficient:
#
# 1. SIGUSR2 runs Bluetit's own internal reconnect, which rebuilds tun0 from the
#    CACHED PROFILE without re-logging in to AirVPN. Once the peer is gone
#    server-side that path retries the same dead endpoint forever - measured
#    2026-08-02, four reconnect cycles over a network path verified clean, zero
#    handshakes, five minutes after the fault had been removed. It is not a
#    weak recovery, it is one that structurally cannot recover a lost handshake.
#    Only a fresh `goldcrest --disconnect` + `--air-connect` re-authenticates,
#    and that recovered the same tunnel in under one second.
# 2. With the vpn-picker agent (#608) in the pod this was a SECOND watcher on
#    the same signal with the same 3-strike/5% bar, so both trip inside the same
#    ~3 minutes and the USR2 can land in the middle of the agent's
#    disconnect/connect pair.
#
# Degradation response now belongs to the agent (#627 fix 4), with the
# traffic-based kubelet liveness probe (#629) as the backstop under it.
set -eu

SUPERVISE_INTERVAL=${SUPERVISE_INTERVAL:-60}

log() { echo "airvpn-bluetit: $*"; }

/entrypoint-render.sh

mkdir -p /run/dbus
rm -f /run/dbus/pid /run/dbus/system_bus_socket
dbus-daemon --system --fork

busybox syslogd -n -O /dev/stdout &

rm -f /etc/airvpn/bluetit.lock

for dev in $(ip -o link show type wireguard 2>/dev/null | awk -F'[:@ ]+' '{print $2}'); do
  case "$dev" in
    tun[0-9]*)
      log "removing stale WireGuard device ${dev} left behind by the previous run"
      ip link del "$dev"
      ;;
  esac
done

/sbin/bluetit

i=0
while [ ! -f /etc/airvpn/bluetit.lock ]; do
  i=$((i + 1))
  if [ "$i" -gt 30 ]; then
    log "bluetit did not create its lock file within 30s - giving up"
    exit 1
  fi
  sleep 1
done

BLUETIT_PID=$(cat /etc/airvpn/bluetit.lock)
log "bluetit running pid=${BLUETIT_PID}"

while kill -0 "$BLUETIT_PID" 2>/dev/null; do
  sleep "$SUPERVISE_INTERVAL"
done

log "bluetit exited - failing so the container restarts"
exit 1
