#!/usr/bin/env bash
# Proxmox pre-start hook. Install on both hosts as local:snippets/pin-vm-host.sh.
# Refusing the incoming start aborts live migration. Offline moves remain
# possible, but the VM cannot start on the wrong host. Root can remove the hook.
set -euo pipefail
vmid=${1:?vmid} phase=${2:?phase}
[[ $phase == pre-start ]] || exit 0

case $vmid in
  1062) want=takaros ;;       # k3s-worker-62
  1065) want=evanthoulaki ;;  # k3s-worker-65
  *) echo "pin-vm-host: VM $vmid has no host pin; refusing to start" >&2; exit 1 ;;
esac

here=$(hostname -s)
if [[ $here != "$want" ]]; then
  echo "pin-vm-host: VM $vmid is pinned to $want, refusing to start on $here${PVE_MIGRATED_FROM:+ (migration from $PVE_MIGRATED_FROM)}" >&2
  exit 1
fi
