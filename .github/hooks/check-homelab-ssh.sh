#!/usr/bin/env bash
# Regenerate the homelab SSH config when the .lan DNS records change, and stage
# it with the commit: 1-proxmox/ssh/homelab.conf and the t3code pod's copy.
# CI (gen-homelab-ssh.test.py, sync-shared.sh --check) fails if this was skipped.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
git diff --cached --name-only | grep -qxE '1-proxmox/pihole/dnsmasq.d/30-epaflix-lan.conf|1-proxmox/ssh/gen-homelab-ssh.py' || exit 0
python3 1-proxmox/ssh/gen-homelab-ssh.py
cp 1-proxmox/ssh/homelab.conf 2-k3s/13.t3code/one/files/homelab-ssh.conf
git add 1-proxmox/ssh/homelab.conf 2-k3s/13.t3code/one/files/homelab-ssh.conf
echo "homelab ssh: regenerated 1-proxmox/ssh/homelab.conf from the .lan DNS records"
