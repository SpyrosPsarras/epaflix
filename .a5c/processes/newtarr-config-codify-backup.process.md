# Process: newtarr config codify/backup (Epaflix issue #137)

## Goal
Deliver issue #137: make the newtarr v1.0.0 JSON integration config (Sonarr/Sonarr2/Radarr
connections + global hunt settings) survivable. Today it lives ONLY in the `newtarr-config`
local-path PVC — none of it is in git, so a fresh/empty PVC forces a manual rebuild (the cost
#131 exposed). The issue asks to **decide** between codifying into GitOps vs documented +
periodic backup, then implement it.

## Approach (composed from the process library)
- `devops-sre-platform/backup-restore-automation` — backup strategy + a proven restore path
- `devops-sre-platform/iac-implementation` — Kustomize/ArgoCD authoring with GitOps guardrails
- `gsd/plan-phase` + `gsd/verify-work` — atomic plan + verification loop
- `methodologies/evolutionary` — small, additive, reversible change to the servarr stack

## Phases
1. **Investigate (read-only)** — inventory live `/config`, detect embedded *arr API keys
   (decides whether codify needs SOPS), confirm the app writes `/config` at runtime, survey
   existing repo backup patterns + candidate targets (NFS / TrueNAS encrypted-backups dataset
   / backup PVC), record servarr ArgoCD app state. Output: facts + a recommended approach.
2. **Decision gate (owner breakpoint)** — choose **A) scheduled backup + restore doc** or
   **B) codify SOPS-encrypted seed + restore mechanism**. This is the issue's core decision.
3. **Plan** — atomic, file-level plan + PR test plan for the chosen approach.
4. **Implement + Validate (refine loop ×3)** — author files on a branch, SOPS-encrypt any
   secrets, wire `08.servarr/kustomization.yaml`, write a restore runbook, ONE local commit;
   then independently validate `kustomize build --enable-helm`, the no-plaintext-Secret guard,
   and plan coverage. Loops back on failure.
5. **Deploy gate (owner breakpoint)** — mandatory approval before push/merge.
6. **Publish + merge** — rebase onto origin/main, force-with-lease, PR, wait for `validate`,
   `gh pr merge --merge`. Merging is the deploy (servarr ArgoCD selfHeal reconciles).
7. **Post-merge verify** — app Synced/Healthy, new resource live, and the backup/restore path
   **proven** (trigger a manual backup run, or non-destructive seed-restore dry-run), then clean up.
8. **Closeout** — close #137 with outcome, tick the PR test plan **by editing the PR body**,
   cross-link #131/#135/#177, open follow-ups (extend pattern to other PVC-only servarr apps;
   re-snapshot/SOPS-rotation cadence if codify was chosen).

## Guardrails
- Never commit plaintext secrets — codify path uses `*.enc.yaml` (SOPS+age); pre-commit hook enforces.
- Two mandatory owner gates: the architecture decision and the deploy (profile `alwaysBreakOn: deploy`).
- All cluster reads/writes via `ssh ubuntu@192.168.10.51`. Merge per the Epaflix merge-commit policy.
