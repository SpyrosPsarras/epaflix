#!/usr/bin/env bash
# Creates the t3code LXC on a Proxmox node. Run as root on the PVE host (B1).
set -euo pipefail

CT_ID=${CT_ID:?free CT id}
CT_IP=${CT_IP:?static guest IP in 192.168.10.x}
CT_GW=${CT_GW:?LAN gateway}
CT_DNS=${CT_DNS:?pihole IP, the guest must resolve cliproxy.epaflix.com}
CT_TEMPLATE=${CT_TEMPLATE:?Debian 13 template, e.g. local:vztmpl/debian-13-standard_13.2-1_amd64.tar.zst}
CT_SSH_KEYS=${CT_SSH_KEYS:-}
STORAGE=${STORAGE:-local-lvm}

SSH_ARGS=()
[[ -n $CT_SSH_KEYS ]] && SSH_ARGS=(--ssh-public-keys "$CT_SSH_KEYS")

pct create "$CT_ID" "$CT_TEMPLATE" \
  --hostname t3code \
  --unprivileged 1 \
  --cores 4 \
  --memory 8192 \
  --swap 8192 \
  --rootfs "${STORAGE}:64" \
  --net0 "name=eth0,bridge=vmbr0,ip=${CT_IP}/24,gw=${CT_GW}" \
  --nameserver "$CT_DNS" \
  --searchdomain epaflix.com \
  "${SSH_ARGS[@]}" \
  --onboot 1 \
  --start 1

echo "t3code LXC $CT_ID up at $CT_IP. Next: set the real IP in 2-k3s/05.traefik-deployment/ingress/t3code-proxy.yaml, then run 2-k3s/13.t3code/provision.sh inside the guest."
