#!/usr/bin/env bash
set -euo pipefail
IMAGE="${1:-airvpn-bluetit:dev}"

echo "== 1. both binaries resolve every library =="
if docker run --rm --entrypoint sh "$IMAGE" -c 'ldd /sbin/bluetit; ldd /usr/local/bin/goldcrest' \
     | grep -q "not found"; then
  echo "FAIL: missing shared library"; exit 1
fi
echo "ok"

echo "== 2. bluetit starts, logs to stdout, writes its lock file =="
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
docker run --rm --entrypoint python3 "$IMAGE" /agent_selftest.py

echo "== 6. the agent entrypoint runs and serves metrics with no credentials =="
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
         vpn_agent_tunnel_device_ok vpn_agent_switch_in_progress \
         vpn_agent_switch_budget_used vpn_agent_switch_budget_exhausted \
         vpn_agent_switch_cooldown_seconds_left; do
  if ! echo "${metrics:-}" | grep -q "$m"; then
    echo "FAIL: missing metric $m"; echo "${metrics:-none}"; docker logs "$cid"; exit 1
  fi
done
if ! echo "$metrics" | grep -q "^vpn_agent_tunnel_device_ok 0$"; then
  echo "FAIL: no tunnel device exists in this container and the metric does not say so"
  echo "$metrics" | grep tunnel_device; exit 1
fi
if ! echo "$metrics" | grep -q "^vpn_agent_switch_in_progress 0$"; then
  echo "FAIL: switch_in_progress is set with no switch running"; echo "$metrics"; exit 1
fi
if echo "$metrics" | grep -qE "^vpn_agent_tunnel_(bytes_total|throughput_bytes_per_sec) "; then
  echo "FAIL: a throughput series is published with no tunnel device present"
  echo "$metrics" | grep vpn_agent_tunnel; exit 1
fi
if echo "$metrics" | grep -q "vpn_agent_healthy_by_throughput_windows_total"; then
  echo "FAIL: vpn_agent_healthy_by_throughput_windows_total is still published"
  echo "$metrics" | grep healthy_by_throughput; exit 1
fi
if ! echo "$metrics" | grep -q "^vpn_agent_dry_run 0$"; then
  echo "FAIL: dry run is not off by default"; echo "$metrics"; exit 1
fi
sleep 20
if ! docker ps --format '{{.ID}}' | grep -q "${cid:0:12}"; then
  echo "FAIL: the agent container exited instead of staying up"; docker logs "$cid"; exit 1
fi
size=$(docker logs "$cid" 2>&1 | wc -c)
if [ "$size" -gt 2000000 ]; then
  echo "FAIL: agent log is ${size} bytes - a goldcrest call was not bounded"; exit 1
fi
docker rm -f "$cid" >/dev/null; trap - EXIT
echo "ok"

echo "== 7. a stale WireGuard tun<N> device does not wedge Bluetit =="
if ! docker run --rm --cap-add NET_ADMIN --entrypoint sh "$IMAGE" \
       -c 'ip link add tun0 type wireguard' >/dev/null 2>&1; then
  echo "SKIP: host kernel has no wireguard module, cannot build the stale-device fixture"
else
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
  if ! echo "$out" | grep -q "Enabling persistent network filter and lock"; then
    echo "FAIL: the persistent network lock was not armed after the cleanup"; echo "$out"; exit 1
  fi
  if ! echo "$out" | grep -q "REMAINING:wg0,"; then
    echo "FAIL: expected only wg0 to remain"; echo "$out" | grep REMAINING; exit 1
  fi
  echo "ok"
fi

echo "== 8. the kill -USR2 degradation watcher is gone, the supervisor is not (#611) =="
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
