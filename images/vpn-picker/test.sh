#!/usr/bin/env bash
# Verifies the image without touching AirVPN or the live cluster. No API key is
# passed anywhere in here, so no cycle ever reaches the real API.
set -euo pipefail
IMAGE="${1:-vpn-picker:dev}"

echo "== 1. ping resolves and answers with NO capabilities =="
# The scorer runs with `capabilities: {drop: ["ALL"]}`. iputils falls back to an
# unprivileged SOCK_DGRAM ICMP socket when it cannot get a raw one, and that
# only works because the container netns has net.ipv4.ping_group_range wide
# open. Assert both here so a base-image change that breaks the fallback fails
# the build instead of silently killing every probe in production.
out=$(docker run --rm --cap-drop ALL --entrypoint sh "$IMAGE" -c '
  grep CapEff /proc/self/status
  cat /proc/sys/net/ipv4/ping_group_range
  ping -n -q -c 3 -i 0.2 -W 2 127.0.0.1 2>&1' )
if ! echo "$out" | grep -q "CapEff:	0000000000000000"; then
  echo "FAIL: capabilities were not actually dropped"; echo "$out"; exit 1
fi
if ! echo "$out" | grep -qE "3 packets transmitted, 3 (packets )?received"; then
  echo "FAIL: ping did not work without capabilities"; echo "$out"; exit 1
fi
echo "ok"

echo "== 2. the app parses the ping output THIS image actually produces =="
# NB: `grep -q X && { ...; }` is wrong under `set -e` - when grep finds nothing
# it returns 1 and the script exits on the SUCCESS path. Use if/then for every
# "fail if found" check in this file.
if ! docker run --rm --cap-drop ALL --entrypoint sh "$IMAGE" -c '
  cd /app
  ping -n -q -c 5 -i 0.2 -W 2 127.0.0.1 > /tmp/p.txt 2>&1
  python3 -c "
import sys; sys.path.insert(0, \"/app\")
import vpn_picker
loss, rtt = vpn_picker.parse_ping(open(\"/tmp/p.txt\").read())
assert loss == 0.0, loss
assert rtt >= 0.0, rtt
print(\"parsed loss=%s rtt=%s\" % (loss, rtt))
"' | grep -q "parsed loss=0.0"; then
  echo "FAIL: parse_ping does not match this image's ping output format"; exit 1
fi
echo "ok"

echo "== 3. scoring rule, contract and atomic publish self-checks =="
# Covers the design's own sanity anchors: Dedalus beats Anser, Anser is rejected
# by the probe gate at 22% loss, Cygnus is excluded on health. Plus the atomic
# publish race and the API-outage behaviour.
docker run --rm --entrypoint python3 "$IMAGE" /app/selftest.py

echo "== 4. no API key means a clean exit 1, never a crash loop on a stack trace =="
set +e
out=$(docker run --rm -e AIRVPN_API_KEY= "$IMAGE" --once 2>&1); rc=$?
set -e
if [ "$rc" -ne 1 ]; then
  echo "FAIL: expected exit 1 with an empty key, got $rc"; echo "$out"; exit 1
fi
if echo "$out" | grep -q "Traceback"; then
  echo "FAIL: crashed instead of reporting the missing key"; echo "$out"; exit 1
fi
echo "ok"

echo "== 5. HTTP serves 503, not an empty list, before the first ranking =="
# Never serve an empty `servers` array - a consumer cannot tell that apart from
# "every candidate failed the gate".
cid=$(docker run -d --rm -p 18080:8080 -e AIRVPN_API_KEY=invalid \
        -e VPN_PICKER_API_URL=http://127.0.0.1:1/unreachable \
        -e VPN_PICKER_OUTPUT=/tmp/ranking.json "$IMAGE")
trap 'docker rm -f "$cid" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 30); do
  code=$(curl -s -o /tmp/body.txt -w '%{http_code}' http://127.0.0.1:18080/ranking.json || true)
  [ "$code" = "503" ] && break
  sleep 1
done
if [ "${code:-}" != "503" ]; then
  echo "FAIL: expected 503 before the first ranking, got '${code:-none}'"
  docker logs "$cid"; exit 1
fi
metrics=$(curl -s http://127.0.0.1:18080/metrics)
if ! echo "$metrics" | grep -q "^vpn_picker_scrape_success 0$"; then
  echo "FAIL: scrape_success does not report the failed cycle"; echo "$metrics"; exit 1
fi
for m in vpn_picker_ranking_generated_timestamp_seconds vpn_picker_candidates_passing_gate; do
  if ! echo "$metrics" | grep -q "^$m "; then
    echo "FAIL: missing metric $m"; echo "$metrics"; exit 1
  fi
done
echo "ok"

echo "ALL CHECKS PASSED"
