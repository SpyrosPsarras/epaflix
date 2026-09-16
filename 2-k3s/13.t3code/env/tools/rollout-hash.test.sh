#!/usr/bin/env bash
# Assert content changes roll pods and PVC retention survives pruning.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cp -r "$ROOT" "$tmp/k"
python3 "$ROOT/env/tools/private-config.test.py" overlay "$tmp/k"
render() { kustomize build "$1" | python3 -c '
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.stdin) if d]
sts = next(d for d in docs if d["kind"] == "StatefulSet")
refs = {v["name"]: v["configMap"]["name"] for v in sts["spec"]["template"]["spec"]["volumes"] if "configMap" in v}
cms = sorted(d["metadata"]["name"] for d in docs if d["kind"] == "ConfigMap")
assert sorted(refs.values()) == cms
assert sts["spec"]["persistentVolumeClaimRetentionPolicy"] == {"whenDeleted": "Retain", "whenScaled": "Retain"}
print(refs["lock"], refs["scripts"])'; }
base=$(render "$tmp/k"); read -r lock0 scripts0 <<<"$base"
[[ $lock0 == t3env-tools-* && $scripts0 == t3env-scripts-* ]]
printf '\n' >>"$tmp/k/env/tools/package-lock.json"
after=$(render "$tmp/k"); read -r lock1 scripts1 <<<"$after"
[[ $lock1 != "$lock0" && $scripts1 == "$scripts0" ]]
printf '\n' >>"$tmp/k/env/files/entrypoint.sh"
after=$(render "$tmp/k"); read -r lock2 scripts2 <<<"$after"
[[ $scripts2 != "$scripts1" && $lock2 == "$lock1" ]]
printf '\n' >>"$tmp/k/env/ingress.yaml"
[[ $(render "$tmp/k") == "$after" ]]
echo 'ok: tool/script updates roll pods; unrelated edits do not; PVCs retained'
before=$(kustomize build "$tmp/k" | python3 -c 'import yaml,sys;print(next(d for d in yaml.safe_load_all(sys.stdin) if d["kind"]=="StatefulSet")["spec"]["template"]["metadata"]["annotations"])')
sed -i 's/synthetic-v1/synthetic-v2/' "$tmp/k/synthetic-private.yaml"
after=$(kustomize build "$tmp/k" | python3 -c 'import yaml,sys;print(next(d for d in yaml.safe_load_all(sys.stdin) if d["kind"]=="StatefulSet")["spec"]["template"]["metadata"]["annotations"])')
[[ $before != "$after" ]]
echo 'ok: private configuration updates trigger rollout'
