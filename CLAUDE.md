# CLAUDE.md

## Project

Infrastructure-as-code and documentation for a K3s Kubernetes cluster + Docker Swarm cluster on two Proxmox VE hosts. Documentation-heavy repo — shell scripts, YAML manifests, markdown guides.

## Critical Rules

- **NEVER** hardcode passwords, tokens, or secrets in any file. Use placeholders (`<POSTGRES_PASSWORD>`, `<CLOUDFLARE_API_TOKEN>`, etc.) and reference a key name in `.github/instructions/secrets.enc.yaml` (SOPS+age encrypted, committed).
- **The credential store is `.github/instructions/secrets.enc.yaml`.** The plaintext `secrets.yml` is GONE — `secrets.yml` stays in `.gitignore` as a guard so it can never come back. Read one key with `sops -d --extract '["key"]' .github/instructions/secrets.enc.yaml`, never decrypt the whole file for one value, never echo the value. Recipes: `.github/instructions/sops.instructions.md` → "The credential store".
- Log significant commands and outputs to `.history/` for future reference.
- Repo path is `/home/spy/Documents/Epaflix/k3s-swarm-proxmox` — not `k3s-proxmox`.
- **Open a GitHub issue for every follow-up.** Any time work surfaces a step that has to happen later — soak-window flip, deferred cleanup, scope-cut spinoff, future migration, "out of scope of this PR" item — open a `gh issue` on `SpyrosPsarras/epaflix` for it before closing the thread. Don't park follow-ups in chat history, PR descriptions only, or local memory; the issue list is the durable shared queue. Use the existing enhancement-issue shape (`## Finding` / `## Current state` / `## Desired outcome` / `## Notes`) and cross-link related issues.
- **Execute PR test plans.** If a PR description contains a test plan / checklist (typically under `## Test plan` or `## Verification`), every unchecked box must be run, and the outcome recorded by editing the PR description itself (tick boxes, append result inline) — NEVER add a new PR comment. Applies to both open PRs (run before merge) and merged PRs where boxes were left unticked (run retroactively against live state). If a step is no longer applicable (e.g. soak window already elapsed, environment changed), strike it through and note why in the same description. Post-merge boxes (soak windows, next-reboot checks) must not stay unchecked forever: at merge time either run them or tick them with "tracked in #NNN" and open that issue — an unchecked box with no issue is a lost follow-up (24 merged PRs accumulated exactly this).
- **Encrypted Secret files use `.enc.yaml` suffix.** All Secrets that ArgoCD must reconcile live as `*.enc.yaml` next to their kustomization, encrypted with SOPS+age (single cluster recipient). The pre-commit hook (`.github/hooks/check-sops-encrypted.sh`) rejects plaintext credential values but permits content-validated placeholder templates; CI runs the same guard over all tracked YAML. New clones must run `./.github/hooks/install-hooks.sh` once. Encrypt/rotate and placeholder rules: `.github/instructions/sops.instructions.md`.
- **Merge policy: merge-commit + mandatory rebase (semi-linear).** Every commit on main arrives via a branch+PR as a `Merge pull request #N` marker. Before merging: rebase the branch onto `origin/main` and `push --force-with-lease` (the required `validate` check + strict up-to-date block stale branches), wait for `validate`, then `gh pr merge <n> --merge`.
- **ArgoCD adoption order: push aligned git BEFORE creating an Application.** Otherwise automated sync reverts live to pre-adoption main.
- **A live-only fix is not a fix.** Any config value fixed live on a PVC or an app's own DB must be codified the same day — SOPS-seed Secret + non-clobber initContainer (the #137/#138 pattern), or at minimum documented as required values in the app's directory. Otherwise the next rebuild silently reverts it (#465, #299, #518, #538).
- **Close soak/flip issues only with the live value pasted.** Never close a "flip prune/selfHeal after soak" (or any config-flip) issue on "soak elapsed" — paste the literal current value (`grep` the manifest AND `kubectl get application ... -o jsonpath`). #50 was closed as done while `app-servarr.yaml` still said `prune: false` (#551). Same discipline for runbooks: verify against source code or live state before writing "X handles Y" (#614).
- **Never extract a secret with a pattern that can echo the value.** No `grep 'key:'` against decrypted SOPS output, and never `cat`/`head` a diff or file that contains plaintext secrets — the matched line lands in the retained transcript (#602 forced a token rotation). Extract the single value into a shell variable with `sops -d --extract '["key"]'` and never print it; print `${#VAR}` if you need to check it.
- **Before destroying a snapshot/PVC/dataset, grep open issues for its name.** Confirm any referencing issue's stated gates are met first — #515 destroyed a rollback target that an open issue still named; root-cause anomalies (e.g. a path-match gap) before bulk deletes (#609).
- **Every cluster command names `--context epaflix` explicitly.** `~/.kube/config` is Syncthing-synced and holds work AKS contexts alongside the homelab one, including production ones. The active context can change under a running session when another machine syncs, so `kubectl config use-context` at the start is not protection - it is a value that something else can overwrite. Write `kubectl --context epaflix ...` (and `helm --kube-context epaflix ...`) in runbooks, scripts and one-off commands. Before any destructive command, confirm with `kubectl config current-context`. A homelab session already landed on a work cluster context once this way. Never name the employer, its clusters or its customers anywhere in this repo - it is public.

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

**Status: the Swarm has been down since ~2026-06** — VMs stopped, `registry`/`swarm-api`/`traefik` services stuck Pending (#583). Treat all swarm docs as dormant until that issue resolves; do not assume a live second cluster.

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
2-k3s/              # K3s cluster — numbered subdirs (01-15) in deploy order + maintenance/
3-docker-swarm/     # Docker Swarm cluster + stacks (dormant, see #583)
docs/               # Design docs — docs/superpowers/{plans,specs} hold spec-driven feature designs
artifacts/          # Per-issue triage / feature working notes (git-ignored scratch, see general.instructions.md, #662)
backups/            # Local backups (git-ignored)
images/             # Documentation images
raid-migration/     # Proxmox RAID migration guides
.github/instructions/  # Domain-specific AI instruction files + secrets.enc.yaml (SOPS-encrypted credential store)
.history/           # Command logs (git-ignored content, tracked .md/.sh)
```

## Conventions

- VMIDs: masters 1051-1053, workers 1061-1065, swarm 1071-1073, templates 9000+
- IPs mirror VMIDs: VMID 1051 → .51, VMID 1071 → .71
- K3s subdirs numbered in deploy order: `01.kube-vip/`, `02.cert-manager/`, etc.
- Stack compose files: `3-docker-swarm/stacks/<name>/docker-compose.yml`
- Placeholders for secrets: `<POSTGRES_PASSWORD>`, `<AUTHENTIK_DB_PASSWORD>`, `<CLOUDFLARE_API_TOKEN>`, `<SMTP_PASSWORD>`, `<TRUENAS_PASSWORD>`
- VM CPU type: standardize on `cpu: host` — see proxmox.instructions.md (#216)
- PR/commit titles: plain conventional-commit style, NO `(#issue)` embedded in the title — GitHub appends `(#PR)` at merge and produces confusing `(#X) (#Y)` doubles in git log. Put `Closes #NNN` in the PR body instead.

## Detailed Instructions

Domain-specific guidance lives in `.github/instructions/`:
- `proxmox.instructions.md` — VM management, iSCSI, cloud-init, console access
- `k3s.instructions.md` — k3sup commands, etcd/config.yaml invariants, netplan, per-subsystem map
- `docker-swarm.instructions.md` — VM provisioning, swarm ops, stack patterns
- `truenas.instructions.md` — SSH access, midclt commands, NFS/iSCSI management
- `pihole.instructions.md` — DNS architecture, record management, Unbound config
- `general.instructions.md` — Security rules, history logging format
- `babysitter.instructions.md` — methodology, recommended processes, CI/CD setup

## Babysitter

Babysitter orchestrates complex multi-step workflows; project profile lives at `.a5c/project-profile.json`, process definitions under `.a5c/processes/`, runs under `.a5c/runs/`. **The whole `.a5c/` tree is git-ignored** — it is local scaffolding, never committed, so nothing under it survives a fresh clone. Anything worth keeping goes to `docs/` or `.github/instructions/`. Profile is version 1, semi-autonomous / low breakpoint tolerance — break only on destructive / deploy / architecture / secrets-rotation steps. Guardrails are in `## Critical Rules` above; methodology and process selection are in `.github/instructions/babysitter.instructions.md`.
