# Docker Swarm on Proxmox

This directory contains instructions and configuration for deploying a Docker Swarm cluster on Proxmox VE, running on dedicated VMs on the `evanthoulaki` Proxmox host.

## Overview

Docker Swarm provides container orchestration with a simpler operational model than Kubernetes. This cluster is deployed alongside the existing K3s cluster and is intended for workloads that benefit from Docker Compose-style service definitions with native swarm scheduling.

> **Current Status:** All three VMs (`ds-master`, `ds-worker-1`, `ds-worker-2`) are currently **stopped** on `evanthoulaki`.
> Start them with: `ssh root@<EVANTHOULAKI_IP> "qm start 1071 && qm start 1072 && qm start 1073"`

## Cluster Architecture

| Role     | VM Name      | VMID | IP              | Host         | Storage    |
|----------|--------------|------|-----------------|--------------|------------|
| Manager  | ds-master    | 1071 | 192.168.10.71   | evanthoulaki | local-raid |
| Worker   | ds-worker-1  | 1072 | 192.168.10.72   | evanthoulaki | local-raid |
| Worker   | ds-worker-2  | 1073 | 192.168.10.73   | evanthoulaki | local-raid |

### VM Specifications

- **OS**: Ubuntu 24.04 LTS (cloud-init, from template 9001 on takaros)
- **CPU**: 2 cores
- **RAM**: 4 GB
- **Disk**: 20 GB (`local-raid` on evanthoulaki)
- **Network**: `vmbr0` bridge, static IPs in 192.168.10.0/24, gateway 192.168.10.1
- **User**: `ubuntu` (SSH key authentication only)

### SSH Access

```bash
# Manager node
ssh ubuntu@192.168.10.71

# Worker nodes
ssh ubuntu@192.168.10.72
ssh ubuntu@192.168.10.73
```

---

## Table of Contents

