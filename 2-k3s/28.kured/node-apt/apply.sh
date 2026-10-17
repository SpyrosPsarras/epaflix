#!/usr/bin/env bash
# Installs the unattended-upgrades drop-in on every k3s node over the homelab SSH aliases.
set -uo pipefail
conf=$(cd "$(dirname "$0")" && pwd)/52unattended-upgrades-homelab
target=/etc/apt/apt.conf.d/52unattended-upgrades-homelab
failed=""

for node in $(kubectl get nodes -o name | sed 's#^node/##'); do
    out=$(ssh "$node" "sudo install -m 0644 /dev/stdin $target && apt-config dump Unattended-Upgrade::Allowed-Origins" <"$conf")
    if grep -qF -- '-updates";' <<<"$out"; then
        echo "$node: ok"
    else
        echo "$node: FAILED" >&2
        failed="$failed $node"
    fi
done

[ -z "$failed" ] || { echo "not applied on:$failed" >&2; exit 1; }
