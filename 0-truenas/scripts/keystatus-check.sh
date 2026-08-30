#!/usr/bin/env bash
set -euo pipefail

NTFY_URL="https://ntfy.epaflix.com/truenas-alerts"
STATE_FILE="${STATE_FILE:-/var/tmp/keystatus-check.failing}"
DATASET="apps/encrypted-backups"

notify() {
    local title="$1" priority="$2" body="$3"
    curl -fsS -4 -m 10 \
        -H "Title: ${title}" \
        -H "Priority: ${priority}" \
        -d "${body}" \
        "${NTFY_URL}" >/dev/null
}

if keystatus="$(zfs get -H -o value keystatus "${DATASET}" 2>/dev/null)"; then
    if [ "${keystatus}" = "available" ]; then
        if [ -f "${STATE_FILE}" ]; then
            notify "TrueNAS ${DATASET} unlocked" "default" \
                "keystatus for ${DATASET} on 192.168.10.200 is available again: the encryption key is loaded. If the dataset is not mounted, run: zfs mount ${DATASET}"
            rm -f "${STATE_FILE}"
        fi
        exit 0
    fi
    if [ ! -f "${STATE_FILE}" ]; then
        notify "TrueNAS ${DATASET} is LOCKED" "high" \
            "A reboot left ${DATASET} locked on 192.168.10.200 (keystatus: ${keystatus}). The dataset is not mounted and backups into it are paused. Unlock it: zfs load-key ${DATASET} && zfs mount ${DATASET}"
        : > "${STATE_FILE}"
    fi
    exit 1
fi

if [ ! -f "${STATE_FILE}" ]; then
    notify "TrueNAS cannot read keystatus of ${DATASET}" "high" \
        "zfs get keystatus ${DATASET} failed on 192.168.10.200, so the locked-dataset check cannot tell locked from unlocked. The dataset may have been renamed or the pool degraded. Investigate on the box."
    : > "${STATE_FILE}"
fi
exit 1
