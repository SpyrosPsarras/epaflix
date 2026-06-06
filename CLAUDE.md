# CLAUDE.md

## Project

Infrastructure-as-code and documentation for a K3s Kubernetes cluster + Docker Swarm cluster on two Proxmox VE hosts. Documentation-heavy repo — shell scripts, YAML manifests, markdown guides.

## Critical Rules

- **NEVER** hardcode passwords, tokens, or secrets in any file. Use placeholders (`<POSTGRES_PASSWORD>`, `<CLOUDFLARE_API_TOKEN>`, etc.) and reference `.github/instructions/secrets.yml` (git-ignored).
- **NEVER** commit `secrets.yml`. It is git-ignored and must stay that way.
- Log significant commands and outputs to `.history/` for future reference.
- Repo path is `/home/spy/Documents/Epaflix/k3s-swarm-proxmox` — not `k3s-proxmox`.
- **Open a GitHub issue for every follow-up.** Any time work surfaces a step that has to happen later — soak-window flip, deferred cleanup, scope-cut spinoff, future migration, "out of scope of this PR" item — open a `gh issue` on `SpyrosPsarras/epaflix` for it before closing the thread. Don't park follow-ups in chat history, PR descriptions only, or local memory; the issue list is the durable shared queue. Use the existing enhancement-issue shape (`## Finding` / `## Current state` / `## Desired outcome` / `## Notes`) and cross-link related issues.
- **Execute PR test plans.** If a PR description contains a test plan / checklist (typically under `## Test plan` or `## Verification`), every unchecked box must be run, and the outcome recorded by editing the PR description itself (tick boxes, append result inline) — NEVER add a new PR comment. Applies to both open PRs (run before merge) and merged PRs where boxes were left unticked (run retroactively against live state). If a step is no longer applicable (e.g. soak window already elapsed, environment changed), strike it through and note why in the same description.
- **Encrypted Secret files use `.enc.yaml` suffix.** All Secrets that ArgoCD must reconcile live as `*.enc.yaml` next to their kustomization, encrypted with SOPS+age (single cluster recipient). Pre-commit hook (`.github/hooks/check-sops-encrypted.sh`) refuses any plaintext `kind: Secret` YAML. New clones must run `./.github/hooks/install-hooks.sh` once. Encrypt/rotate recipes: `.github/instructions/sops.instructions.md`.

## Cluster Inventory

### Proxmox Hosts
| Name         | IP            |
|--------------|---------------|
| takaros      | 192.168.10.10 |
| evanthoulaki | 192.168.10.11 |

### K3s Cluster (3 masters, 4 workers)
| Role   | Hostname      | VMID | External IP   | Internal IP | Host         |
|--------|---------------|------|---------------|-------------|--------------|
| Master | k3s-master-51 | 1051 | 192.168.10.51 | 10.0.0.51   | takaros      |
| Master | k3s-master-52 | 1052 | 192.168.10.52 | 10.0.0.52   | takaros      |
| Master | k3s-master-53 | 1053 | 192.168.10.53 | 10.0.0.53   | evanthoulaki |
| Worker | k3s-worker-61 | 1061 | 192.168.10.61 | 10.0.0.61   | takaros      |
| Worker | k3s-worker-62 | 1062 | 192.168.10.62 | 10.0.0.62   | takaros      |
| Worker | k3s-worker-63 | 1063 | 192.168.10.63 | 10.0.0.63   | evanthoulaki |
| Worker | k3s-worker-65 | 1065 | 192.168.10.65 | 10.0.0.65   | evanthoulaki |

### Docker Swarm (1 manager, 2 workers — all on evanthoulaki)
| Role    | Hostname    | VMID | IP            |
|---------|-------------|------|---------------|
| Manager | ds-master   | 1071 | 192.168.10.71 |
| Worker  | ds-worker-1 | 1072 | 192.168.10.72 |
| Worker  | ds-worker-2 | 1073 | 192.168.10.73 |

### Key IPs
| Service              | IP             |
|----------------------|----------------|
| K3s VIP (kube-vip)   | 192.168.10.100 |
| Traefik LB           | 192.168.10.101 |
| Pi-hole DNS          | 192.168.10.30  |
| TrueNAS              | 192.168.10.200 |
| Gateway              | 192.168.10.1   |

## Network

- **External** (vmbr0): 192.168.10.0/24 — internet, SSH, LoadBalancer, NFS, iSCSI
- **Internal** (vmbr1): 10.0.0.0/24 — flannel overlay, K3s inter-node
- Docker Swarm uses vmbr0 only

## DNS

Pi-hole at 192.168.10.30 is the sole DNS authority for `*.epaflix.com`.
- K3s services: `/etc/dnsmasq.d/10-epaflix.conf` — all `*.epaflix.com` → 192.168.10.101 (except `truenas` → 192.168.10.101 via Traefik proxy)
- User VMs: `/etc/dnsmasq.d/10-vm-epaflix.conf` — `*.vm.epaflix.com` for jumpbox access only (not K3s services)
- NXDOMAIN guard for `vm.epaflix.com.` in Unbound prevents accidental leak to public DNS
- **Golden rule**: edit dnsmasq.d files only, never Pi-hole web UI or custom.list
- The `.epavli` and `.internal.epaflix.com` internal domains no longer exist — all K3s services use `*.epaflix.com` with Let's Encrypt

## SSH

