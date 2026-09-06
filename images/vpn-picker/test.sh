#!/usr/bin/env bash
set -euo pipefail
IMAGE="${1:-vpn-picker:dev}"

echo "== 1. ping resolves and answers with NO capabilities =="
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
for m in vpn_picker_ranking_generated_timestamp_seconds vpn_picker_candidates_passing_gate \
         vpn_picker_active_agent_verdicts vpn_picker_servers_ejected; do
  if ! echo "$metrics" | grep -q "^$m "; then
    echo "FAIL: missing metric $m"; echo "$metrics"; exit 1
  fi
done
echo "ok"

echo "== 6. POST /verdict intake: bearer-token gated, persists under verdict_dir =="
docker rm -f "$cid" >/dev/null 2>&1 || true
verdict_dir=$(mktemp -d)
ranking_path=$(mktemp -u)
cid=$(docker run -d --rm -p 18080:8080 -e AIRVPN_API_KEY=invalid \
        -e VPN_PICKER_API_URL=http://127.0.0.1:1/unreachable \
        -e VPN_PICKER_OUTPUT="$ranking_path" \
        -e VPN_PICKER_VERDICT_DIR="$verdict_dir" \
        -e VPN_PICKER_INTAKE_TOKEN=test-token \
        -v "$verdict_dir:$verdict_dir" "$IMAGE")
trap 'docker rm -f "$cid" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18080/healthz || true)
  [ "$code" = "200" ] && break
  sleep 1
done
stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
body="{\"schema\":1,\"source\":\"vpn-agent\",\"producer\":\"nick\",\"server\":\"Piautos\",\"observed_at\":\"$stamp\",\"ttl_seconds\":21600,\"loss_pct\":8.0,\"bad_windows\":3}"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer wrong' \
  --data "$body" http://127.0.0.1:18080/verdict)
if [ "$code" != "401" ]; then
  echo "FAIL: wrong token must be 401, got $code"; docker logs "$cid"; exit 1
fi
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' \
  --data "$body" http://127.0.0.1:18080/verdict)
if [ "$code" != "401" ]; then
  echo "FAIL: missing token must be 401, got $code"; docker logs "$cid"; exit 1
fi
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer test-token' \
  --data "$body" http://127.0.0.1:18080/verdict)
if [ "$code" != "202" ]; then
  echo "FAIL: right token must be 202, got $code"; docker logs "$cid"; exit 1
fi
sleep 1
if [ "$(ls "$verdict_dir" | wc -l)" -ne 1 ]; then
  echo "FAIL: intake did not write exactly one file under verdict_dir"; ls "$verdict_dir"; exit 1
fi
written=$(cat "$verdict_dir"/*.json)
if ! echo "$written" | grep -q '"producer": "nick"' \
   || ! echo "$written" | grep -q '"server": "Piautos"' \
   || ! echo "$written" | grep -q '"source": "vpn-agent"'; then
  echo "FAIL: written verdict does not match the accepted document"; echo "$written"; exit 1
fi
docker rm -f "$cid" >/dev/null 2>&1 || true
rm -rf "$verdict_dir"
echo "ok"

echo "ALL CHECKS PASSED"
