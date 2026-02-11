# Repository Overview

This repository contains infrastructure-as-code and documentation for deploying a K3s Kubernetes cluster and a Docker Swarm cluster on Proxmox VE virtualization hosts. The project is designed for two-node Proxmox setups (`takaros` and `evanthoulaki`) with a 3-master/4-worker K3s cluster and a 1-manager/2-worker Docker Swarm cluster. The Project aims to make the process repeatable and well-documented, with a focus on best practices for both Proxmox VM management and container orchestration configuration.

## Repository Type and Size

- **Type**: Infrastructure documentation and automation scripts
- **Size**: Small to medium (documentation-heavy)
- **Languages**: Shell scripts (bash), YAML (Kubernetes manifests, Docker Compose stacks, cloud-init configs)
- **Frameworks**: K3s (lightweight Kubernetes) install via k3sup, Docker Swarm, Proxmox VE
- **Target Runtime**: Linux (Ubuntu 24.04) VMs on Proxmox

## Project Structure

```
k3s-swarm-proxmox/
├── .github/
│   ├── copilot-instructions.md           # This file
│   └── instructions/                      # Domain-specific instruction files
│       ├── general.instructions.md       # General practices and command history
│       ├── k3s.instructions.md           # K3s-specific guidance
│       ├── proxmox.instructions.md       # Proxmox-specific guidance
│       ├── truenas.instructions.md       # TrueNAS-specific guidance
│       ├── docker-swarm.instructions.md  # Docker Swarm-specific guidance
│       └── secrets.yml                   # Credentials reference (git-ignored)
├── .history/                              # Command logs & outputs (git-ignored)
│   ├── README.md                         # History documentation guide
│   ├── example-session.log               # Template for session logs
│   └── YYYY-MM-DD-*.log                  # Dated session logs
├── 0-truenas/                             # TrueNAS iSCSI setup
│   └── README.md
├── 1-proxmox/                             # Proxmox VM configuration
│   └── README.md
├── 2-k3s/                                 # K3s cluster deployment
│   └── README.md
├── 3-docker-swarm/                        # Docker Swarm cluster deployment
│   ├── README.md
│   └── stacks/                           # Docker Compose stack definitions
│       └── README.md
├── .gitignore
└── README.md                              # Project overview
```

### Setup Steps

This is a documentation and infrastructure repository. There are no traditional build steps, but validation involves:

1. **Lint Documentation** (if tools are added later):
   ```bash
   # Not yet implemented - would use markdownlint
   # markdownlint '**/*.md'
   ```

2. **Validate Shell Scripts** (when scripts are added):
   ```bash
   # Check shell script syntax
   shellcheck scripts/**/*.sh
   ```

3. **Test in Lab Environment**:
   - Deploy to actual Proxmox hosts
   - Verify VM creation and K3s installation
   - Validate cluster functionality

### No Automated Tests

Currently, this repository contains documentation and manual procedures. Testing is manual and requires:
- Access to Proxmox VE hosts

## Key Implementation Details


### Markdown Documentation
- Use clear section headers with `##` and `###`
- Include code blocks with language specifiers: ` ```bash` or ` ```yaml`
- Add step-by-step instructions with numbered or bulleted lists
- Include verification commands after each major step
- Add troubleshooting sections for common issues
- Use placeholders like `<VMID>`, `<MASTER_IP>`, `<TOKEN>` for values users must customize

### Shell Scripts (when creating)
- **Shebang**: Always use `#!/usr/bin/env bash` or `#!/bin/bash`
- **Error handling**: Use `set -euo pipefail` at the start
- **Style**: Follow Google Shell Style Guide
- **Comments**: Explain what commands do, especially complex ones
- **Variables**: Use UPPERCASE for constants, lowercase for local variables
- **Functions**: Document parameters and return values
- **Validation**: Check prerequisites before executing (e.g., command availability, root access)

### YAML Files (Kubernetes manifests)
- **Indentation**: Use 2 spaces (never tabs)
- **Naming**: Use kebab-case for resource names
- **Namespaces**: Explicitly specify namespace when not using default
- **Comments**: Add comments for non-obvious configurations
- **Structure**: Group related resources in the same file with `---` separator

