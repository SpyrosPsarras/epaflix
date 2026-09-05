#!/usr/bin/env bash
# One-shot local fixer: refresh the sops omp-api-key snapshot with the live
# key recovered from the guest env file. Fingerprints verified, values never
# printed.
set -euo pipefail
cd "$(dirname "$0")/../../.."
export SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/k3s-cluster.txt"

REPO_ENC=2-k3s/17.remote-pi/cliproxy/cliproxy-secrets.enc.yaml
LIVE=/tmp/live-omp-key
LIVE_FP=fd328c7f

[[ -s $LIVE ]] || { echo "no live key at $LIVE"; exit 1; }
LIVE=$(cat "$LIVE")

sops -d "$REPO_ENC" > /tmp/clip-secrets.yaml
chmod 0600 /tmp/clip-secrets.yaml
grep -q "omp-api-key" /tmp/clip-secrets.yaml

python3 - <<'EOF'
import yaml, hashlib
live = open('/tmp/live-omp-key').read().strip()
docs = list(yaml.safe_load_all(open('/tmp/clip-secrets.yaml')))
for d in docs:
    if d and 'omp-api-key' in d.get('stringData', {}):
        d['stringData']['omp-api-key'] = live
with open('/tmp/clip-secrets.yaml', 'w') as f:
    yaml.safe_dump_all(docs, f, explicit_start=True, sort_keys=False)
print('plaintext updated; fp:', hashlib.sha256(live.encode()).hexdigest()[:8])
EOF

TMPENC=$(mktemp --suffix=.enc.yaml)
cp /tmp/clip-secrets.yaml "$TMPENC"
sops -e "$TMPENC" > "$REPO_ENC"
rm -f "$TMPENC" /tmp/clip-secrets.yaml

sops -d "$REPO_ENC" > /tmp/rt-check.yaml
python3 - "$LIVE_FP" <<'EOF'
import yaml, hashlib, sys
fp_arg = sys.argv[1]
docs = list(yaml.safe_load_all(open('/tmp/rt-check.yaml')))
omp = next(d for d in docs if d and 'omp-api-key' in d.get('stringData', {}))['stringData']['omp-api-key']
fp = hashlib.sha256(omp.encode()).hexdigest()[:8]
print('round-trip fp:', fp)
assert fp == fp_arg, 'round-trip mismatch'
print('ROUND_TRIP_OK')
EOF
rm -f /tmp/rt-check.yaml