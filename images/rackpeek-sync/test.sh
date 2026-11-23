#!/usr/bin/env bash
set -euo pipefail
IMAGE="${1:-rackpeek-sync:dev}"

echo "== 1. tools =="
docker run --rm --entrypoint sh "$IMAGE" -c 'ssh -V && git --version && gitleaks version && ssh -G -o BatchMode=yes localhost >/dev/null'

echo "== 2. unit tests in the image =="
docker run --rm --entrypoint python3 -w /tmp "$IMAGE" -m unittest discover -s /app -p 'test_*.py'

echo "== 3. gitleaks finds a planted secret =="
docker run --rm --entrypoint sh "$IMAGE" -c '
  mkdir -p /tmp/t && printf "token: ghp_%s\n" "$(head -c 18 /dev/urandom | od -An -tx1 | tr -d " \n")" > /tmp/t/x.conf
  gitleaks dir /tmp/t --no-banner --exit-code 0 --report-format json --report-path /tmp/r.json >/dev/null 2>&1
  grep -q x.conf /tmp/r.json'

echo "== 4. no env: exit 1, no traceback =="
set +e
out=$(docker run --rm "$IMAGE" 2>&1); rc=$?
set -e
if [ "$rc" -ne 1 ] || echo "$out" | grep -q Traceback; then
  echo "FAIL: rc=$rc"; echo "$out"; exit 1
fi

echo "ALL CHECKS PASSED"
