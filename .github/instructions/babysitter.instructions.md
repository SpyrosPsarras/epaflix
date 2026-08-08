# Babysitter instructions

Babysitter orchestrates complex multi-step workflows in this repo. The project profile lives at `.a5c/project-profile.json` (version 1, semi-autonomous / low breakpoint tolerance - break only on destructive / deploy / architecture / secrets-rotation steps). Process definitions live under `.a5c/processes/`, runs under `.a5c/runs/`.

The **entire `.a5c/` tree is git-ignored** - it is local scaffolding, not repo content. Do not try to commit any of it, and do not treat a process definition as a durable record: a fresh clone has none of them. If a run produces something worth keeping (a runbook, a decision, a recipe), write it to `docs/` or `.github/instructions/` as part of the same PR.

Guardrails are in the root `CLAUDE.md` under `## Critical Rules` - merge policy, follow-up issues, no plaintext Secrets, ArgoCD adoption order. This file covers methodology and process selection only.

## Recommended Methodology

Evolutionary — mature IaC repo with no unit tests, so favor small reversible increments (adopt → soak → selfHeal-flip). Complemented by self-assessment + spec-driven processes for change gating.

## Recommended Processes

| Process path | When to use |
|--------------|-------------|
| `methodologies/gsd/map-codebase` | Onboard to the numbered `2-k3s` deploy-order + app-of-apps layout |
| `methodologies/gsd/plan-phase` + `execute-phase` + `verify-work` | Gated change workflow — verify = ArgoCD Synced/Healthy, zero live drift (compensates for no unit tests) |
| `methodologies/gsd/iterative-convergence` + `audit-milestone` | Model adopt → soak → selfHeal-flip pairs and periodic drift audits |
| `specializations/devops-sre-platform/iac-implementation` | Author/change Kustomize + Helm + ArgoCD manifests with GitOps guardrails |
| `specializations/devops-sre-platform/secrets-management` | Drives issue #29; preserve SOPS+age `*.enc.yaml` + pre-commit guard |
| `specializations/devops-sre-platform/incident-response` | Runbooks for recurring firefighting (Postgres sequence drift, servarr import races, qbittorrent VPN flap) |
| `specializations/devops-sre-platform/security-scanning` + `iac-testing` | Gate image-updater promotions; `kustomize build` / `helm template` validation |

## CI/CD

- Babysitter CI integration is **not configured** — no `ANTHROPIC_API_KEY` is available.
- To enable later: add an `ANTHROPIC_API_KEY` (or wire `CLAUDE_CODE_OAUTH_TOKEN` from a Claude subscription) and re-run `/babysitter:project-install`, or add a fork-guarded workflow that does NOT touch the existing secret-free `validate` gate.
