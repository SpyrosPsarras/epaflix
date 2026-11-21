# Proxmox backups

The k3s VMs (1051-1053, 1061-1065) run with `agent: enabled=1,freeze-fs=0`, so vzdump skips the guest filesystem freeze. With freeze on, the guest stays frozen until the QMP `backup` call returns, which can take up to 125 s. On 2026-11-21 that stalled the CNPG primary on k3s-worker-63 for about 2 minutes. A new value only applies after a full Proxmox restart (`qm reboot`, after draining the node). A reboot from inside the guest, such as a kured reboot, leaves it pending.
