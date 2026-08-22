#!/usr/bin/env bash
# Alert when the TrueNAS GPU stops working.
#
# The 2026-08-08 failure was completely silent. Nothing on the host, in the apps,
# or in netdata said the card had gone. ollama kept answering - at 2.97 tok/s on
# CPU instead of 67.8 tok/s on the GPU - and Jellyfin transcoded on CPU. It took
# a human noticing slow chat replies to find it, roughly 23 hours later.
# Full write-up: 0-truenas/README.md, section "GPU (RTX 2070 SUPER)".
#
# Notifications go to the ntfy instance in K3s (LoadBalancer 192.168.10.112:8091,
# topic truenas-alerts). Alertmanager already uses this ntfy on topic
# k8s-alertmanager; TrueNAS host alerts get their own topic so the two do not mix.
#
# A state file makes this edge-triggered: one alert when the GPU goes, one when it
# comes back, nothing in between. Run from cron every 15 minutes.
#
# Installed on truenas (192.168.10.200) as /root/gpu-health-check.sh.
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

# Deliberately not `nvidia-smi -L; echo $?`. During the 2026-08-08 outage the
# driver was loaded and only the device was gone, so exit codes are not a
# dependable signal - the presence of a real GPU line is.
gpu_visible() {
    nvidia-smi -L 2>/dev/null | grep -q '^GPU 0'
}

if gpu_visible; then
    if [[ -f "${STATE_FILE}" ]]; then
        notify "TrueNAS GPU recovered" "default" \
            "nvidia-smi on 192.168.10.200 can see the GPU again: $(nvidia-smi -L)"
        rm -f "${STATE_FILE}"
    fi
    # Persistence mode is what stops the driver having to re-initialise under
    # memory pressure. If the daemon died, the protection is gone silently.
    if ! pgrep -f '/usr/bin/nvidia-persistenced' >/dev/null 2>&1; then
        notify "TrueNAS nvidia-persistenced died" "high" \
            "nvidia-persistenced is not running on 192.168.10.200. The GPU still works, but the driver can now tear down to idle and has to re-initialise - the exact condition that wedged the card on 2026-08-08. Restart it with /root/gpu-persistenced.sh"
    fi
    exit 0
fi

# GPU is not visible. Alert once per outage, not every 15 minutes.
if [[ ! -f "${STATE_FILE}" ]]; then
    nvrm_tail=$(dmesg 2>/dev/null | grep -iE 'NVRM|WPR2' | tail -3 | tr '\n' ' ' || true)
    notify "TrueNAS GPU is down" "urgent" \
        "nvidia-smi found no GPU on 192.168.10.200 - ollama and Jellyfin have silently fallen back to CPU. Recovery: stop the ollama app and jellyfin container, rmmod the nvidia modules, PCIe reset 0000:01:00.0, modprobe nvidia. Recovery steps: 0-truenas/README.md, section 'GPU (RTX 2070 SUPER)'. Last NVRM lines: ${nvrm_tail:-none}"
    : > "${STATE_FILE}"
fi

exit 1
