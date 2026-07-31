#!/bin/sh
# AirVPN Bluetit sidecar entrypoint.
#
# Bluetit forks and returns 0, so this script has to hold the foreground and
# watch the daemon. It also runs the degradation probe: a server that stays up
# but drops packets is invisible to both the network lock and a restart, and
# that is exactly the failure that caused this work (9% loss on the entry IP,
# 60% inside the tunnel, upload down to 0.03 MB/s).
set -eu

PROBE_TARGET=${PROBE_TARGET:-10.128.0.1}
PROBE_INTERVAL=${PROBE_INTERVAL:-60}
PROBE_COUNT=${PROBE_COUNT:-20}
PROBE_LOSS_THRESHOLD=${PROBE_LOSS_THRESHOLD:-5}
PROBE_STRIKES=${PROBE_STRIKES:-3}
PROBE_COOLDOWN=${PROBE_COOLDOWN:-900}

log() { echo "airvpn-bluetit: $*"; }

/entrypoint-render.sh

mkdir -p /run/dbus
# A previous life of this container can leave /run/dbus/pid and
# /run/dbus/system_bus_socket behind when /run/dbus is a shared volume (the
# #498/#501 agent-sidecar design) - dbus-daemon refuses to start with a stale
# pid file present (it never checks whether that pid is actually alive), so
# under set -eu the script aborts before bluetit even starts and the
# container CrashLoopBackOffs forever. Found by the #509 scratch-pod
# prototype, reproduced twice. Same failure mode as the bluetit.lock cleanup
# below, just a different leftover file.
rm -f /run/dbus/pid /run/dbus/system_bus_socket
dbus-daemon --system --fork

# Bluetit logs ONLY to syslog - there is no logfile directive. Without this the
# container emits nothing and a healthy daemon looks like a silent crash.
busybox syslogd -n -O /dev/stdout &

rm -f /etc/airvpn/bluetit.lock
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

# ponytail: monotonic clock via /proc/uptime instead of pulling in date maths.
now_secs() { cut -d. -f1 /proc/uptime; }

strikes=0
last_reconnect=0

# Known bound, not a surprise: kill -0 is only re-checked at the top of each
# iteration, and each iteration starts with `sleep "$PROBE_INTERVAL"` before
# the next check runs - so a Bluetit death right after a check is noticed up
# to PROBE_INTERVAL (default 60s) later, not immediately.
while kill -0 "$BLUETIT_PID" 2>/dev/null; do
  sleep "$PROBE_INTERVAL"

  loss=$(ping -c "$PROBE_COUNT" -i 0.2 -W 2 "$PROBE_TARGET" 2>/dev/null \
         | sed -n 's/.*[^0-9]\([0-9][0-9]*\)% packet loss.*/\1/p' | tail -1)
  [ -n "${loss:-}" ] || loss=100

  if [ "$loss" -gt "$PROBE_LOSS_THRESHOLD" ]; then
    strikes=$((strikes + 1))
    log "probe loss=${loss}% strike=${strikes}/${PROBE_STRIKES}"
  else
    strikes=0
  fi

  if [ "$strikes" -ge "$PROBE_STRIKES" ]; then
    now=$(now_secs)
    if [ $((now - last_reconnect)) -ge "$PROBE_COOLDOWN" ]; then
      # This exact string is what the Loki alert matches - keep them in step.
      log "reconnect triggered, loss=${loss}%"
      kill -USR2 "$BLUETIT_PID"
      last_reconnect=$now
    else
      log "reconnect suppressed by cooldown, loss=${loss}%"
    fi
    strikes=0
  fi
done

log "bluetit exited - failing so the container restarts"
exit 1
