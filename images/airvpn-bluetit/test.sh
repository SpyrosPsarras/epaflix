#!/usr/bin/env bash
# Verifies the image without connecting to AirVPN (no credentials => airconnectatboot
# never fires, so the live tunnel is untouched).
set -euo pipefail
IMAGE="${1:-airvpn-bluetit:dev}"

echo "== 1. both binaries resolve every library =="
# NB: `grep -q X && { ...; }` is wrong under `set -e` - when grep finds nothing it
# returns 1, the && list returns 1, and the script exits on the SUCCESS path.
# Use if/then for every "fail if found" check in this file.
#
# NB2: the Dockerfile sets ENTRYPOINT ["/entrypoint.sh"] with no CMD. Without
# `--entrypoint sh`, `docker run image sh -c '...'` does NOT replace the
# entrypoint - "sh -c '...'" is appended as ARGS to /entrypoint.sh, which
# ignores them, so the real entrypoint runs instead and the intended command
# never executes (verified: `docker ps` showed the container's command as
# literally "/entrypoint.sh sh -c ..."). Every check below overrides
# --entrypoint sh for this reason.
if docker run --rm --entrypoint sh "$IMAGE" -c 'ldd /sbin/bluetit; ldd /usr/local/bin/goldcrest' \
     | grep -q "not found"; then
  echo "FAIL: missing shared library"; exit 1
fi
echo "ok"

echo "== 2. bluetit starts, logs to stdout, writes its lock file =="
# No /config mount here on purpose: without the fragment there is no
# `airconnectatboot quick`, so Bluetit never dials out and the live tunnel
# (which uses the same `Default` key) is not disturbed.
out=$(docker run --rm --entrypoint sh --cap-add NET_ADMIN -e AIRVPN_USERNAME= -e AIRVPN_PASSWORD= "$IMAGE" \
  -c '/entrypoint.sh & sleep 12; cat /etc/airvpn/bluetit.lock 2>/dev/null || echo NOLOCK' 2>&1)
if ! echo "$out" | grep -q "Bluetit successfully initialized and ready"; then
  echo "FAIL: no init line on stdout (syslog forwarder broken?)"; echo "$out"; exit 1
fi
if echo "$out" | grep -q NOLOCK; then
  echo "FAIL: no lock file"; echo "$out"; exit 1
fi
echo "ok"

echo "== 3. our directives landed in bluetit.rc, shipped ones survived =="
docker run --rm --entrypoint sh -e AIRVPN_USERNAME=u -e AIRVPN_PASSWORD=p \
  -v "$PWD/testdata:/config:ro" "$IMAGE" -c '
    . /entrypoint-render.sh
    grep -q "^rsamodulus" /etc/airvpn/bluetit.rc || { echo "FAIL: shipped rsamodulus lost"; exit 1; }
    grep -q "^bootserver" /etc/airvpn/bluetit.rc || { echo "FAIL: shipped bootserver lost"; exit 1; }
    grep -q "^airvpntype                  wireguard" /etc/airvpn/bluetit.rc || { echo "FAIL: fragment not appended"; exit 1; }
    grep -q "^airusername                 u" /etc/airvpn/bluetit.rc || { echo "FAIL: username not substituted"; exit 1; }
    echo ok'
