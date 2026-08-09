#!/usr/bin/env bash
# Keep the NVIDIA driver initialised on the TrueNAS host.
#
# Why this exists: on 2026-08-08 the RTX 2070 SUPER wedged itself. ARC had eaten
# the host RAM, the driver hit NV_ERR_NO_MEMORY part-way through initialising the
# GSP firmware, and the half-written WPR2 region left the card unusable until a
# PCIe function-level reset. Every nvidia-smi after that said "No devices were
# found", and ollama plus Jellyfin silently fell back to CPU for ~23 hours.
# Full write-up: 0-truenas/README.md, section "GPU (RTX 2070 SUPER)".
#
# Persistence mode keeps the driver loaded and initialised even when no client
# holds the device, so the driver never has to re-initialise under memory
# pressure - which is exactly when it died.
#
# Installed on truenas (192.168.10.200) as /root/gpu-persistenced.sh and run at
# POSTINIT by a TrueNAS init/shutdown script. Idempotent - safe to re-run.
set -euo pipefail

if pgrep -f '/usr/bin/nvidia-persistenced' >/dev/null 2>&1; then
    echo "nvidia-persistenced already running"
    exit 0
fi

# The driver modules normally load at boot, but do not assume it - persistenced
# cannot open /dev/nvidiactl without them.
if ! modprobe nvidia; then
    echo "modprobe nvidia failed - no GPU driver, nothing to persist" >&2
    exit 1
fi

/usr/bin/nvidia-persistenced --verbose
echo "nvidia-persistenced started"