All VMs: `ssh ubuntu@192.168.10.XX` — passwordless via SSH keys.
Proxmox hosts: `ssh root@192.168.10.{10,11}`.
TrueNAS: `ssh truenas_admin@192.168.10.200`.

## Storage

- K3s VM disks: TrueNAS iSCSI targets (iscsi-master-51, iscsi-worker-61, etc.)
- Docker Swarm VM disks: `local-raid` on evanthoulaki
- NFS media: `/mnt/pool1/dataset01/{animes,downloads,movies,tvshows}`
- App configs: `local-path` PVCs on worker nodes (not NFS)

## Directory Guide

```
0-truenas/          # TrueNAS iSCSI + NFS setup
1-proxmox/          # Proxmox host config, VM creation, user VMs
2-k3s/              # K3s cluster — numbered subdirs (01-10) in deploy order
3-docker-swarm/     # Docker Swarm cluster + stack definitions
.github/instructions/  # Domain-specific AI instruction files + secrets.yml
.history/           # Command logs (git-ignored content, tracked .md/.sh)
raid-migration/     # Proxmox RAID migration guides
```

## Conventions

- VMIDs: masters 1051-1053, workers 1061-1065, swarm 1071-1073, templates 9000+
- IPs mirror VMIDs: VMID 1051 → .51, VMID 1071 → .71
- K3s subdirs numbered in deploy order: `01.kube-vip/`, `02.cert-manager/`, etc.
- Stack compose files: `3-docker-swarm/stacks/<name>/docker-compose.yml`
- Placeholders for secrets: `<POSTGRES_PASSWORD>`, `<AUTHENTIK_DB_PASSWORD>`, `<CLOUDFLARE_API_TOKEN>`, `<SMTP_PASSWORD>`, `<TRUENAS_PASSWORD>`

## Detailed Instructions

Domain-specific guidance lives in `.github/instructions/`:
- `proxmox.instructions.md` — VM management, iSCSI, cloud-init, console access
- `k3s.instructions.md` — k3sup commands, etcd config, add-on install order, netplan
- `docker-swarm.instructions.md` — VM provisioning, swarm ops, stack patterns
- `truenas.instructions.md` — SSH access, midclt commands, NFS/iSCSI management
- `pihole.instructions.md` — DNS architecture, record management, Unbound config
- `general.instructions.md` — Security rules, history logging format

## Babysitter

Babysitter orchestrates complex multi-step workflows; project profile lives at `.a5c/project-profile.json` (git-ignored, version 1, semi-autonomous / low breakpoint tolerance — break only on destructive / deploy / architecture / secrets-rotation steps).

### Commands

- `/babysitter:call` — orchestrate a run
- `/babysitter:plan` — plan only, no execution
- `/babysitter:yolo` — non-interactive run (no breakpoints)
- `/babysitter:resume` — resume an interrupted run
- `/babysitter:project-install` — re-run project onboarding
- Runs live under `.a5c/runs/` (git-ignored).

### Recommended Methodology

Evolutionary — mature IaC repo with no unit tests, so favor small reversible increments (adopt → soak → selfHeal-flip). Complemented by self-assessment + spec-driven processes for change gating.

### Recommended Processes

| Process path | When to use |
|--------------|-------------|
| `methodologies/gsd/map-codebase` | Onboard to the numbered `2-k3s` deploy-order + app-of-apps layout |
| `methodologies/gsd/plan-phase` + `execute-phase` + `verify-work` | Gated change workflow — verify = ArgoCD Synced/Healthy, zero live drift (compensates for no unit tests) |
| `methodologies/gsd/iterative-convergence` + `audit-milestone` | Model adopt → soak → selfHeal-flip pairs and periodic drift audits |
| `specializations/devops-sre-platform/iac-implementation` | Author/change Kustomize + Helm + ArgoCD manifests with GitOps guardrails |
| `specializations/devops-sre-platform/secrets-management` | Drives issue #29; preserve SOPS+age `*.enc.yaml` + pre-commit guard |
| `specializations/devops-sre-platform/incident-response` | Runbooks for recurring firefighting (Postgres sequence drift, servarr import races, qbittorrent VPN flap) |
| `specializations/devops-sre-platform/security-scanning` + `iac-testing` | Gate image-updater promotions; `kustomize build` / `helm template` validation |

### Project Guardrails for Babysitter

- Respect the **merge-commit + mandatory-rebase (semi-linear) + PR policy** (see Critical Rules / Epaflix merge policy). Every commit on main arrives via a branch+PR as a `Merge pull request #N` marker. Before merging: rebase the branch onto `origin/main` and `push --force-with-lease` (the required `validate` check + strict up-to-date block stale branches). Merge with `gh pr merge <n> --merge`.
- **Open a `gh issue` on `SpyrosPsarras/epaflix` for every follow-up** before closing the thread (see Critical Rules).
- **Never commit secrets** or plaintext `*.enc.yaml` — pre-commit hook refuses plaintext `kind: Secret` (see Critical Rules).
- **ArgoCD adoption order**: push aligned git BEFORE creating an Application, otherwise automated sync reverts live to pre-adoption main.

### CI/CD

- Babysitter CI integration is **not configured** — no `ANTHROPIC_API_KEY` is available.
- To enable later: add an `ANTHROPIC_API_KEY` (or wire `CLAUDE_CODE_OAUTH_TOKEN` from a Claude subscription) and re-run `/babysitter:project-install`, or add a fork-guarded workflow that does NOT touch the existing secret-free `validate` gate.
