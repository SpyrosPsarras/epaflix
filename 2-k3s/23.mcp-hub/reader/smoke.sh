#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
image=${READER_IMAGE:-vault-reader:dev}
if [[ ${SKIP_BUILD:-0} != 1 ]]; then
  docker build -f "$root/reader/Dockerfile" -t "$image" "$root"
fi
common=(--rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges
  --memory 1g --cpus 2 --pids-limit 128 --tmpfs '/tmp:rw,nosuid,nodev,exec,size=128m'
  --mount "type=bind,src=$root,dst=/fixture/23.mcp-hub,readonly"
  --mount "type=bind,src=$root/../13.t3code,dst=/fixture/13.t3code,readonly")
docker run "${common[@]}" -e HOME=/tmp -e TMPDIR=/tmp --entrypoint node "$image" /fixture/23.mcp-hub/tests/fixtures/reader_bridge_native.cjs
docker run "${common[@]}" -e HOME=/tmp -e TMPDIR=/tmp --entrypoint python "$image" -m unittest discover -s /fixture/23.mcp-hub/tests -p 'test_vault*.py' -v
docker run "${common[@]}" -e HOME=/tmp -e TMPDIR=/tmp --entrypoint python "$image" /fixture/23.mcp-hub/tests/test_reader_startup.py -v
inputs=$(mktemp -d)
cleanup() {
  docker run --rm --network none --user 0:0 --mount "type=bind,src=$inputs,dst=/inputs" \
    --entrypoint python "$image" -c 'import shutil; shutil.rmtree("/inputs/..generation", ignore_errors=True)'
  rm -rf "$inputs"
}
trap cleanup EXIT
chmod 0755 "$inputs"
docker run --rm --network none --user 0:0 --mount "type=bind,src=$inputs,dst=/inputs" \
  --mount "type=bind,src=$root/tests/fixtures/reader_package_smoke.py,dst=/smoke.py,readonly" \
  --entrypoint python "$image" /smoke.py provision /inputs
docker run "${common[@]}" --tmpfs /run/vault-reader:rw,nosuid,nodev,size=128m,uid=0,gid=1000,mode=0770 \
  --mount "type=bind,src=$inputs,dst=/run/vault-reader-inputs,readonly" \
  --entrypoint python "$image" /fixture/23.mcp-hub/tests/fixtures/reader_package_smoke.py
