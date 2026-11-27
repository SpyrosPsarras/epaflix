#!/usr/bin/env bash
set -euo pipefail
src="$(cd "$(dirname "$0")" && pwd)"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/img" "$tmp/api/repos/PrismML-Eng/llama.cpp/releases/tags"
cp "$src/sync-checksum.sh" "$tmp/img/"
printf 'ARG PRISM_RELEASE=prism-b99999-abc1234\nARG PRISM_SHA256=old\n' > "$tmp/img/Dockerfile"
want=$(printf 'f%.0s' $(seq 64))
cat > "$tmp/api/repos/PrismML-Eng/llama.cpp/releases/tags/prism-b99999-abc1234" <<EOF
{"assets": [
  {"name": "llama-prism-b99999-abc1234-bin-linux-cuda-13.3-x64.tar.gz", "digest": "sha256:$(printf '0%.0s' $(seq 64))"},
  {"name": "llama-prism-b99999-abc1234-bin-linux-cuda-12.8-x64.tar.gz", "digest": "sha256:$want"}
]}
EOF
LLAMA_PRISM_API="file://$tmp/api" bash "$tmp/img/sync-checksum.sh"
grep -qx "ARG PRISM_SHA256=$want" "$tmp/img/Dockerfile"
grep -qx "ARG PRISM_RELEASE=prism-b99999-abc1234" "$tmp/img/Dockerfile"
printf 'ARG PRISM_RELEASE=prism-b99999-abc1234\n' > "$tmp/img/Dockerfile"
if LLAMA_PRISM_API="file://$tmp/api" bash "$tmp/img/sync-checksum.sh" 2>/dev/null; then
  echo 'accepted a Dockerfile without PRISM_SHA256'; exit 1
fi
echo 'sync-checksum ok'
