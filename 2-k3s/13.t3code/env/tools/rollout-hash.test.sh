#!/usr/bin/env bash
# Assert content changes roll pods and PVC retention survives pruning.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/env/tools"
cp -r "$ROOT/one" "$tmp/k"
cp "$ROOT/env/Dockerfile" "$ROOT/env/Dockerfile.dockerignore" "$tmp/env/"
cp "$ROOT/env/tools/"{runtime-tag.sh,os-packages.txt,install-cluster-tools.sh,package.json,package-lock.json} "$tmp/env/tools/"
cp "$ROOT/versions.env" "$tmp/"
python3 "$ROOT/env/tools/private-config.test.py" overlay "$tmp/k"
render() { kustomize build "$1" | python3 -c '
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.stdin) if d]
sts = next(d for d in docs if d["kind"] == "StatefulSet")
refs = {v["name"]: v["configMap"]["name"] for v in sts["spec"]["template"]["spec"]["volumes"] if "configMap" in v}
rollout = next(d for d in docs if d["kind"] == "CronJob")["spec"]["jobTemplate"]["spec"]["template"]["spec"]
rollout_refs = [v["configMap"]["name"] for v in rollout["volumes"] if "configMap" in v]
cms = sorted(d["metadata"]["name"] for d in docs if d["kind"] == "ConfigMap")
assert sorted([*refs.values(), *rollout_refs]) == cms
assert sts["spec"]["persistentVolumeClaimRetentionPolicy"] == {"whenDeleted": "Retain", "whenScaled": "Retain"}
for job in (next(d for d in docs if d["kind"] == "Job")["spec"]["template"]["spec"], rollout):
    assert job["containers"][0]["image"] == sts["spec"]["template"]["spec"]["containers"][0]["image"]
    assert job["affinity"] == sts["spec"]["template"]["spec"]["affinity"]
print(refs["scripts"])'; }
scripts0=$(render "$tmp/k")
[[ $scripts0 == t3code-scripts-* ]]
tag0=$(bash "$tmp/env/tools/runtime-tag.sh")
printf '\n' >>"$tmp/env/tools/package-lock.json"
[[ $(bash "$tmp/env/tools/runtime-tag.sh") != "$tag0" ]]
[[ $(render "$tmp/k") == "$scripts0" ]]
printf '\n' >>"$tmp/k/files/entrypoint.sh"
after=$(render "$tmp/k")
[[ $after != "$scripts0" ]]
printf '\n' >>"$tmp/k/service.yaml"
[[ $(render "$tmp/k") == "$after" ]]
echo 'ok: tool/script updates roll pods; unrelated edits do not; PVCs retained; prepull matches pod image and node'
before=$(kustomize build "$tmp/k" | python3 -c 'import yaml,sys;print(next(d for d in yaml.safe_load_all(sys.stdin) if d["kind"]=="StatefulSet")["spec"]["template"]["metadata"]["annotations"])')
sed -i 's/synthetic-v1/synthetic-v2/' "$tmp/k/synthetic-private.yaml"
after=$(kustomize build "$tmp/k" | python3 -c 'import yaml,sys;print(next(d for d in yaml.safe_load_all(sys.stdin) if d["kind"]=="StatefulSet")["spec"]["template"]["metadata"]["annotations"])')
[[ $before != "$after" ]]
echo 'ok: private configuration updates trigger rollout'
sed -i 's/hub-v1/hub-v2/' "$tmp/k/synthetic-private.yaml"
rotated=$(kustomize build "$tmp/k" | python3 -c 'import yaml,sys;print(next(d for d in yaml.safe_load_all(sys.stdin) if d["kind"]=="StatefulSet")["spec"]["template"]["metadata"]["annotations"])')
[[ $rotated != "$after" && $rotated == *hub-v2* ]]
echo 'ok: MCP hub token rotation triggers rollout'
