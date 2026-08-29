#!/usr/bin/env bash
set -euo pipefail

NTFY_URL="https://ntfy.epaflix.com/truenas-alerts"
STATE_FILE="/var/tmp/gpu-health.failing"

notify() {
    local title="$1" priority="$2" body="$3"
    curl -fsS -4 -m 10 \
        -H "Title: ${title}" \
        -H "Priority: ${priority}" \
        -d "${body}" \
        "${NTFY_URL}" >/dev/null
}

gpu_visible() {
    nvidia-smi -L 2>/dev/null | grep -q '^GPU 0'
}

if gpu_visible; then
    if [[ -f "${STATE_FILE}" ]]; then
        notify "TrueNAS GPU recovered" "default" \
            "nvidia-smi on 192.168.10.200 can see the GPU again: $(nvidia-smi -L)"
        rm -f "${STATE_FILE}"
    fi
    if ! pgrep -f '/usr/bin/nvidia-persistenced' >/dev/null 2>&1; then
        notify "TrueNAS nvidia-persistenced died" "high" \
            "nvidia-persistenced is not running on 192.168.10.200. The GPU still works, but the driver can now tear down to idle and has to re-initialise - the exact condition that wedged the card on 2026-08-08. Restart it with /root/gpu-persistenced.sh"
    fi
    exit 0
fi

if [[ ! -f "${STATE_FILE}" ]]; then
    nvrm_tail=$(dmesg 2>/dev/null | grep -iE 'NVRM|WPR2' | tail -3 | tr '\n' ' ' || true)
    notify "TrueNAS GPU is down" "urgent" \
        "nvidia-smi found no GPU on 192.168.10.200 - ollama and Jellyfin have silently fallen back to CPU. Recovery: stop the ollama app and jellyfin container, rmmod the nvidia modules, PCIe reset 0000:01:00.0, modprobe nvidia. Last NVRM lines: ${nvrm_tail:-none}"
    : > "${STATE_FILE}"
fi

exit 1
