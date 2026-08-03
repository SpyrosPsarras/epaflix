# Process: Deliver issue #130 — guard against unsupervised K3s minor jumps

## Goal
Deliver the three outcomes of [issue #130](https://github.com/SpyrosPsarras/epaflix/issues/130):
1. A **future-upgrade SOP** (extend `2-k3s/maintenance/system-upgrade/README.md`).
2. A **version-vs-channel DECISION** + guardrail (edit the Plans).
3. An **alert** for unsupervised minor jumps (add a PrometheusRule group to `custom-alerts.yaml`, routed to the existing Alertmanager email path).

## Methodology composition
- **evolutionary** — small reversible increments.
- **iac-implementation** — analyze → implement → validate → deploy gating for Kustomize/ArgoCD GitOps.
- **monitoring-setup** — PrometheusRule alert authoring against kube-state-metrics.
- **gsd/verify-work** — verify against the spec, zero scope creep.

## Phases
| # | Phase | Kind | Gate |
|---|-------|------|------|
| 1 | Analyze + plan + version-vs-channel recommendation | agent | — |
| — | **Decision breakpoint** (architecture decision) | breakpoint | owner |
| 2 | Implement (SOP doc, Plans change, alert) | agent | — |
| 3 | Validate (`kustomize build` plans + YAML lint) | shell | — |
| 4 | Verify vs issue #130 (score ≥90, no scope creep) | agent | refine loop → Phase 2 |
| — | **Integration breakpoint** (deploy / destructive-git) | breakpoint | owner |
| 5 | Integrate (branch from origin/main, commit, push, PR closes #130) | agent | — |
| 6 | Follow-ups (run PR test plan, open gh issues) | agent | — |

## Breakpoints (low tolerance: only critical decision + deploy)
1. **Version-vs-channel decision** — the policy choice is the heart of the issue.
2. **Integration** — branch/commit/push/PR is `deploy` + `destructive-git`.

## Guardrails honoured
- Path-scoped commits (never `git add -A`; no `.a5c/` or `.history/` scaffolding).
- Epaflix merge policy: branch + PR, rebase onto origin/main, `--force-with-lease`.
- No secrets / no plaintext `kind: Secret`.
- Follow-ups opened as `gh` issues using the repo enhancement shape.
