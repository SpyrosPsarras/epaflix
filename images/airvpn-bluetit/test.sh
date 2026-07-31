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
echo "ALL CHECKS PASSED"