### Code Organization Rules

1. **Location**: Place files in their logical directories:
   - Proxmox scripts → `1-proxmox/scripts/`
   - K3s manifests → `2-k3s/manifests/`
   - Docker Swarm stacks → `3-docker-swarm/stacks/`
   - Common utilities → `scripts/` (at root)

2. **Naming Conventions**:
   - Scripts: `verb-noun.sh` (e.g., `create-vm-template.sh`)
   - Manifests: `component-name.yaml` (e.g., `metallb-config.yaml`)
   - Docker Compose stacks: `docker-compose.yml` inside a named subdirectory
   - Documentation: `README.md` in each directory
   - Names should start with a number indicating order when necessary (e.g., `1-proxmox/01-something/01-script.sh`, `2-k3s/01-something/01-manifest.yaml`)

3. **Executable Permissions**: Scripts must be executable: `chmod +x script.sh`

## Important Constraints and Guidelines

### Security Requirements
- **Never commit secrets**: No passwords, API tokens, SSH private keys
- Use placeholders like `<YOUR_TOKEN>` or reference external secret management
- Add `.gitignore` entries for any credential files
- For Kubernetes secrets, show creation commands, not actual secret values
- Always use SSH keys for authentication, not passwords

### User Customization Required
When writing instructions or scripts, always make clear what needs to be customized:
- IP addresses (Proxmox hosts, VM IPs, MetalLB ranges)
- VMIDs (unique identifiers for VMs)
- Hostnames and DNS names
- Resource allocations (CPU, RAM, disk)
- Network configurations (bridges, VLANs)
- Storage pool names

### Verification Steps
After each major configuration step, provide verification commands:
```bash
# Example verification pattern
# Check Proxmox VM status
qm status <VMID>

# Check K3s node status
kubectl get nodes -o wide

# Verify service is running
systemctl status k3s
```

## Cluster Inventory

### K3s Cluster (Kubernetes)

| Role   | Hostname      | VMID | IP             | Host         | Storage           |
|--------|---------------|------|----------------|--------------|-------------------|
| Master | k3s-master-51 | 1051 | 192.168.10.51  | takaros      | iscsi-master-51   |
| Master | k3s-master-52 | 1052 | 192.168.10.52  | takaros      | iscsi-master-52   |
| Master | k3s-master-53 | 1053 | 192.168.10.53  | evanthoulaki | iscsi-master-53   |
| Worker | k3s-worker-61 | 1061 | 192.168.10.61  | takaros      | iscsi-worker-61   |
| Worker | k3s-worker-62 | 1062 | 192.168.10.62  | takaros      | iscsi-worker-62   |
| Worker | k3s-worker-63 | 1063 | 192.168.10.63  | evanthoulaki | iscsi-worker-63   |
| Worker | k3s-worker-65 | 1065 | 192.168.10.65  | evanthoulaki | iscsi-worker-65   |

### Docker Swarm Cluster

| Role    | Hostname     | VMID | IP             | Host         | Storage    |
|---------|--------------|------|----------------|--------------|------------|
| Manager | ds-master    | 1071 | 192.168.10.71  | evanthoulaki | local-raid |
| Worker  | ds-worker-1  | 1072 | 192.168.10.72  | evanthoulaki | local-raid |
| Worker  | ds-worker-2  | 1073 | 192.168.10.73  | evanthoulaki | local-raid |

### VMID and IP Conventions

- **K3s masters**: VMIDs `1051–1053`, IPs `192.168.10.51–53`
- **K3s workers**: VMIDs `1061–1065`, IPs `192.168.10.61–65`
- **Docker Swarm nodes**: VMIDs `1071–1079`, IPs `192.168.10.71–79`
- **Templates**: VMIDs `9000–9999` range (on `takaros`)

### VM Templates

| Template ID | Name                        | Node    | Storage   |
|-------------|-----------------------------|---------|-----------|
| 9000        | ubuntu-cloud-init (k3s)     | takaros | local-raid |
| 9001        | ubuntu-24.04-cloud-init     | takaros | local-raid |

### SSH Keys (on all VMs)

