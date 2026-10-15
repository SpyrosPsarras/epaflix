#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/2-k3s"
for name in 13.t3code 13.t3code-homelab 13.t3code-work; do
  cp -r "$root/../$name" "$tmp/2-k3s/$name"
done
tools=$tmp/2-k3s/13.t3code/env/tools
mixed=$(bash "$tools/runtime-tag.sh")
homelab=$(bash "$tools/runtime-tag.sh" homelab)
work=$(bash "$tools/runtime-tag.sh" work)
[[ $mixed == "$(bash "$tools/runtime-tag.sh" mixed)" && $mixed != "$homelab" && $work != "$mixed" && $work != "$homelab" ]]
if bash "$tools/runtime-tag.sh" invalid; then exit 1; fi
for input in env/Dockerfile env/Dockerfile.dockerignore env/tools/os-packages.txt \
    env/tools/install-cluster-tools.sh env/tools/package.json env/tools/package-lock.json \
    env/files/vault-client.py versions.env; do
  printf '\n' >>"$tmp/2-k3s/13.t3code/$input"
  next_mixed=$(bash "$tools/runtime-tag.sh" mixed)
  next_homelab=$(bash "$tools/runtime-tag.sh" homelab)
  next_work=$(bash "$tools/runtime-tag.sh" work)
  [[ $next_mixed != "$mixed" && $next_homelab != "$homelab" && $next_work != "$work" ]]
  mixed=$next_mixed; homelab=$next_homelab; work=$next_work
  bash "$tools/sync-runtime-image.sh"
  grep -q "t3-runtime:inputs-$mixed" "$tmp/2-k3s/13.t3code/one/statefulset.yaml"
  grep -q "t3-runtime:homelab-inputs-$homelab" "$tmp/2-k3s/13.t3code-homelab/statefulset.yaml"
  grep -q "t3-runtime:work-inputs-$work" "$tmp/2-k3s/13.t3code-work/statefulset.yaml"
done
bash "$tools/sync-runtime-image.sh"
echo 'ok: mixed default, three variant tags, all public image inputs and generated references'
