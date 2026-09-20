#!/usr/bin/env bash
# Guards the nightly aaaa-tripwire CronJob against the two silent rots:
# the mirror drifting from the canonical script, and the CronJob losing
# its wiring (secret mount, schedule, deps, alert). No cluster is touched.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
canonical="$here/../../1-proxmox/pihole/aaaa-tripwire.sh"
mirror="$here/files/aaaa-tripwire.sh"
cronjob="$here/aaaa-tripwire-cronjob.yaml"
alerts="$here/../../2-k3s/10.observability/alertmanager-config/custom-alerts.yaml"
kustomization="$here/kustomization.yaml"

fail() { echo "FAIL: $*" >&2; exit 1; }

# The mirror must be byte-identical to the canonical script. Any edit to
# 1-proxmox/pihole/aaaa-tripwire.sh must be re-copied, or the nightly run
# silently checks a stale version of the rules.
cmp -s "$canonical" "$mirror" \
    || fail "files/aaaa-tripwire.sh drifted from 1-proxmox/pihole/aaaa-tripwire.sh - re-copy the canonical file"

bash -n "$mirror" || fail "the mirrored tripwire script does not parse"

grep -q 'PIHOLE="${PIHOLE:-192.168.10.30}"' "$mirror" \
    || fail "the mirror no longer pins the Pi-hole IP"

# CronJob wiring.
grep -q 'secretName: aaaa-tripwire-ssh' "$cronjob" \
    || fail "the CronJob no longer mounts the SSH key Secret"
grep -q 'name: aaaa-tripwire-script' "$cronjob" \
    || fail "the CronJob no longer mounts the script ConfigMap"
grep -q 'schedule: "40 3 \* \* \*"' "$cronjob" \
    || fail "the nightly schedule moved - update the alert description and this test together"
grep -q 'openssh-client bind-tools bash iproute2' "$cronjob" \
    || fail "the job image lost one of its runtime deps (ssh/dig/bash/ip)"

# The mirror must actually be generated from this overlay, and the
# CronJob applied from it.
grep -q 'aaaa-tripwire-cronjob.yaml' "$kustomization" \
    || fail "kustomization.yaml no longer applies the CronJob"
grep -q 'files/aaaa-tripwire.sh' "$kustomization" \
    || fail "kustomization.yaml no longer generates the script ConfigMap"

# The alert must exist with the right namespace/job selector, or a failed
# nightly run would be silent (the ntfy-canary #1108 class).
grep -q 'AaaaTripwireCheckFailed' "$alerts" \
    || fail "the AaaaTripwireCheckFailed alert is gone from custom-alerts.yaml"
grep -q 'kube_job_failed{namespace="kube-system", job_name=~"aaaa-tripwire.\*"}' "$alerts" \
    || fail "the alert no longer selects aaaa-tripwire jobs in kube-system"

echo "aaaa-tripwire-cronjob tests OK"
