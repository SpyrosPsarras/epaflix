# K3s & Docker Swarm on Proxmox

This project contains infrastructure configuration and documentation for deploying a K3s Kubernetes cluster and a Docker Swarm cluster on Proxmox VE virtual machines.

## Project Structure

```
.
├── 0-truenas/          # TrueNAS iSCSI & NFS storage setup
├── 1-proxmox/          # Proxmox VM setup and configuration
├── 2-k3s/              # K3s cluster deployment and management
└── 3-docker-swarm/     # Docker Swarm cluster deployment and management
```

## Overview

This repository provides step-by-step instructions and automation scripts to:
- Configure two Proxmox hosts in a cluster (`takaros` and `evanthoulaki`)
- Deploy a lightweight Kubernetes cluster using K3s (3 masters, 4 workers)
- Deploy a Docker Swarm cluster (1 manager, 2 workers) on `evanthoulaki`
- Manage the infrastructure and cluster lifecycle

## Prerequisites

- Two Proxmox VE installations configured as a cluster
- Network connectivity between Proxmox hosts
- SSH access to Proxmox hosts
- Basic knowledge of Kubernetes, Docker Swarm, and virtualization

---

## Clusters

### K3s Kubernetes Cluster

A highly-available K3s cluster spread across both Proxmox hosts.

| Role   | Hostname      | VMID | IP             | Host         |
|--------|---------------|------|----------------|--------------|
| Master | k3s-master-51 | 1051 | 192.168.10.51  | takaros      |
| Master | k3s-master-52 | 1052 | 192.168.10.52  | takaros      |
| Master | k3s-master-53 | 1053 | 192.168.10.53  | evanthoulaki |
| Worker | k3s-worker-61 | 1061 | 192.168.10.61  | takaros      |
| Worker | k3s-worker-62 | 1062 | 192.168.10.62  | takaros      |
| Worker | k3s-worker-63 | 1063 | 192.168.10.63  | evanthoulaki |
| Worker | k3s-worker-65 | 1065 | 192.168.10.65  | evanthoulaki |

See [2-k3s/README.md](2-k3s/README.md) for full setup instructions.

### Docker Swarm Cluster

A Docker Swarm cluster running entirely on `evanthoulaki`, using `local-raid` storage.

| Role    | Hostname     | VMID | IP            | Host         |
|---------|--------------|------|---------------|--------------|
| Manager | ds-master    | 1071 | 192.168.10.71 | evanthoulaki |
| Worker  | ds-worker-1  | 1072 | 192.168.10.72 | evanthoulaki |
| Worker  | ds-worker-2  | 1073 | 192.168.10.73 | evanthoulaki |

See [3-docker-swarm/README.md](3-docker-swarm/README.md) for full setup instructions.

---

## Proxmox Hosts

| Name         | IP             | Role                            |
|--------------|----------------|---------------------------------|
| takaros      | 192.168.10.10  | Hosts K3s masters 51/52, workers 61/62, template 9001 |
| evanthoulaki | 192.168.10.11  | Hosts K3s master 53, workers 63/65, all Docker Swarm VMs |

Credentials are stored in `.github/instructions/secrets.yml` (git-ignored).

---

## Quick Start

### 1. Setup Proxmox Hosts & Storage
- Follow instructions in [1-proxmox/README.md](1-proxmox/README.md)
- Configure networking, storage pools, and VM templates

### 2. Deploy K3s Cluster
- Follow instructions in [2-k3s/README.md](2-k3s/README.md)
- Initialize control plane and join worker nodes

### 3. Deploy Docker Swarm Cluster
- Follow instructions in [3-docker-swarm/README.md](3-docker-swarm/README.md)
- Install Docker Engine, initialize swarm, join workers

---

## SSH Access

All VMs share the same authorized SSH keys (injected via cloud-init):
- `spy@spy-linux` (local laptop)
- `root@takaros` (Proxmox host)
- `spy@epaflix` (remote access)

```bash
# K3s nodes
ssh ubuntu@192.168.10.51   # k3s-master-51
ssh ubuntu@192.168.10.61   # k3s-worker-61

# Docker Swarm nodes
ssh ubuntu@192.168.10.71   # ds-master
ssh ubuntu@192.168.10.72   # ds-worker-1
ssh ubuntu@192.168.10.73   # ds-worker-2
```

---

## VM Templates

| Template ID | Name                         | Node    | Storage   | Base OS       |
|-------------|------------------------------|---------|-----------|---------------|
| 9000        | ubuntu-cloud-init (k3s)      | takaros | local-raid | Ubuntu 24.04  |
| 9001        | ubuntu-24.04-cloud-init      | takaros | local-raid | Ubuntu 24.04  |

---

## Network Layout

- **External network**: `192.168.10.0/24` via `vmbr0` — internet access, SSH, cluster API
- **Internal network**: `10.0.0.0/24` via `vmbr1` — K3s inter-node communication (flannel)
- **Gateway**: `192.168.10.1`

Docker Swarm VMs use only `vmbr0` (single NIC), as swarm overlay networking handles cross-node traffic.

---

## Directory Guide

| Directory         | Description |
|-------------------|-------------|
| `0-truenas/`      | TrueNAS iSCSI target setup, NFS shares, storage pool management |
| `1-proxmox/`      | Proxmox host configuration, VM templates, network bridges |
| `2-k3s/`          | K3s install, kube-vip, MetalLB, Traefik, monitoring, app stacks |
| `3-docker-swarm/` | Docker Swarm init, node join, service stack definitions |
| `.github/`        | Copilot/AI instructions, domain-specific guidance per directory |
| `.history/`       | Session logs of commands run and their outputs (git-ignored) |

---

## References

- [Proxmox VE Documentation](https://pve.proxmox.com/wiki/Main_Page)
- [K3s Documentation](https://docs.k3s.io/)
- [k3sup Documentation](https://github.com/alexellis/k3sup)
- [Docker Swarm Documentation](https://docs.docker.com/engine/swarm/)
- [Kubernetes Documentation](https://kubernetes.io/docs/)