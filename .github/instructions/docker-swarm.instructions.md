---
applyTo: "3-docker-swarm/**"
description: "Instructions for Docker Swarm cluster setup and management on Proxmox"
---

# Docker Swarm-Specific Instructions

When working with files in the `3-docker-swarm/` directory, follow these Docker Swarm-specific guidelines.

**Credential Placeholders:**
All commands use placeholders for sensitive information. Replace with values from `.github/instructions/secrets.yml`:
- `<PROXMOX_HOST1_USER>` / `<PROXMOX_HOST1_IP>` → proxmox-takaros credentials
- `<PROXMOX_HOST2_USER>` / `<PROXMOX_HOST2_IP>` → proxmox-evanthoulaki credentials

## Cluster Overview

Docker Swarm cluster running on `evanthoulaki` Proxmox host. All VMs were cloned from template `9001` on `takaros` and migrated to `evanthoulaki` targeting `local-raid` storage.

### Node Inventory

| Role    | Hostname     | VMID | IP             | Storage    |
|---------|--------------|------|----------------|------------|
| Manager | ds-master    | 1071 | 192.168.10.71  | local-raid |
| Worker  | ds-worker-1  | 1072 | 192.168.10.72  | local-raid |
| Worker  | ds-worker-2  | 1073 | 192.168.10.73  | local-raid |

- **OS**: Ubuntu 24.04 LTS
- **User**: `ubuntu`
- **CPU**: 2 cores per VM
- **RAM**: 4 GB per VM
- **Disk**: 20 GB per VM on `local-raid`
- **Network**: `vmbr0`, static IPs, gateway `192.168.10.1`

### SSH Access

```bash
ssh ubuntu@192.168.10.71   # ds-master (manager)
ssh ubuntu@192.168.10.72   # ds-worker-1
ssh ubuntu@192.168.10.73   # ds-worker-2
```

## Quick Actions Reference

### Check Cluster Health

```bash
# List all swarm nodes (run on manager)
ssh ubuntu@192.168.10.71 "docker node ls"

# Check all running services
ssh ubuntu@192.168.10.71 "docker service ls"

# Check all deployed stacks
ssh ubuntu@192.168.10.71 "docker stack ls"
```

### Check VM Status on Proxmox

```bash
# Check all Docker Swarm VMs on evanthoulaki
ssh <PROXMOX_HOST2_USER>@<PROXMOX_HOST2_IP> "qm list | grep -E 'ds-'"

# Ping all nodes
for ip in 71 72 73; do ping -c 1 -W 2 192.168.10.$ip && echo "192.168.10.$ip UP" || echo "192.168.10.$ip DOWN"; done
```

### Restart a VM

```bash
# Stop and start a Docker Swarm VM
ssh <PROXMOX_HOST2_USER>@<PROXMOX_HOST2_IP> "qm stop <VMID> && qm start <VMID>"

# Wait for VM to be reachable
ssh <PROXMOX_HOST2_USER>@<PROXMOX_HOST2_IP> "qm agent <VMID> ping"
```

## VM Provisioning Details

### Why Two-Step Clone + Migrate

Template `9001` lives on `takaros` node. `local-raid` storage only exists on `evanthoulaki`. Proxmox cross-node cloning requires the target storage to be available on the source node. The workaround is:

1. Clone template on takaros → `local-lvm` (temporary)
2. Migrate VM to `evanthoulaki` with `--targetstorage local-raid`
3. Clone workers directly on `evanthoulaki` from the already-migrated manager VM