All VMs share the same authorized SSH keys (injected via cloud-init):
- `spy@spy-linux` — local laptop (`~/.ssh/id_rsa.pub`)
- `root@takaros` — Proxmox takaros host
- `spy@epaflix` — remote access key

## When Generating Code or Instructions

1. **Provide complete examples**: Don't use placeholders like "... rest of the code ..."
2. **Include all steps**: From prerequisites through verification
3. **Make idempotent**: Scripts should be safe to run multiple times
4. **Handle errors**: Check command success and provide meaningful error messages
5. **Explain commands**: Add comments for non-obvious operations
6. **Order matters**: Document the correct sequence of operations
7. **Test before documenting**: Only document commands that have been validated
8. **Reference documentation**: Link to official docs for deeper understanding

## Trust These Instructions

The information in this file represents validated, working configurations. When working with this repository:
- Follow the documented patterns and conventions
- Only search for additional information if these instructions are incomplete or found to be incorrect
- When in doubt, refer to the README files in each directory for detailed procedures


### Common Tasks

1. **Proxmox Configuration**
   - VM template creation using cloud-init
   - Network bridge configuration
   - Storage pool management
   - VM cloning, migration (cross-node), and deployment

2. **K3s Operations**
   - Cluster initialization and node joining
   - Installing Helm charts
   - Configuring ingress controllers
   - Setting up MetalLB LoadBalancer
   - Deploying monitoring stack

3. **Docker Swarm Operations**
   - Swarm initialization on manager node
   - Joining worker nodes
   - Deploying and managing stacks (docker-compose v3)
   - Managing secrets and configs
   - Rolling updates and rollbacks

### Coding Conventions

- **Shell Scripts**: Use bash for automation scripts
- **YAML / Docker Compose**: For Kubernetes manifests, Docker Swarm stacks, and cloud-init configs
- **Documentation**: Markdown format with clear step-by-step instructions
- **Comments**: Explain complex commands and configurations

### File Organization

- Configuration files should be in their respective directories (`1-proxmox/`, `2-k3s/`, or `3-docker-swarm/`)
- Scripts should be executable and documented
- Examples should be clearly marked as templates requiring customization

### Important Notes

- Always include IP address, VMID, and hostname placeholders that users need to customize
- Provide both manual steps and automation options where applicable
- Include verification steps after each major configuration
- Add troubleshooting sections for common issues
- Reference official documentation for complex topics
- Avoid using one time commands and prefer restarting the script or yaml file for repeatability and idempotency
- **Document commands and outputs**: Log significant commands, outputs, and troubleshooting sessions in `.history/` for future LLM reference and debugging (see [`.history/README.md`](.history/README.md) for format)

### Security Considerations

- Never commit sensitive data (passwords, tokens, keys)
- Use secrets management for Kubernetes (K3s secrets) and Docker Swarm (`docker secret`)
- Implement RBAC for cluster access
- Follow principle of least privilege
- Docker Swarm: use overlay networks to isolate service communication

## When Suggesting Code

- Provide complete, runnable examples
- Include error handling in scripts
- Add comments explaining complex operations
- Suggest verification commands
- Consider idempotency for automation scripts

## External Resources

- [Proxmox Documentation](https://pve.proxmox.com/wiki/Main_Page)
- [K3s Documentation](https://docs.k3s.io/)
- [k3sup Documentation](https://github.com/alexellis/k3sup)
- [Kubernetes Documentation](https://kubernetes.io/docs/)
- [Kube-vip Documentation](https://kube-vip.io/)
- [kube-vip-cloud-provider Documentation](https://kube-vip.io/docs/usage/cloud-provider/#install-the-kube-vip-cloud-provider)
- [Docker Swarm Documentation](https://docs.docker.com/engine/swarm/)
- [Docker Compose File v3 Reference](https://docs.docker.com/compose/compose-file/compose-file-v3/)
- [Docker Secrets Documentation](https://docs.docker.com/engine/swarm/secrets/)

# Tips

Try not to use sleep commands. Use commands that will give you the information you need to know when the system is ready. For example, instead of `sleep 15 && ping -c 3 192.168.10.51`, avoid using loops and sleep, and instead find if the VM is reacdy by checking the status of the VM or using a command that waits for the VM to be ready. This will make your scripts and commands more efficient and reliable.