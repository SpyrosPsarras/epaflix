# K3s Cluster Deployment

Highly-available K3s cluster (3 masters, 4 workers) running on Proxmox VMs across two hosts.

## Cluster Inventory

| Role   | Hostname      | VMID | External IP    | Internal IP | Host         |
|--------|---------------|------|----------------|-------------|--------------|
| Master | k3s-master-51 | 1051 | 192.168.10.51  | 10.0.0.51   | takaros      |
| Master | k3s-master-52 | 1052 | 192.168.10.52  | 10.0.0.52   | takaros      |
| Master | k3s-master-53 | 1053 | 192.168.10.53  | 10.0.0.53   | evanthoulaki |
| Worker | k3s-worker-61 | 1061 | 192.168.10.61  | 10.0.0.61   | takaros      |
| Worker | k3s-worker-62 | 1062 | 192.168.10.62  | 10.0.0.62   | takaros      |
| Worker | k3s-worker-63 | 1063 | 192.168.10.63  | 10.0.0.63   | evanthoulaki |
| Worker | k3s-worker-65 | 1065 | 192.168.10.65  | 10.0.0.65   | evanthoulaki |

- **OS**: Ubuntu 24.04 LTS
- **User**: `ubuntu` (SSH key auth)
- **Masters**: 10GB RAM, 6 cores, 30GB disk (local-raid)
- **Workers**: 22GB RAM, 8 cores, 50GB disk (local-raid)
- **VIP**: 192.168.10.100 (kube-vip control plane HA)
- **Traefik LB**: 192.168.10.101

## Network

- **External** (vmbr0, 192.168.10.0/24): Internet, SSH, LoadBalancer services, NFS, iSCSI
- **Internal** (vmbr1, 10.0.0.0/24): Flannel overlay, inter-node comms via eth1
- **DNS**: Pi-hole at 192.168.10.30
- **Gateway**: 192.168.10.1

## Installation

K3s was installed via [k3sup](https://github.com/alexellis/k3sup) with:
- `--disable servicelb --disable traefik` (replaced by kube-vip + Traefik Helm chart)
- `--flannel-iface eth1` (internal network)
- `--tls-san` for VIP and all master IPs
- Control plane taint on masters
- Etcd auto-compaction (1h periodic, 8GB quota)

Full k3sup commands are in [../.github/instructions/k3s.instructions.md](../.github/instructions/k3s.instructions.md).

## Deployment Order

Follow the numbered directories in sequence:

| # | Directory                        | Component                          |
|---|----------------------------------|------------------------------------|
| 1 | `01.kube-vip/`                   | Control plane VIP (192.168.10.100) |
| 2 | `02.cert-manager/`               | TLS certificates (self-signed CA for internal, Let's Encrypt for public) |
| 3 | `03.kube-vip-cloud-provider/`    | LoadBalancer IP pool management    |
| 4 | `04.coredns/`                    | Custom DNS forwarding to Pi-hole   |
| 5 | `05.traefik-deployment/`         | Reverse proxy, wildcard TLS for *.epaflix.com |
| 6 | `06.postgres/`                   | CloudNative-PG PostgreSQL cluster  |
| 7 | `07.authentik-deployment/`       | SSO/authentication (auth.epaflix.com) |
| 8 | `08.servarr/`                    | Media stack (Sonarr, Radarr, Prowlarr, etc.) |
| 9 | `09.filebrowser/`                | FileBrowser Quantum                |
| 10| `10.observability/`              | Prometheus, Loki, Grafana          |

Each directory has its own README with specific instructions.

## Key Services

All services are exposed via Traefik at `*.epaflix.com`, with DNS managed by Pi-hole pointing to 192.168.10.101.

## Maintenance

| Tool | Location | Purpose |
|------|----------|---------|
| K3s auto-upgrade | `maintenance/system-upgrade/` | Rolling K3s version upgrades |
| Node OS updater | `maintenance/` | Weekly apt upgrades with safe reboot |
| Image cleanup | `maintenance/` | Weekly containerd garbage collection |
| DB backups | `maintenance/` | PostgreSQL dump scripts |

See [maintenance/README.md](maintenance/README.md) for details.

## Useful Commands

```bash
# Check cluster
kubectl get nodes -o wide
kubectl get pods -A

# SSH to nodes
ssh ubuntu@192.168.10.51   # master-51
ssh ubuntu@192.168.10.61   # worker-61

# K3s service management
sudo systemctl restart k3s        # on masters
sudo systemctl restart k3s-agent  # on workers

# Uninstall K3s
/usr/local/bin/k3s-uninstall.sh        # master
/usr/local/bin/k3s-agent-uninstall.sh  # worker
```

## References

- [K3s Documentation](https://docs.k3s.io/)
- [k3sup Documentation](https://github.com/alexellis/k3sup)
- [Kubernetes Documentation](https://kubernetes.io/docs/)
