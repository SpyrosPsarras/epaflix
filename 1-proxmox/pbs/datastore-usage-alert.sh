#!/usr/bin/env bash
set -euo pipefail

# datastore-usage-alert.sh - warn when the PBS datastore VMs-NFS runs out of room.
#
# The #1075 incident: the datastore sat at 100% for ~12 days while every nightly
# backup on evanthoulaki failed with ENOSPC, and the PVE notification path was
# dead (#1076, #1108). This script runs on the PBS guest (LXC 1031), which owns
# /mnt/VMs, and posts straight to ntfy, bypassing PVE's broken webhook entirely.
#
# Install on LXC 1031:
#   scp datastore-usage-alert.sh root@<lxc-1031-ip>:/usr/local/sbin/
#   chmod +x /usr/local/sbin/datastore-usage-alert.sh
#   echo '*/15 * * * * root /usr/local/sbin/datastore-usage-alert.sh' \
#     > /etc/cron.d/pbs-datastore-usage
#
# Notify-only. Warns at WARN_PCT (85), goes urgent at CRIT_PCT (95), posts a
# recovery when usage drops below WARN_PCT, and stays silent while the level
# is unchanged (state file). A fall from critical back to warn stays silent
# but relaxes the state, so a re-peak into critical pages again: a datastore
# that crosses the critical line every night pages every night. A datastore
# that findmnt or df cannot read is urgent and re-notifies on every run: an
# unmounted datastore reads as the guest's root filesystem to df and would
# otherwise look healthy while backups fail (#1075's silence, again).
#
# Thresholds are defaults, not measurements: calibrate WARN_PCT/CRIT_PCT
# against the post-fix steady state (issue #1075 box 4: measure a week of
# nightly runs before the mp0 sizing decision, #763). GC at 00:01 holds
# pruned chunks for 24h5m, so the pre-GC peak every night is the honest high
# water mark; a nightly urgent page at 00:00 is a real sizing problem, not
# noise.
#
# ponytail: cron+ntfy alert, not a Prometheus exporter - if this guest itself
# is down nothing fires here, but then backups fail loudly elsewhere. Promote
# to an exporter + PrometheusRule if the metric is wanted in dashboards.
#
# Exit codes: 0 measured and quiet, 1 a notification was sent, 2 a notification
# was attempted but delivery failed.

DATASTORE_NAME="${DATASTORE_NAME:-VMs-NFS}"
DATASTORE_PATH="${DATASTORE_PATH:-/mnt/VMs}"
NTFY_URL="${NTFY_URL:-https://ntfy.epaflix.com/pve-backups}"
STATE_FILE="${STATE_FILE:-/var/tmp/pbs-datastore-usage.level}"
WARN_PCT="${WARN_PCT:-85}"
CRIT_PCT="${CRIT_PCT:-95}"

notify() {
    local title="$1" priority="$2" body="$3"
    curl -fsS -4 -m 10 \
        -H "Title: ${title}" \
        -H "Priority: ${priority}" \
        -d "${body}" \
        "${NTFY_URL}" >/dev/null
}

level_of() {
    local pct="$1"
    if [ "$pct" -ge "$CRIT_PCT" ]; then
        echo 2
    elif [ "$pct" -ge "$WARN_PCT" ]; then
        echo 1
    else
        echo 0
    fi
}

read_state() {
    if [ -f "$STATE_FILE" ]; then
        cat "$STATE_FILE"
    else
        echo 0
    fi
}

# A datastore df cannot measure is one branch; "mounted, but not the
# datastore" is the other and df cannot see it (unmounted mountpoint reads as
# the root filesystem). findmnt --mountpoint is the gate: unlike --target it
# requires the path itself to be a mountpoint and does not walk up to the
# enclosing filesystem.
mount_src="$(findmnt -n -o SOURCE --mountpoint "$DATASTORE_PATH" 2>/dev/null)" || true

# One df call: row 2 is the usage, anything else (no row, a warning line
# shifting the table down, error text on the same stream) means the datastore
# could not be measured.
out="$(df -Pk "$DATASTORE_PATH" 2>&1)" || true
readout="$(awk 'NR==2 && $5 ~ /^[0-9]+%$/ {
    pct = $5
    sub(/%$/, "", pct)
    printf "%d %d %d %d\n", int($2 / 1048576), int($3 / 1048576), int($4 / 1048576), pct
}' <<<"$out")"

if [ -z "$mount_src" ] || [ -z "$readout" ]; then
    if [ -z "$mount_src" ]; then
        reason="${DATASTORE_PATH} is not a mountpoint (findmnt found nothing) - the datastore may be unmounted"
    else
        reason="df on ${DATASTORE_PATH} failed: ${out%%$'\n'*}"
    fi
    if notify "PBS datastore ${DATASTORE_NAME} cannot be measured" urgent \
"The datastore cannot be measured on the PBS guest (LXC 1031): ${reason}.
The nightly backups are failing right now (issue #1075).
Check: mount | grep /mnt/VMs ; systemctl status proxmox-backup-proxy."; then
        exit 1
    else
        exit 2
    fi
fi

read -r total_gib used_gib avail_gib pct <<<"$readout"
cur="$(level_of "$pct")"

prev="$(read_state)"
case "$prev" in
    0|1|2) ;;
    *) prev=0 ;;
esac

if [ "$cur" -eq "$prev" ]; then
    exit 0
fi

usage_line="${DATASTORE_NAME} (${DATASTORE_PATH}): ${used_gib} GiB used of ${total_gib} GiB, ${avail_gib} GiB free (${pct}%)."

if [ "$cur" -gt "$prev" ]; then    if [ "$cur" -eq 2 ]; then
        body="${usage_line}
At 100% every nightly backup fails with ENOSPC - the #1075 incident ran silent for 12 days.
Run 'proxmox-backup-manager garbage-collection start ${DATASTORE_NAME}' by hand now and check what grew before the 01:00 job.
Some of the usage may be grace-window garbage: GC holds pruned chunks for 24h5m, check /mnt/VMs/.gc-status (pending-bytes)."
        title="PBS datastore ${DATASTORE_NAME} is ${pct}% full - backups will fail"
        priority=urgent
    else
        body="${usage_line}
At 100% every nightly backup fails with ENOSPC (issue #1075: 12 days silent).
Some of the usage may be grace-window garbage: GC holds pruned chunks for 24h5m, check /mnt/VMs/.gc-status (pending-bytes).
If steady-state usage keeps climbing, that is the #763 mp0 sizing decision."
        title="PBS datastore ${DATASTORE_NAME} is ${pct}% full"
        priority=high
    fi
    if notify "$title" "$priority" "$body"; then
        echo "$cur" >"$STATE_FILE"
        exit 1
    else
        exit 2
    fi
fi

# De-escalation: critical -> warn stays silent but relaxes the state, so a
# re-peak into critical pages again instead of hiding behind the old level.
# Recovery posts only when usage is actually back below the warn line.
if [ "$cur" -eq 0 ]; then
    if notify "PBS datastore ${DATASTORE_NAME} recovered" default \
"${usage_line}
Usage is back below the ${WARN_PCT}% warn line."; then
        rm -f "$STATE_FILE"
        exit 1
    else
        exit 2
    fi
fi

echo "$cur" >"$STATE_FILE"
exit 0
