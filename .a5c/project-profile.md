# Project Profile: k3s-swarm-proxmox

Epaflix homelab Infrastructure-as-Code monorepo: a GitOps-managed K3s Kubernetes cluster (3 masters, 4 workers) plus a Docker Swarm cluster (1 manager, 2 workers) running on two Proxmox VE hosts. Declarative state lives in Git and is reconciled by ArgoCD (app-of-apps); manifests are assembled with Kustomize (Helm inflated via the helmCharts generator), and SOPS/age-encrypted Secrets (*.enc.yaml) are decrypted at render time by KSOPS on argocd-repo-server. Documentation-heavy: shell bootstrap scripts, YAML manifests, and markdown runbooks are all first-class.

> Last updated: 2026-05-30T19:08:00Z | Version: 1

## Goals

- **infrastructure** [high]: Keep the Epaflix k3s-swarm-proxmox cluster healthy and fully GitOps-managed via ArgoCD (active)
- **infrastructure** [high]: Complete ArgoCD adoption and the selfHeal/prune rollout across all Applications (per-App, post-48h-soak) (active)
- **infrastructure** [high]: Maintain reliable Postgres backups via the CNPG Barman Cloud Plugin (ObjectStore CR, v0.12.0) (active)
- **security** [high]: Land secret automation (issue #29) to drop per-Application Secret carve-outs; keep all reconciled Secrets SOPS/age-encrypted *.enc.yaml with the pre-commit guard (active)
- **reliability** [medium]: Reduce recurring app firefighting (Postgres sequence drift, servarr import races, qbittorrent VPN flapping, image-pin breakage) (active)
- **documentation** [medium]: Keep documentation-first hygiene: docs() commits record design decisions; CLAUDE.md and domain instruction files stay authoritative (active)

## Tech Stack

### Languages

- YAML (Primary declarative (~1120 files): k8s manifests, Kustomize overlays, Helm values, ArgoCD Applications, CRs, compose, CI/Renovate config)
- Shell (bash) (~37 scripts: per-stack bootstrap/deploy, DB health/maintenance, backups, git hooks; all set -euo pipefail)
- Markdown (~127 docs: CLAUDE.md, READMEs, runbooks, .github/instructions/*)
- Go/.tpl (Vendored Helm chart files only, not project source)

### Frameworks

- K3s
- ArgoCD
- Kustomize
- Helm
- SOPS+age+KSOPS
- cert-manager
- kube-vip / cloud-provider
- Traefik
- CloudNativePG
- Authentik
- kube-prometheus-stack / Loki / Promtail / Alertmanager
- Renovate + system-upgrade-controller
- Docker Swarm / Proxmox VE / TrueNAS

### Databases

- PostgreSQL (CNPG)
- Redis
- SQLite

### Infrastructure

- Proxmox VE cluster
- Networking
- DNS
- Storage
- Secrets/PKI

**Build tools:** kustomize build, helm pull/template, sops, ksops, GitHub Actions CI

**Package managers:** Helm, Renovate, no app-level package manager (IaC repo)

## Architecture

**Pattern:** GitOps Infrastructure-as-Code monorepo: declarative state in Git reconciled by ArgoCD (app-of-apps), manifests assembled with Kustomize (Helm inflated via helmCharts generator), SOPS/age-encrypted Secrets decrypted at render time by KSOPS on argocd-repo-server; imperative shell scripts for one-time bootstrap; markdown docs are first-class.
**Data flow:** Edit manifests on branch -> PR (CI validates YAML/Helm pins/kustomize build without age key) -> merge to main -> ArgoCD app-of-apps syncs each child Application path into its namespace; argocd-repo-server renders kustomize, inflates Helm, KSOPS decrypts *.enc.yaml with cluster age key at render time; argocd-image-updater resolves floating tags to digests and git-writes back. Most Apps selfHeal=true prune=false; app-of-apps and ArgoCD self-management stay manual.

### Modules

| Module | Path | Description |
|--------|------|-------------|
| 0-truenas | `undefined` |  |
| 1-proxmox | `undefined` |  |
| 2-k3s | `undefined` |  |
| 2-k3s/06.postgres | `undefined` |  |
| 2-k3s/08.servarr | `undefined` |  |
| 2-k3s/11.argocd | `undefined` |  |
| 3-docker-swarm | `undefined` |  |
| .github/instructions | `undefined` |  |
| .github/hooks+workflows | `undefined` |  |

**Entry points:** `2-k3s/11.argocd/apps/app-of-apps.yaml (ArgoCD root Application)`, `2-k3s/11.argocd/apps/kustomization.yaml (enumerates child Applications)`, `2-k3s/11.argocd/install.sh + image-updater/install.sh (bootstrap)`, `Numbered deploy-order stacks 2-k3s/01.* through 12.*`, `.github/hooks/install-hooks.sh (one-shot hook install)`, `README.md / per-directory READMEs`

## Team

- **Spyros Psarras** (Sole maintainer / Senior Platform & Infrastructure Engineer)

## Workflows

### development

Branch off main, edit manifests/docs, open a PR. CI 'validate' (secret-free, fork-guarded) parses YAML, resolves Helm pins, and kustomize-builds sops-free dirs. Merge-commit only with admin bypass (0 approvals). After merge, in-cluster ArgoCD reconciles. No unit tests (IaC); verification is CI + per-app DB health scripts + ArgoCD Synced+Healthy + 48h soak.
**Triggers:** pull_request, push to main

1. Branch off main (SpyrosPsarras/<slug>)
2. Edit manifests/docs; record design in docs() commit
3. Open PR; ci.yml validate runs
4. gh pr merge --admin --merge
5. ArgoCD reconciles to live

### GitOps deploy (ArgoCD adoption -> selfHeal flip)

Adoption order matters: push git aligned with live state BEFORE creating the Application, otherwise automated sync reverts live to pre-adoption main (cost filebrowser its OIDC Secret on 2026-05-24). First sync manual; soak ~48h; then flip selfHeal in a dedicated PR. prune stays false (rolled out per-App post-soak, authentik first); app-of-apps and ArgoCD self-management selfHeal stay manual permanently (PR #105).
**Triggers:** adopting or modifying an ArgoCD Application

1. Push aligned git matching live state
2. Create child Application (manual sync first)
3. Soak ~48h, confirm Synced+Healthy
4. Flip selfHeal in a separate PR
5. Roll out prune per-App post-soak

### Merge-commit-only PR policy with admin bypass

Mirrors IndicoSystems policy: merge-commit only, PR required (0 approvals), admin bypass. Every change including bot PRs goes through a PR.
**Triggers:** any change to main

1. Branch off main
2. Open PR
3. gh pr merge --admin --merge

### SOPS encrypt/rotate flow

Secrets live as *.enc.yaml encrypted with SOPS+age (single cluster recipient); KSOPS decrypts via argocd/sops-age at render time. Pre-commit hook hard-fails any plaintext kind:Secret; new clones run install-hooks.sh once.
**Triggers:** adding or rotating a Secret

1. sops -e -i then mv to .enc.yaml
2. pre-commit hook blocks plaintext kind:Secret
3. new clones run .github/hooks/install-hooks.sh

### Follow-up = GitHub issue

Every deferred/next-PR/future-migration item gets a gh issue on SpyrosPsarras/epaflix using the Finding / Current state / Desired outcome / Notes shape; cross-link related issues. PRs close issues by number. Record PR test plans by editing the PR body, never a new comment.
**Triggers:** work surfaces a later step

1. Identify deferred work
2. gh issue create (Finding/Current state/Desired outcome/Notes)
3. cross-link related issues
4. PR closes by number

### Renovate dependency updates

Renovate (in-cluster + .github/renovate.json) opens chart/image bump PRs on a 02:00-06:00 Athens schedule (limit 4/hr, 6 concurrent). platformAutomerge is inert because validate is not a required check; Authentik is never auto-merged. argocd-image-updater digest-pins floating tags with manual last-good rollback when an upstream build is broken.
**Triggers:** scheduled nightly, new upstream chart/image versions

1. Scan 02:00-06:00 Athens
2. Open PR (limit 4/hr, 6 concurrent)
3. CI validate
4. manual merge (Authentik never auto-merged)

## Tools

### Linting

- YAML parse check (`.github/workflows/ci.yml`)
- SOPS encryption pre-commit hook

### Testing

- Helm chart pins resolve (`.github/workflows/ci.yml`)
- Kustomize build (sops-free dirs) (`.github/workflows/ci.yml`)

## Services

- **ArgoCD** (GitOps CD) - argocd.epaflix.com
- **ArgoCD Image Updater** (Image auto-update controller)
- **Renovate** (Dependency bot (in-cluster + .github/renovate.json))
- **Traefik (K3s)** (Ingress/LB) - traefik.epaflix.com (192.168.10.101)
- **Traefik (Swarm)** (Reverse proxy v3.7.1) - ds-master 192.168.10.71
- **cert-manager** (TLS mgmt (jetstack v1.20.2))
- **CloudNativePG** (Postgres operator + Barman backups)
- **Authentik** (SSO/IdP OIDC (chart 2026.5.2)) - authentik.epaflix.com
- **kube-prometheus-stack** (Observability) - grafana.epaflix.com
- **Loki+Promtail** (Log aggregation)
- **kube-vip+cloud-provider** (VIP + LB IP allocation) - 192.168.10.100
- **Pi-hole** (DNS authority) - 192.168.10.30
- **Servarr stack** (Media automation)
- **filebrowser** (Web file manager)
- **system-upgrade-controller** (K3s node upgrades)
- **TrueNAS** (Storage + SOPS age key mirror) - 192.168.10.200
- **Proxmox VE** (Hypervisor) - 192.168.10.10/.11
- **PBS** (Backup target (epaflix-pbs))

## CI/CD

**Provider:** GitHub Actions
**Config files:** `.github/workflows/ci.yml`, `.github/workflows/README.md`

### Pipelines

- **ci**

## Pain Points

- **medium** [gitops]: Soak windows serialize rollout: ~48h before a selfHeal flip means changes arrive as adopt->flip PR pairs, bounding throughput
- **high** [gitops]: ArgoCD adoption-order footgun: git out-of-sync with live before Application creation reverts live state (cost filebrowser its OIDC Secret 2026-05-24)
- **medium** [reliability]: Recurring app firefighting: Postgres sequence drift, servarr import races (Sonarr2/Huntarr), qbittorrent AirVPN tunnel flapping
- **medium** [automation]: Image-pin breakage: argocd-image-updater can digest-pin a broken upstream build, requiring manual last-good rollback
- **low** [automation]: Renovate vs argocd-image-updater overlap had to be untangled to avoid duplicate/competing bumps
- **low** [postgres]: CNPG webhook/plugin convergence churn (e.g. needing explicit plugins[].enabled=true declaration, #104)

## Bottlenecks

- Soak windows serialize rollout (~48h before selfHeal flip; changes arrive as adopt+flip PR pairs) at rollout
  Impact: undefined
- Single maintainer; throughput bounded by bursty marathon sessions at team
  Impact: undefined
- ArgoCD adoption-order fragility: git out-of-sync with live before Application creation causes reverts at 2-k3s/11.argocd/apps
  Impact: undefined
- Recurring app firefighting concentrated in servarr/postgres/qbittorrent (sequence drift, import races, VPN flapping) at 2-k3s/08.servarr
  Impact: undefined
- Image-pin breakage: image-updater can pin a broken upstream build, needs manual last-good rollback at 2-k3s/11.argocd/image-updater
  Impact: undefined
- CNPG webhook/plugin convergence churn (plugins[].enabled explicit declaration #104) at 2-k3s/06.postgres
  Impact: undefined

## Conventions

### Naming

- **apps:** app-<name>.yaml under 11.argocd/apps; per-app subdir with kustomization.yaml
- **deployOrder:** K3s add-on stacks numbered 01.kube-vip .. 12.renovate
- **encryptedSecrets:** .enc.yaml suffix next to its kustomization
- **secretPlaceholders:** <POSTGRES_PASSWORD>, <AUTHENTIK_DB_PASSWORD>, <CLOUDFLARE_API_TOKEN>, <SMTP_PASSWORD>, <TRUENAS_PASSWORD>
- **stacks:** 3-docker-swarm/stacks/<name>/docker-compose.yml
- **topLevel:** 0-/1-/2-/3- prefixed areas
- **vmidIpMirror:** masters 1051-1053, workers 1061-1065, swarm 1071-1073, templates 9000+; last octet mirrors VMID

### Git

- **branching:** Branch off main (SpyrosPsarras/<slug>); never commit secrets.yml or .gitignore-matched files (git add -f retired)
- **commitStyle:** Conventional Commits type(scope): subject; scope = affected component dir; append issue number e.g. (#10); bots use [renovate]/[image-updater] prefixes
- **commitTrailer:** Co-Authored-By trailer
- **followUps:** Every deferred item gets a gh issue on SpyrosPsarras/epaflix using Finding/Current state/Desired outcome/Notes shape
- **mergePolicy:** Merge-commit only, PR required (0 approvals), admin bypass (gh pr merge --admin --merge)
- **prTestPlans:** Execute every PR test-plan box; record by editing PR body, never a new comment
- **repoUrl:** SpyrosPsarras/epaflix.git; ArgoCD targetRevision main

**Error handling:** Bash fail-fast (set -euo pipefail); pre-commit hook hard-fails plaintext kind:Secret; CI fails on bad YAML/Helm pins/kustomize; VPN/network-config failsafe: build the recovery path first, change client before server

**Testing:** No unit tests (IaC). Verification = secret-free GitHub Actions CI (YAML parse, Helm pin resolve, kustomize build of sops-free dirs), pre-commit SOPS guard, per-app DB health scripts, postgres-sequence-audit CronJob, ArgoCD Synced+Healthy + 48h soak

### Additional Rules

- NEVER hardcode secrets; use placeholders + git-ignored secrets.yml
- Encrypted Secrets use .enc.yaml; new clones run install-hooks.sh once
- Log significant commands to .history/ (content git-ignored; no force-add)
- DNS golden rule: edit dnsmasq.d files only, never Pi-hole UI/custom.list
- Repo path is k3s-swarm-proxmox, not k3s-proxmox
- Cloudflare proxied wildcard hijacks undefined subs; LAN-only services need a shadow A -> 192.168.10.101 DNS-only record

## Repositories

- **epaflix** - https://github.com/SpyrosPsarras/epaflix

## CLAUDE.md Instructions

- NEVER hardcode passwords, tokens, or secrets in any file; use placeholders (<POSTGRES_PASSWORD>, <CLOUDFLARE_API_TOKEN>, etc.) and reference the git-ignored .github/instructions/secrets.yml
- NEVER commit secrets.yml; it is git-ignored and must stay that way
- Encrypted Secret files use the .enc.yaml suffix, encrypted with SOPS+age (single cluster recipient); the pre-commit hook (.github/hooks/check-sops-encrypted.sh) refuses any plaintext kind:Secret YAML; new clones run ./.github/hooks/install-hooks.sh once
- Open a GitHub issue for every follow-up on SpyrosPsarras/epaflix using the Finding / Current state / Desired outcome / Notes shape before closing the thread; cross-link related issues
- Merge-commit only, PR required (0 approvals), admin bypass (gh pr merge --admin --merge)
- K3s add-on subdirectories are numbered in deploy order (01.kube-vip .. 12.renovate)
- DNS golden rule: edit dnsmasq.d files only, never the Pi-hole web UI or custom.list
- Log significant commands and outputs to .history/ (content git-ignored; no git add -f of gitignored files)
- Repo path is /home/spy/Documents/Epaflix/k3s-swarm-proxmox — not k3s-proxmox
- Domain-specific guidance lives in .github/instructions/ (proxmox, k3s, docker-swarm, truenas, pihole, sops, general)

## Installed Extensions

- Skills: specializations/devops-sre-platform/skills/gitops, specializations/devops-sre-platform/skills/kubernetes-ops, specializations/devops-sre-platform/skills/secrets-management, specializations/devops-sre-platform/skills/helm-charts
- Agents: specializations/devops-sre-platform/agents/platform-engineer, specializations/devops-sre-platform/agents/sre-expert, specializations/devops-sre-platform/agents/secops-expert, specializations/devops-sre-platform/agents/kubernetes-expert
- Processes: methodologies/gsd/map-codebase.js, methodologies/gsd/plan-phase.js, methodologies/gsd/execute-phase.js, methodologies/gsd/verify-work.js, methodologies/gsd/audit-milestone.js, methodologies/gsd/iterative-convergence.js, specializations/devops-sre-platform/iac-implementation.js, specializations/devops-sre-platform/secrets-management.js, specializations/devops-sre-platform/incident-response.js, specializations/devops-sre-platform/security-scanning.js, specializations/devops-sre-platform/iac-testing.js
