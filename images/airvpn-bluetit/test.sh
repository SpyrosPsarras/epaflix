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
for m in vpn_agent_dry_run vpn_agent_switches_total vpn_agent_consecutive_bad_windows; do
  if ! echo "${metrics:-}" | grep -q "$m"; then
    echo "FAIL: missing metric $m"; echo "${metrics:-none}"; docker logs "$cid"; exit 1
  fi
done
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

echo "ALL CHECKS PASSED"
