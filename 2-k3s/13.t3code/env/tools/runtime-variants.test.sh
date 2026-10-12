#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/2-k3s"
cp -r "$root" "$tmp/2-k3s/13.t3code"
cp -r "$root/../13.t3code-homelab" "$tmp/2-k3s/13.t3code-homelab"
tools=$tmp/2-k3s/13.t3code/env/tools
mixed=$(bash "$tools/runtime-tag.sh")
homelab=$(bash "$tools/runtime-tag.sh" homelab)
[[ $mixed == "$(bash "$tools/runtime-tag.sh" mixed)" && $mixed != "$homelab" ]]
if bash "$tools/runtime-tag.sh" invalid; then exit 1; fi
for input in env/Dockerfile env/Dockerfile.dockerignore env/tools/os-packages.txt \
    env/tools/install-cluster-tools.sh env/tools/package.json env/tools/package-lock.json versions.env; do
  printf '\n' >>"$tmp/2-k3s/13.t3code/$input"
  next_mixed=$(bash "$tools/runtime-tag.sh" mixed)
  next_homelab=$(bash "$tools/runtime-tag.sh" homelab)
  [[ $next_mixed != "$mixed" && $next_homelab != "$homelab" ]]
  mixed=$next_mixed; homelab=$next_homelab
  bash "$tools/sync-runtime-image.sh"
  grep -q "t3-runtime:inputs-$mixed" "$tmp/2-k3s/13.t3code/one/statefulset.yaml"
  grep -q "t3-runtime:homelab-inputs-$homelab" "$tmp/2-k3s/13.t3code-homelab/statefulset.yaml"
done
bash "$tools/sync-runtime-image.sh"
echo 'ok: mixed default, variant separation, all image inputs and both generated references'
