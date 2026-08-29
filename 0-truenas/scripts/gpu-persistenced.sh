#!/usr/bin/env bash
set -euo pipefail

if pgrep -f '/usr/bin/nvidia-persistenced' >/dev/null 2>&1; then
    echo "nvidia-persistenced already running"
    exit 0
fi

if ! modprobe nvidia; then
    echo "modprobe nvidia failed - no GPU driver, nothing to persist" >&2
    exit 1
fi

/usr/bin/nvidia-persistenced --verbose
echo "nvidia-persistenced started"