1. [VM Provisioning](#1-vm-provisioning)
2. [Install Docker Engine](#2-install-docker-engine)
3. [Initialize Docker Swarm](#3-initialize-docker-swarm)
4. [Join Worker Nodes](#4-join-worker-nodes)
5. [Verify Cluster](#5-verify-cluster)
6. [Deploying Stacks](#6-deploying-stacks)
7. [Useful Commands](#7-useful-commands)
8. [Troubleshooting](#8-troubleshooting)
9. [Terraform IAC](#9-terraform-iac)

---

## 1. VM Provisioning

The VMs were created from Proxmox template `9001` (Ubuntu 24.04 cloud-init template on `takaros`).
Since `local-raid` storage exists only on `evanthoulaki`, the process was:

1. Clone template `9001` on `takaros` to `local-lvm` temporarily.
2. Migrate VM to `evanthoulaki` targeting `local-raid`.
3. Clone workers `1072` and `1073` from `1071` directly on `evanthoulaki`.

### Re-create VMs (if needed)

```bash
# SSH into takaros
ssh root@<PROXMOX_HOST1_IP>

# Step 1: Clone template to takaros local-lvm
qm clone 9001 1071 --name ds-master --full --storage local-lvm

# Step 2: Configure before migration
ssh root@<PROXMOX_HOST2_IP> "cat > /tmp/ds-sshkeys.pub << 'SSHEOF'
<YOUR_SSH_PUBLIC_KEYS>
SSHEOF"

# Step 3: Migrate to evanthoulaki local-raid
pvesh create /nodes/takaros/qemu/1071/migrate \
  --target evanthoulaki \
  --with-local-disks 1 \
  --targetstorage local-raid

# Step 4: Configure VM 1071 on evanthoulaki
ssh root@<PROXMOX_HOST2_IP> "
qm set 1071 --memory 4096 --cores 2 &&
qm set 1071 --ciuser ubuntu &&
qm set 1071 --ipconfig0 ip=192.168.10.71/24,gw=192.168.10.1 &&
qm set 1071 --sshkeys /tmp/ds-sshkeys.pub &&
qm set 1071 --serial0 socket --vga serial0 &&
qm set 1071 --agent enabled=1 &&
qm set 1071 --nameserver 192.168.10.1 &&
qm resize 1071 scsi0 20G
"

# Step 5: Clone workers from ds-master on evanthoulaki
ssh root@<PROXMOX_HOST2_IP> "
qm clone 1071 1072 --name ds-worker-1 --full --storage local-raid &&
qm clone 1071 1073 --name ds-worker-2 --full --storage local-raid
"

# Step 6: Configure workers
ssh root@<PROXMOX_HOST2_IP> "
qm set 1072 --memory 4096 --cores 2 --ciuser ubuntu \
  --ipconfig0 ip=192.168.10.72/24,gw=192.168.10.1 \
  --sshkeys /tmp/ds-sshkeys.pub --nameserver 192.168.10.1 &&
qm set 1073 --memory 4096 --cores 2 --ciuser ubuntu \
  --ipconfig0 ip=192.168.10.73/24,gw=192.168.10.1 \
  --sshkeys /tmp/ds-sshkeys.pub --nameserver 192.168.10.1
"

# Step 7: Start all VMs
ssh root@<PROXMOX_HOST2_IP> "qm start 1071 && qm start 1072 && qm start 1073"
```

### Verify VMs are reachable

```bash
ping -c 3 192.168.10.71
ping -c 3 192.168.10.72
ping -c 3 192.168.10.73

ssh ubuntu@192.168.10.71 "hostname"
ssh ubuntu@192.168.10.72 "hostname"
ssh ubuntu@192.168.10.73 "hostname"
```

---

## 2. Install Docker Engine

Run the following on **all 3 nodes** (manager + workers):

```bash
# Update package index
sudo apt-get update

# Install prerequisites
sudo apt-get install -y ca-certificates curl gnupg lsb-release

# Add Docker's official GPG key
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# Add Docker repository
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker Engine
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin

# Add ubuntu user to docker group (no sudo needed)
sudo usermod -aG docker ubuntu

# Enable and start Docker
sudo systemctl enable docker
sudo systemctl start docker
```

### Verify Docker installation

```bash
# Check Docker version
docker --version

# Check Docker is running
sudo systemctl status docker

# Run a test container (after re-login or newgrp docker)
docker run --rm hello-world
```

### One-liner to install Docker on all nodes from your laptop

```bash
for ip in 71 72 73; do
  echo "=== Installing Docker on 192.168.10.$ip ==="
  ssh ubuntu@192.168.10.$ip "
    sudo apt-get update -qq &&
    sudo apt-get install -y -q ca-certificates curl gnupg &&
    sudo install -m 0755 -d /etc/apt/keyrings &&
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      -o /etc/apt/keyrings/docker.asc &&
    sudo chmod a+r /etc/apt/keyrings/docker.asc &&
    echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
      https://download.docker.com/linux/ubuntu \
      \$(. /etc/os-release && echo \"\$VERSION_CODENAME\") stable\" | \
      sudo tee /etc/apt/sources.list.d/docker.list > /dev/null &&
    sudo apt-get update -qq &&
    sudo apt-get install -y -q docker-ce docker-ce-cli containerd.io \
      docker-buildx-plugin docker-compose-plugin &&
    sudo usermod -aG docker ubuntu &&
    sudo systemctl enable docker &&
    sudo systemctl start docker &&
    echo 'Docker installed successfully on \$(hostname)'
  "
done
```

---

## 3. Initialize Docker Swarm

Run on the **manager node** (`ds-master`, `192.168.10.71`) only:

```bash
# Initialize swarm - advertise on the node's IP
docker swarm init --advertise-addr 192.168.10.71
```

The output will include a `docker swarm join` command with a token. Save this token.

### Get join tokens (if needed later)

```bash
# On ds-master: Get worker join token
docker swarm join-token worker

# On ds-master: Get manager join token (for adding more managers)
docker swarm join-token manager
```

---

## 4. Join Worker Nodes

Run the following on **both worker nodes** (`ds-worker-1` and `ds-worker-2`).
Replace `<JOIN_TOKEN>` with the token from the previous step:

```bash
# On ds-worker-1 (192.168.10.72) and ds-worker-2 (192.168.10.73)
docker swarm join --token <JOIN_TOKEN> 192.168.10.71:2377
```

### Join workers from your laptop (one-liner)

```bash
# Get the join command from the manager
JOIN_CMD=$(ssh ubuntu@192.168.10.71 "docker swarm join-token worker -q")
JOIN_TOKEN=$(echo "$JOIN_CMD")

for ip in 72 73; do
  echo "=== Joining 192.168.10.$ip to swarm ==="
  ssh ubuntu@192.168.10.$ip \
    "docker swarm join --token $JOIN_TOKEN 192.168.10.71:2377"
done
```

---

## 5. Verify Cluster

Run on the **manager node** (`ds-master`):

```bash
# List all nodes in the swarm
docker node ls

# Expected output:
# ID          HOSTNAME      STATUS    AVAILABILITY   MANAGER STATUS   ENGINE VERSION
# xxxx *      ds-master     Ready     Active         Leader           xx.x.x
# yyyy        ds-worker-1   Ready     Active                          xx.x.x
# zzzz        ds-worker-2   Ready     Active                          xx.x.x

# Inspect a node
docker node inspect ds-worker-1 --pretty

# Check swarm info
docker info | grep -A 10 "Swarm"
```

---

## 6. Deploying Stacks

Docker Swarm uses **Docker Compose files** (v3+) deployed as **stacks**.

### Currently deployed stacks

| Stack   | Directory                              | Domain                | Description                            |
|---------|----------------------------------------|-----------------------|----------------------------------------|
| traefik | [`stacks/traefik/`](./stacks/traefik/) | `traefik.epaflix.com` | Reverse proxy, wildcard TLS, dashboard |

See [`stacks/README.md`](./stacks/README.md) for deployment conventions and the full list.

### Example: Deploy a simple nginx stack

```bash
# Create a stack file
cat > nginx-stack.yml << 'EOF'
version: "3.8"
services:
  web:
    image: nginx:alpine
    ports:
      - "80:80"
    deploy:
      replicas: 3
      update_config:
        parallelism: 1
        delay: 10s
      restart_policy:
        condition: on-failure
EOF

# Deploy the stack
docker stack deploy -c nginx-stack.yml nginx-test

# List stacks
docker stack ls

# List services in a stack
docker stack services nginx-test

# List tasks (containers) in a stack
docker stack ps nginx-test

# Remove the stack
docker stack rm nginx-test
```

### Stack files location

Place stack compose files in the `stacks/` subdirectory of this folder.
See [`stacks/`](./stacks/) for available service definitions.

---

## 7. Useful Commands

### Node Management

```bash
# List nodes
docker node ls

# Promote a worker to manager
docker node promote ds-worker-1

# Demote a manager to worker
docker node demote ds-worker-1

# Drain a node (stop scheduling new tasks, for maintenance)
docker node update --availability drain ds-worker-1

# Re-activate a drained node
docker node update --availability active ds-worker-1

# Remove a node from swarm (run on node first: docker swarm leave)
docker node rm ds-worker-1
```

### Service Management

```bash
# List running services
docker service ls

# Scale a service
docker service scale <service-name>=5

# Update a service image
docker service update --image nginx:latest <service-name>

# View service logs
docker service logs <service-name>

# Inspect a service
docker service inspect <service-name> --pretty

# List tasks of a service
docker service ps <service-name>
```

### Stack Management

```bash
# Deploy or update a stack
docker stack deploy -c stack-file.yml <stack-name>

# List all stacks
docker stack ls

# List services in a stack
docker stack services <stack-name>

# Remove a stack (removes all services in it)
docker stack rm <stack-name>
```

### Networking

```bash
# List networks
docker network ls

# Create an overlay network for swarm services
docker network create --driver overlay --attachable my-overlay

# Inspect a network
docker network inspect my-overlay
```

### Volumes & Secrets

```bash
# Create a swarm secret
echo "mysecretvalue" | docker secret create my_secret -

# List secrets
docker secret ls

# Use in a stack (compose file):
# secrets:
#   my_secret:
#     external: true

# Create a named volume
docker volume create my-data

# List volumes
docker volume ls
```

---

## 8. Troubleshooting

### Node shows as Down

```bash
# Check Docker service on the affected node
ssh ubuntu@192.168.10.<XX> "sudo systemctl status docker"

# Restart Docker if needed
ssh ubuntu@192.168.10.<XX> "sudo systemctl restart docker"

# Check node in swarm
docker node inspect <node-name> --pretty
```

### Task keeps failing / restarting

```bash
# Check task history for error
docker service ps --no-trunc <service-name>

# Check logs of a specific task
docker service logs --tail 50 <service-name>

# Check container logs directly on the node running the task
ssh ubuntu@192.168.10.<XX> "docker ps -a && docker logs <container-id>"
```

### Swarm network connectivity issues

```bash
# Verify ports required by Docker Swarm are not blocked:
# TCP 2377 - cluster management (manager only)
# TCP/UDP 7946 - node communication
# UDP 4789 - overlay network traffic

# Test connectivity between nodes
ssh ubuntu@192.168.10.71 "nc -zv 192.168.10.72 2377"
ssh ubuntu@192.168.10.71 "nc -zv 192.168.10.73 2377"
```

### Re-initialize the swarm (destructive - last resort)

```bash
# On all workers - leave the swarm
docker swarm leave

# On manager - force leave
docker swarm leave --force

# Re-initialize (see Section 3)
docker swarm init --advertise-addr 192.168.10.71
```

### Check VM is healthy (from Proxmox)

```bash
# On evanthoulaki
ssh root@<PROXMOX_HOST2_IP> "qm list | grep -E 'ds-'"

# Check QEMU guest agent
ssh root@<PROXMOX_HOST2_IP> "qm agent 1071 ping && echo OK"
```

---

## 9. Terraform IAC

Terraform IAC for this cluster is the primary goal of the `appish` project. The Proxmox Terraform provider (`bpg/proxmox`) will be used to manage the full VM lifecycle — creation, configuration, and teardown — replacing the manual `qm` commands documented in [Section 1](#1-vm-provisioning).

> **Status:** Not yet implemented. The `terraform/` directory will be added to this folder as the first Terraform module is built.

### Planned scope

- Declarative definition of all three Swarm VMs (`ds-master`, `ds-worker-1`, `ds-worker-2`) on `evanthoulaki`
- Cloud-init integration for first-boot configuration (user, SSH keys, static IP)
- Remote state backend (S3-compatible or local)

### References

- [Proxmox Terraform Provider (`bpg/proxmox`)](https://registry.terraform.io/providers/bpg/proxmox/latest/docs)
- [Provider: Virtual Machine resource](https://registry.terraform.io/providers/bpg/proxmox/latest/docs/resources/virtual_environment_vm)

---

## Next Steps

- **Bring the cluster back up** — start the VMs and verify `docker node ls` shows all three nodes `Ready`
- **Terraform IAC** — implement `terraform/` module to manage VM provisioning (see [Section 9](#9-terraform-iac))
- **Additional stacks** — see [`stacks/`](./stacks/) for service definitions; Traefik is already deployed
- **Monitoring** — add a Prometheus/Grafana stack or Portainer for observability
- **Private registry** — consider a self-hosted Docker registry for private images

## References

- [Docker Swarm Documentation](https://docs.docker.com/engine/swarm/)
- [Docker Compose File Reference (v3)](https://docs.docker.com/compose/compose-file/compose-file-v3/)
- [Docker Secrets](https://docs.docker.com/engine/swarm/secrets/)
- [Proxmox VE Documentation](https://pve.proxmox.com/wiki/Main_Page)