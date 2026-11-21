#!/usr/bin/env bash
# Guards the Git-owned tracker-rule table and its wiring into Cleanuparr's
# boot-time seeding guard and the hourly cleanuparr-policy reconcile.
# No cluster is touched.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
servarr="$here/../08.servarr"
rules="$servarr/cleanuparr/files/tracker-rules.json"
guard="$servarr/cleanuparr/files/enforce-private-seeding.py"
kustomization="$servarr/kustomization.yaml"
deployment="$servarr/cleanuparr/cleanuparr.yaml"
policy="$here/files/cleanuparr-policy.py"
helpers="$here/files/tracker_rules.py"
cronjob="$here/cleanuparr-policy-cronjob.yaml"
hnr_check="$here/files/torrent-hnr-check.py"
hnr_cronjob="$here/torrent-hnr-check-cronjob.yaml"

fail() { echo "FAIL: $*" >&2; exit 1; }

python3 -m json.tool "$rules" >/dev/null || fail "tracker-rules.json is not valid JSON"

TRACKER_RULES="$rules" python3 "$guard" --selftest | grep -qx 'selftest OK' \
    || fail "enforce-private-seeding.py --selftest failed"

grep -q 'tracker-rules.json=cleanuparr/files/tracker-rules.json' "$kustomization" \
    || fail "kustomization.yaml no longer generates the tracker-rules ConfigMap"
grep -q 'disableNameSuffixHash: true' "$kustomization" \
    || fail "the tracker-rules ConfigMap lost its fixed name, which Tasks 2-4 mount"
grep -q 'mountPath: /rules' "$deployment" \
    || fail "the seeding guard init container no longer mounts /rules"

TRACKER_RULES="$rules" python3 "$helpers" --selftest | grep -qx 'selftest OK' \
    || fail "tracker_rules.py --selftest failed"
TRACKER_RULES="$rules" python3 "$policy" --selftest | grep -qx 'selftest OK' \
    || fail "cleanuparr-policy.py --selftest failed"

# Boot-time writer and hourly reconcile must agree, or each would undo the other.
TRACKER_RULES="$rules" PYTHONPATH="$here/files" python3 - "$guard" "$policy" <<'EOF' \
    || fail "enforce-private-seeding.py and cleanuparr-policy.py build different rule sets"
import importlib.util, json, os, sys
mods = []
for i, path in enumerate(sys.argv[1:]):
    spec = importlib.util.spec_from_file_location("m%d" % i, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mods.append(mod)
with open(os.environ["TRACKER_RULES"]) as f:
    rules = json.load(f)
a, b = (m.desired_seeding_rules(rules) for m in mods)
assert a == b and len(a) == 20, (a, b)
EOF

grep -q 'files/cleanuparr-policy.py' "$here/kustomization.yaml" \
    && grep -q 'files/tracker_rules.py' "$here/kustomization.yaml" \
    || fail "kustomization.yaml no longer generates cleanuparr-policy-script with both files"
grep -q 'mountPath: /rules' "$cronjob" && grep -q 'name: tracker-rules' "$cronjob" \
    || fail "the cleanuparr-policy CronJob no longer mounts tracker-rules at /rules"

TRACKER_RULES="$rules" python3 "$hnr_check" --selftest | grep -qx 'selftest OK' \
    || fail "torrent-hnr-check.py --selftest failed"
grep -q 'files/torrent-hnr-check.py' "$here/kustomization.yaml" \
    || fail "kustomization.yaml no longer generates torrent-hnr-check-script"
[ "$(grep -c 'mountPath: /rules' "$hnr_cronjob")" = 2 ] \
    || fail "both hit-and-run CronJobs must mount tracker-rules at /rules"

echo "torrent-policy tests OK"