echo "== 4. stale /run/dbus/pid + socket from a previous restart do not wedge dbus-daemon (#524) =="
# Simulates the #509 scratch-pod bug: /run/dbus survives a container restart
# (a shared emptyDir, per the #498/#501 agent-sidecar design), so a stale pid
# file and socket from the daemon's previous life are already present when
# entrypoint.sh starts. dbus-daemon refuses to start with a pre-existing pid
# file (it never checks if that pid is alive) - reproduced twice on the
# scratch pod. Without cleanup, the whole script aborts under set -eu before
# bluetit ever starts, and the container CrashLoopBackOffs forever.
out=$(docker run --rm --entrypoint sh --cap-add NET_ADMIN -e AIRVPN_USERNAME= -e AIRVPN_PASSWORD= "$IMAGE" \
  -c 'mkdir -p /run/dbus && echo 99999 > /run/dbus/pid && : > /run/dbus/system_bus_socket
      /entrypoint.sh & sleep 12; cat /etc/airvpn/bluetit.lock 2>/dev/null || echo NOLOCK' 2>&1)
if ! echo "$out" | grep -q "Bluetit successfully initialized and ready"; then
  echo "FAIL: no init line on stdout with stale /run/dbus present"; echo "$out"; exit 1
fi
if echo "$out" | grep -q NOLOCK; then
  echo "FAIL: no lock file with stale /run/dbus present"; echo "$out"; exit 1
fi
echo "ok"

echo "== 5. the agent's decision logic (#608, spec Components 4-7) =="
# Runs against the real agent.py in the built image, not a copy. Covers the
# stale-ranking rule, the top-5 band test, degradation hysteresis, the cooldown
# and daily cap, the failed-connect walk, and the goldcrest byte cap.
docker run --rm --entrypoint python3 "$IMAGE" /agent_selftest.py

echo "== 6. the agent entrypoint runs and serves metrics with no credentials =="
# No AIRVPN_USERNAME/AIRVPN_PASSWORD and no Bluetit to talk to. It must stay up
# and keep serving, not exit - a crash-looping agent container makes the whole
# pod not-ready, which drops qbittorrent out of its Service endpoints.
cid=$(docker run -d --rm -p 18081:8081 --entrypoint /agent-entrypoint.sh \
        -e VPN_AGENT_GOLDCREST_TIMEOUT=3 -e VPN_AGENT_BOOT_WAIT_SECONDS=5 \
        -e VPN_AGENT_PROBE_INTERVAL_SECONDS=5 "$IMAGE")
trap 'docker rm -f "$cid" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 30); do
  metrics=$(curl -s http://127.0.0.1:18081/metrics || true)
  echo "$metrics" | grep -q "^vpn_agent_dry_run " && break
  sleep 1
done
for m in vpn_agent_dry_run vpn_agent_switches_total vpn_agent_consecutive_bad_windows \
         vpn_agent_tunnel_device_ok vpn_agent_switch_in_progress; do
  if ! echo "${metrics:-}" | grep -q "$m"; then
    echo "FAIL: missing metric $m"; echo "${metrics:-none}"; docker logs "$cid"; exit 1
  fi
done
# There is no tunnel in this container and no switch has run, so the honest
# answer is 0 - and this is the #690 defect end to end: the metric used to be a
# field initialised True and written only on the switch path, so it printed 1
# here, on a container that has never had a tun0 at all.
if ! echo "$metrics" | grep -q "^vpn_agent_tunnel_device_ok 0$"; then
  echo "FAIL: no tunnel device exists in this container and the metric does not say so"
  echo "$metrics" | grep tunnel_device; exit 1
fi
if ! echo "$metrics" | grep -q "^vpn_agent_switch_in_progress 0$"; then
  echo "FAIL: switch_in_progress is set with no switch running"; echo "$metrics"; exit 1
fi
# Same rule for the throughput series: there is no tun0 in this container, so the
# honest answer is NO SERIES, not a zero (#686/#690). These are diagnostics only
# since #771 - a fabricated zero would still be a lie about a device that is not
# there.
if echo "$metrics" | grep -qE "^vpn_agent_tunnel_(bytes_total|throughput_bytes_per_sec) "; then
  echo "FAIL: a throughput series is published with no tunnel device present"
  echo "$metrics" | grep vpn_agent_tunnel; exit 1
fi
# The gate's counter is DELETED, not parked at 0 (#771). Nothing increments it any
# more, and a metric no code path writes reads as coverage while being none.
if echo "$metrics" | grep -q "vpn_agent_healthy_by_throughput_windows_total"; then
  echo "FAIL: vpn_agent_healthy_by_throughput_windows_total is still published"
  echo "$metrics" | grep healthy_by_throughput; exit 1
fi
# DRY_RUN defaults OFF - the agent acts. The flag only exists because the spec's
# rollback is "flip back to dry-run".
if ! echo "$metrics" | grep -q "^vpn_agent_dry_run 0$"; then
  echo "FAIL: dry run is not off by default"; echo "$metrics"; exit 1
fi
sleep 20
if ! docker ps --format '{{.ID}}' | grep -q "${cid:0:12}"; then
  echo "FAIL: the agent container exited instead of staying up"; docker logs "$cid"; exit 1
fi
# The credential-less goldcrest calls must not have flooded the log with the
# 673 MB prompt. 2 MB is generous and still catches an unbounded call.
size=$(docker logs "$cid" 2>&1 | wc -c)
if [ "$size" -gt 2000000 ]; then
  echo "FAIL: agent log is ${size} bytes - a goldcrest call was not bounded"; exit 1
fi
docker rm -f "$cid" >/dev/null; trap - EXIT
echo "ok"

echo "== 7. a stale WireGuard tun<N> device does not wedge Bluetit =="
# The device lives in the POD netns, so it survives an `airvpn` container
# restart, and #535 proved there is no graceful exit path that would remove it
# (SIGKILL, exitCode 137). Bluetit then refuses to arm the persistent lock and
# refuses the boot connection, forever, until the POD is deleted. Four occurrences
# on 2026-08-02. The fixture below is the exact leftover state.
#
# Needs the wireguard module on the host kernel to build the fixture at all.
# Skip rather than fail if it is missing - a false red on every image build is
# worse than one uncovered check, and the fix is verified by hand on a host that
# has the module.
if ! docker run --rm --cap-add NET_ADMIN --entrypoint sh "$IMAGE" \
       -c 'ip link add tun0 type wireguard' >/dev/null 2>&1; then
  echo "SKIP: host kernel has no wireguard module, cannot build the stale-device fixture"
else
  # testdata/bluetit.conf carries `networklock`, which arms only during a
  # connection. The production pod uses `networklockpersist`, which arms at boot
  # and is the directive the stale device actually breaks - so render that shape
  # here instead of touching the fixture the other checks depend on.
  out=$(docker run --rm --cap-add NET_ADMIN \
    -e AIRVPN_USERNAME= -e AIRVPN_PASSWORD= -e BLUETIT_CONFIG=/tmp/lock.conf \
    --entrypoint sh "$IMAGE" -c '
      printf "airvpntype wireguard\nnetworklockpersist iptables\nallowprivatenetwork yes\nallowping yes\nairipv6 no\nignorednspush yes\nairkey Default\n" > /tmp/lock.conf
      ip link add tun0 type wireguard
      ip link add tun1 type wireguard
      ip link add wg0  type wireguard
      /entrypoint.sh & sleep 12
      echo "REMAINING:$(ip -o link show type wireguard | awk -F"[:@ ]+" "{print \$2}" | tr "\n" ",")"' 2>&1)
  if echo "$out" | grep -q "An existing WireGuard tunnel device has been found"; then
    echo "FAIL: Bluetit still saw a stale WireGuard device"; echo "$out"; exit 1
  fi
  # "Enabling ..." means Bluetit got PAST the stale-device check and tried, which
  # is the whole regression. It is deliberately not an assertion that the rules
  # installed - on a host whose iptables is nft-only the legacy tables are
  # missing and the install fails after this line, which is a property of the
  # test host, not of the image.
  if ! echo "$out" | grep -q "Enabling persistent network filter and lock"; then
    echo "FAIL: the persistent network lock was not armed after the cleanup"; echo "$out"; exit 1
  fi
  # wg0 must survive: it is not a tun<N>, so it is not ours to delete.
  if ! echo "$out" | grep -q "REMAINING:wg0,"; then
    echo "FAIL: expected only wg0 to remain"; echo "$out" | grep REMAINING; exit 1
  fi
  echo "ok"
fi

echo "== 8. the kill -USR2 degradation watcher is gone, the supervisor is not (#611) =="
# The watcher recovered with `kill -USR2`, which runs Bluetit's INTERNAL
# reconnect: it rebuilds tun0 from the cached profile without re-logging in to
# AirVPN, so once the peer is gone server-side it retries the same dead endpoint
# forever (measured 2026-08-02: four cycles over a verified-clean path, zero
# handshakes). It was also a second watcher racing the agent's own switch.
# Removed in #611. Only `kill -0` may remain - that sends no signal, it just
# asks whether bluetit is alive, and it is what fails the container when it is
# not. Both halves are checked here, because deleting the loop and deleting the
# supervisor look almost identical in a diff.
# Comment lines are stripped first: the header comment above the supervisor
# explains WHY there is no USR2 path and must survive, or the next person
# re-adds the loop.
if docker run --rm --entrypoint sh "$IMAGE" \
     -c 'grep -vE "^[[:space:]]*#" /entrypoint.sh | grep -nE "kill[[:space:]]+-[^0[:space:]]"'; then
  echo "FAIL: entrypoint.sh signals bluetit again - only kill -0 is allowed"; exit 1
fi
out=$(docker run --rm --entrypoint sh --cap-add NET_ADMIN \
  -e AIRVPN_USERNAME= -e AIRVPN_PASSWORD= -e SUPERVISE_INTERVAL=2 "$IMAGE" -c '
    { /entrypoint.sh; echo "ENTRYPOINT_EXIT=$?"; } &
    sleep 12
    kill -9 "$(cat /etc/airvpn/bluetit.lock)"
    sleep 8' 2>&1)
if ! echo "$out" | grep -q "ENTRYPOINT_EXIT=1"; then
  echo "FAIL: bluetit died and the supervisor did not fail the container with it"
  echo "$out"; exit 1
fi
echo "ok"

echo "ALL CHECKS PASSED"
