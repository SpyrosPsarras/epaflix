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

# Only how often the kill -0 check below runs. The probe knobs that used to sit
# here (PROBE_TARGET/COUNT/LOSS_THRESHOLD/STRIKES/COOLDOWN) are gone with the
# watcher - setting them now does nothing.
SUPERVISE_INTERVAL=${SUPERVISE_INTERVAL:-60}

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

# Same class of leftover as the /run/dbus files above, one layer down. The
# WireGuard device lives in the POD netns, so it outlives this container, and
# #535 measured that nothing ever removes it: no kubelet path reaches bluetit
# with a signal it can act on, the cgroup is SIGKILLed (exitCode 137), so there
# is no graceful path at all. Next start, Bluetit finds it and refuses to work
# ("Cannot enable persistent network filter and lock" / "Cannot start AirVPN
# boot connection") while STAYING ALIVE with the lock unarmed and no tunnel.
# The probe then kills it and the next container inherits the same device. A
# container restart can never clear this - only a new pod, which is a new netns.
# Four occurrences on 2026-08-02, each one zero ready endpoints on
# svc/qbittorrent and the whole download path down.
#
# Measured against the real 2.1.0 binary, not assumed: tun0, tun1, tun9 and
# tun15 of type wireguard all trip it; a wireguard device named wg0 does not,
# and a non-wireguard device named tun0 does not. So match on type AND the
# tun<N> name - a wg* device would belong to something that is not us.
#
# NOT goldcrest --remove-wireguard-device, the option the error text names: it
# is a D-Bus call into a RUNNING Bluetit, which is exactly what we do not have
# yet. With the daemon down it prints "D-Bus service org.airvpn.server is not
# available", removes nothing, and still exits 0.
#
# Safe even if the device is still passing traffic. A WireGuard device is kernel
# state and keeps forwarding after its owner dies, but by this line the owner IS
# dead (fresh container, no bluetit in it), so the device is always an orphan.
# Removing it fails CLOSED: the persistent iptables lock is netns state and is
# not touched here, and qBittorrent is bound to tun0 so it opens no socket while
# the device is missing. Leaving it is what fails open - Bluetit then never arms
# the lock at all.
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

# `kill -0` sends no signal - it only asks whether the process is still there.
# It is the one `kill` this script is allowed to make, and the reason the whole
# file can be grepped for `USR2` as a regression check.
#
# Known bound, not a surprise: the check only runs at the top of each iteration,
# and each iteration starts with `sleep "$SUPERVISE_INTERVAL"` before the next
# one - so a Bluetit death right after a check is noticed up to
# SUPERVISE_INTERVAL (default 60s) later, not immediately.
while kill -0 "$BLUETIT_PID" 2>/dev/null; do
  sleep "$SUPERVISE_INTERVAL"
done

log "bluetit exited - failing so the container restarts"
exit 1