```bash
# Step 1 – Clone template on takaros
ssh <PROXMOX_HOST1_USER>@<PROXMOX_HOST1_IP> \
  "qm clone 9001 1071 --name ds-master --full --storage local-lvm"

# Step 2 – Migrate to evanthoulaki targeting local-raid
ssh <PROXMOX_HOST1_USER>@<PROXMOX_HOST1_IP> \
  "pvesh create /nodes/takaros/qemu/1071/migrate \
    --target evanthoulaki \
    --with-local-disks 1 \
    --targetstorage local-raid"

# Step 3 – Configure VM on evanthoulaki (set IP, SSH keys, resources)
ssh <PROXMOX_HOST2_USER>@<PROXMOX_HOST2_IP> "
  qm set 1071 --memory 4096 --cores 2 &&
  qm set 1071 --ciuser ubuntu &&
  qm set 1071 --ipconfig0 ip=192.168.10.71/24,gw=192.168.10.1 &&
  qm set 1071 --sshkeys /tmp/ds-sshkeys.pub &&
  qm set 1071 --serial0 socket --vga serial0 &&
  qm set 1071 --agent enabled=1 &&
  qm set 1071 --nameserver 192.168.10.1 &&
  qm resize 1071 scsi0 20G
"

# Step 4 – Clone workers on evanthoulaki from ds-master
ssh <PROXMOX_HOST2_USER>@<PROXMOX_HOST2_IP> "
  qm clone 1071 1072 --name ds-worker-1 --full --storage local-raid &&
  qm clone 1071 1073 --name ds-worker-2 --full --storage local-raid
"

# Step 5 – Configure workers
ssh <PROXMOX_HOST2_USER>@<PROXMOX_HOST2_IP> "
  qm set 1072 --memory 4096 --cores 2 --ciuser ubuntu \
    --ipconfig0 ip=192.168.10.72/24,gw=192.168.10.1 \
    --sshkeys /tmp/ds-sshkeys.pub --nameserver 192.168.10.1 &&
  qm set 1073 --memory 4096 --cores 2 --ciuser ubuntu \
    --ipconfig0 ip=192.168.10.73/24,gw=192.168.10.1 \
    --sshkeys /tmp/ds-sshkeys.pub --nameserver 192.168.10.1
"

# Step 6 – Start all VMs
ssh <PROXMOX_HOST2_USER>@<PROXMOX_HOST2_IP> \
  "qm start 1071 && qm start 1072 && qm start 1073"
```

## Docker Swarm Concepts

### Manager vs Worker
- **Manager nodes**: Maintain cluster state, schedule services, expose Swarm API
- **Worker nodes**: Execute container tasks assigned by the manager
- For fault tolerance, use an odd number of managers (1, 3, 5)
- This cluster has 1 manager — suitable for development/small production workloads

### Services vs Stacks
- **Service**: A single containerized application definition in Swarm (equivalent to a Deployment in k8s)
- **Stack**: A group of services defined in a Docker Compose file (equivalent to a Helm release)
- Always use `docker stack deploy` for production deployments (not `docker service create`)

### Overlay Networks
- Swarm services communicate over **overlay networks** (cross-node virtual networks)
- Always create a named overlay network for services that need to talk to each other
- The default `ingress` overlay handles published port routing

## Key Docker Swarm Patterns

### Deploy a Stack

```bash
# Deploy or update a stack from a compose file
docker stack deploy -c <compose-file.yml> <stack-name>

# Check deployment status
docker stack ps <stack-name>

# View service logs
docker service logs --tail 100 -f <stack-name>_<service-name>

# Remove a stack
docker stack rm <stack-name>
```

### Scaling Services

```bash
# Scale a service within a stack (prefer editing compose file and redeploying)
docker service scale <stack-name>_<service-name>=3

# Or update the replicas in the compose file and redeploy:
docker stack deploy -c <compose-file.yml> <stack-name>
```

### Rolling Updates

```bash
# Update service image (rolling update)
docker service update \
  --image <new-image>:<tag> \
  --update-parallelism 1 \
  --update-delay 10s \
  <stack-name>_<service-name>

# Rollback a service to previous version
docker service rollback <stack-name>_<service-name>
```

### Secrets Management

```bash
# Create a secret from stdin
echo "my-secret-value" | docker secret create secret_name -

# Create a secret from a file
docker secret create secret_name /path/to/secret.file

# List secrets
docker secret ls

# Reference in a compose file (secrets are mounted at /run/secrets/<name>):
# services:
#   myapp:
#     secrets:
#       - secret_name
# secrets:
#   secret_name:
#     external: true
```

### Configs (non-sensitive data)

```bash
# Create a config (for config files, not secrets)
docker config create nginx_conf /path/to/nginx.conf

# List configs
docker config ls

# Reference in a compose file:
# services:
#   nginx:
#     configs:
#       - source: nginx_conf
#         target: /etc/nginx/nginx.conf
# configs:
#   nginx_conf:
#     external: true
```

## Node Management

