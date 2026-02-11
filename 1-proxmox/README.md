# Proxmox Setup and Configuration

This directory contains instructions for setting up two Proxmox VE hosts that serve as the foundation for the K3s and Docker Swarm clusters.

## Proxmox Hosts

| Name         | IP             | Role                                                       |
|--------------|----------------|------------------------------------------------------------|
| takaros      | 192.168.10.10  | K3s masters 51/52, workers 61/62, templates 9000/9001      |
| evanthoulaki | 192.168.10.11  | K3s master 53, workers 63/65, all Docker Swarm VMs         |

Both are HPE ProLiant DL380 Gen9 servers connected via two 10GbE bonded interfaces (balance-xor) and one 1GbE interface for the LAN. A quorum container on TrueNAS provides the 3rd vote for the Proxmox cluster.

SSH: `ssh root@192.168.10.10` (takaros), `ssh root@192.168.10.11` (evanthoulaki) — passwordless via SSH keys.

## Network Bridges

| Bridge | Purpose                                  |
|--------|------------------------------------------|
| vmbr0  | External network (192.168.10.0/24), internet, iSCSI, NFS |
| vmbr1  | Internal network (10.0.0.0/24), K3s flannel / inter-node |

Docker Swarm VMs use only vmbr0 (single NIC).

## Storage

VMs are stored on **TrueNAS iSCSI targets**, presented as local LVM-thin storage on each Proxmox node. Each K3s VM has its own iSCSI target:

| Storage Name     | VM              |
|------------------|-----------------|
| iscsi-master-51  | k3s-master-51   |
| iscsi-master-52  | k3s-master-52   |
| iscsi-master-53  | k3s-master-53   |
| iscsi-worker-61  | k3s-worker-61   |
| iscsi-worker-62  | k3s-worker-62   |
| iscsi-worker-63  | k3s-worker-63   |
| iscsi-worker-65  | k3s-worker-65   |

Docker Swarm VMs use `local-raid` storage on evanthoulaki.

## VM Templates

| Template ID | Name                    | Node    | Storage   | OS            |
|-------------|-------------------------|---------|-----------|---------------|
| 9000        | ubuntu-cloud-init (k3s) | takaros | local-raid | Ubuntu 24.04  |
| 9001        | ubuntu-24.04-cloud-init | takaros | local-raid | Ubuntu 24.04  |

## K3s VM Creation

All K3s VMs were created on takaros from template 9000:

```bash
# Clone, configure, resize (example for master)
qm clone 9000 <VMID> --name <NAME> --full --storage local-raid
qm set <VMID> --memory 10240 --cores 6
qm set <VMID> --ipconfig0 ip=<IP>/24,gw=192.168.10.1
qm set <VMID> --ipconfig1 ip=<INTERNAL_IP>/24
qm resize <VMID> scsi0 30G
```

**Master Nodes** (10GB RAM, 6 cores, 30GB disk on local-raid):
- VM 1051 (k3s-master-51): 192.168.10.51 / 10.0.0.51
- VM 1052 (k3s-master-52): 192.168.10.52 / 10.0.0.52
- VM 1053 (k3s-master-53): 192.168.10.53 / 10.0.0.53

**Worker Nodes** (22GB RAM, 8 cores, 50GB disk on local-raid):
- VM 1061 (k3s-worker-61): 192.168.10.61 / 10.0.0.61
- VM 1062 (k3s-worker-62): 192.168.10.62 / 10.0.0.62
- VM 1063 (k3s-worker-63): 192.168.10.63 / 10.0.0.63
- VM 1065 (k3s-worker-65): 192.168.10.65 / 10.0.0.65

## Node Placement Strategy

- Masters 51, 52 on takaros; master 53 on evanthoulaki (etcd quorum survives one host loss)
- Workers 61, 62 on takaros; workers 63, 65 on evanthoulaki (balanced workloads)

## Sysctl Configuration (All K3s VMs)

```bash
for ip in 51 52 53 61 62 63 65; do
  ssh ubuntu@192.168.10.$ip "sudo tee /etc/sysctl.d/99-k3s.conf > /dev/null << 'EOL'
net.ipv4.conf.all.rp_filter=2
net.ipv4.conf.all.src_valid_mark=1
net.ipv6.conf.all.disable_ipv6=1
EOL
sudo sysctl -p /etc/sysctl.d/99-k3s.conf"
done
```

## Subdirectories

| Directory    | Description                              |
|--------------|------------------------------------------|
| `user-vms/`  | User VM provisioning with jumpbox access |

## Next Steps

- K3s cluster installation: [../2-k3s/README.md](../2-k3s/README.md)
- Docker Swarm setup: [../3-docker-swarm/README.md](../3-docker-swarm/README.md)
- Detailed Proxmox instructions: [../.github/instructions/proxmox.instructions.md](../.github/instructions/proxmox.instructions.md)

## References

- [Proxmox VE Documentation](https://pve.proxmox.com/wiki/Main_Page)
- [Cloud-Init Documentation](https://cloudinit.readthedocs.io/)
