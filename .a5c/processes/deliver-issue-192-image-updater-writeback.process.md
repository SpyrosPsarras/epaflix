# Process: deliver issue #192 — consolidate digest automation on Renovate

## Goal
ArgoCD Image Updater's git write-back to `main` is rejected on every poll by branch protection
(required `validate` check). Digest automation is silently broken for `servarr` (and `authentik`
has the same latent breakage). Renovate digest PRs became the fallback but are not automerged, so
they pile up and need manual rebase+merge.

Owner goal this run: **all apps auto-update, no manual branch rebasing.**

## Chosen path (issue Option 3) — reversible increment
1. Renovate `packageRule` to **automerge `digest` updates** for the servarr kustomization's docker images.
2. Global `rebaseWhen: behind-base-branch` so Renovate branches auto-rebase (no manual rebasing).
3. Remove broken Image Updater annotations from `app-servarr.yaml` **and** `app-authentik.yaml`
   (authentik's DB-migration manual-merge gate is **preserved** — it stays Renovate-owned).
4. Fix stale comments.
5. **Defer** decommissioning the now-idle Image Updater install to a follow-up issue (destructive deploy → after soak).

## Phases
1. **Design** (read-only) → **DESIGN gate** (owner): Option-3 decision, matcher, rebase knob, authentik scope, decommission defer.
2. **Implement + Validate loop**: edits on a fresh branch; gate = `renovate-config-validator` valid, manifests render, scope correct, authentik gate intact.
3. **Adversarial review loop**: matcher catches every servarr digest image; no over-match (authentik major stays manual); annotation removal benign for ArgoCD.
4. **Finalize**: push, open PR (`Closes #192`), tick pre-merge test-plan boxes, open follow-ups.
5. **DEPLOY/MERGE gate** (owner; alwaysBreakOn deploy + destructive-git).
6. **Merge + verify**: rebase → wait for `validate` → `gh pr merge --merge`; confirm ArgoCD Synced/Healthy + Image Updater push loop quiet; close #192.

## Follow-ups to open
- Decommission idle ArgoCD Image Updater install (Application + `image-updater/` dir + `argocd:git-creds`) after soak.
- Soak verification: next servarr `:latest` digest PR opens, auto-rebases, auto-merges with zero manual touch (~1 week).
- Any image-tracking gap found (e.g. `newtarr:rolling`, not in the kustomization images block).

## Guarantees preserved
- `validate` CI gate on `main` (nothing bypasses it).
- authentik minor/major manual-merge gate (DB migrations).
- No `kind: node` tasks; `shell`/git via agents; two owner breakpoints per low breakpoint tolerance.