```bash
# List nodes with details
docker node ls

# Inspect a node
docker node inspect <node-name> --pretty

# Drain a node for maintenance (stops new tasks, reschedules existing)
docker node update --availability drain <node-name>

# Re-activate a node after maintenance
docker node update --availability active <node-name>

# Add a label to a node (for service placement constraints)
docker node update --label-add role=database ds-worker-1

# Remove a node from the swarm
# First on the node: docker swarm leave
# Then on manager:
docker node rm <node-name>
```

## Placement Constraints

Use node labels to control where services run:

```yaml
# In a stack compose file:
services:
  db:
    image: postgres:16
    deploy:
      placement:
        constraints:
          - node.labels.role == database
          - node.role == worker
```

## VMID and IP Conventions for Docker Swarm

- VMIDs: `107X` range (1071–1079) reserved for Docker Swarm nodes
- IPs: `192.168.10.7X` range (71–79) reserved for Docker Swarm nodes
- Current allocation:
  - `1071` / `.71` → ds-master (manager)
  - `1072` / `.72` → ds-worker-1
  - `1073` / `.73` → ds-worker-2

## File Organization

```
3-docker-swarm/
├── README.md                    # Main setup and operations guide
└── stacks/                      # Docker Compose stack definitions
    ├── README.md                # Stack index and deployment notes
    └── <service-name>/
        ├── docker-compose.yml   # Stack definition
        └── README.md            # Service-specific notes
```

### Stack File Naming Conventions
- Directory name: `kebab-case` matching the stack name used in `docker stack deploy`
- Main file: always `docker-compose.yml`
- Supporting configs/secrets: next to the compose file, never committed with real values

## Resource Recommendations

### Docker Swarm Manager Node
- **CPU**: 2 cores minimum (4 recommended for larger clusters)
- **RAM**: 4 GB minimum
- **Disk**: 20 GB minimum (more for image cache)

### Docker Swarm Worker Node
- **CPU**: 2+ cores (scale to workload)
- **RAM**: 4 GB minimum (increase for heavy workloads)
- **Disk**: 20 GB minimum

## Security Notes

- Use Docker secrets for all sensitive values (passwords, tokens, API keys)
- Never hardcode secrets in compose files committed to git
- Expose only necessary ports via `ports:` in stack files
- Use overlay networks to isolate service-to-service communication
- Manager nodes should not run user workloads in production (use `--constraint node.role==worker`)

## Required Ports (Firewall Rules)

Docker Swarm requires these ports to be open between all nodes:

| Port     | Protocol | Purpose                         |
|----------|----------|---------------------------------|
| 2377     | TCP      | Cluster management (manager only) |
| 7946     | TCP/UDP  | Node-to-node communication      |
| 4789     | UDP      | Overlay network (VXLAN)         |

```bash
# Verify ports are open between nodes
ssh ubuntu@192.168.10.71 "nc -zv 192.168.10.72 2377 && echo TCP 2377 OK"
ssh ubuntu@192.168.10.71 "nc -zv 192.168.10.73 2377 && echo TCP 2377 OK"
```

## Troubleshooting

### Node stuck in "Down" state

```bash
# Check Docker daemon on the affected node
ssh ubuntu@192.168.10.<XX> "sudo systemctl status docker"
ssh ubuntu@192.168.10.<XX> "sudo journalctl -u docker --since '10 minutes ago'"

# Restart Docker
ssh ubuntu@192.168.10.<XX> "sudo systemctl restart docker"
```

### Service tasks not starting

```bash
# Check task error on manager
docker service ps --no-trunc <service-name>

# Check available resources on nodes
docker node inspect <node-name> --pretty | grep -A5 Resources
```

### Overlay network not routing traffic

```bash
# Check overlay networks
docker network ls --filter driver=overlay

# Inspect the network
docker network inspect <overlay-network-name>

# Recreate if corrupted (remove stack and redeploy)
docker stack rm <stack-name>
docker stack deploy -c <compose-file.yml> <stack-name>
```

## External References

- [Docker Swarm Mode Overview](https://docs.docker.com/engine/swarm/)
- [Deploy Services to a Swarm](https://docs.docker.com/engine/swarm/services/)
- [Docker Compose File v3 Reference](https://docs.docker.com/compose/compose-file/compose-file-v3/)
- [Manage Swarm Secrets](https://docs.docker.com/engine/swarm/secrets/)
- [Swarm Administration Guide](https://docs.docker.com/engine/swarm/admin_guide/)