#!/bin/sh
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
