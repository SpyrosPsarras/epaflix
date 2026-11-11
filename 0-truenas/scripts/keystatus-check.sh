#!/usr/bin/env bash
set -euo pipefail

STATE_FILE="${STATE_FILE:-/var/tmp/keystatus-check.failing}"
DATASET="mpara-tank-lan/encrypted-backups"
DISCORD_ENV_FILE="${DISCORD_ENV_FILE:-/root/.config/discord-webhook.env}"
# shellcheck source=/dev/null
if [ ! -r "${DISCORD_ENV_FILE}" ] || ! . "${DISCORD_ENV_FILE}" \
    || [ -z "${DISCORD_WEBHOOK_URL:-}" ] || [ -z "${DISCORD_ROLE_ID:-}" ]; then
    echo "${DISCORD_ENV_FILE} is missing or does not set DISCORD_WEBHOOK_URL and DISCORD_ROLE_ID" >&2
    exit 2
fi

notify() {
    local title="$1" priority="$2" body="$3" json
    json="$(jq -n --arg username 'TrueNAS' --arg title "${title}" --arg priority "${priority}" \
        --arg body "${body}" --arg role "${DISCORD_ROLE_ID}" '
        ($priority == "high" or $priority == "urgent") as $loud
        | {username: $username,
           content: ((if $loud then "<@&\($role)> " else "" end) + "**\($title)**\n\($body)")}
        + {allowed_mentions: (if $loud then {roles: [$role]} else {parse: []} end)}')" || return
    curl -fsS -4 -m 10 \
        -H 'Content-Type: application/json' \
        --data-binary "${json}" \
        "${DISCORD_WEBHOOK_URL}" >/dev/null
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
