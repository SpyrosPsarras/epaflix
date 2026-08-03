# Process: Revoke & retire the Authentik admin API token (#175)

**Approach A — revoke & retire** (owner-confirmed). End-state: no standing long-lived Authentik admin token.

## Why this shape
- The token is **already expired** (live `GET /api/v3/core/users/me/` → HTTP 403).
- It is consumed by **no running workload** — only documentation references it → zero blast radius.
- The #134 automation that needed it is complete → cleanest is to delete it and document on-demand minting.

## Phases
1. **Assess** (agent, read-only) — re-confirm blast radius, live token status, exact edit plan.
2. **Author docs** (agent) — on a fresh branch off `origin/main`: add an "Admin API token (retired — mint on demand)" runbook to `2-k3s/07.authentik-deployment/README.md`; fix the stale "Step 0 blocker" note in `0-truenas/custom-apps/odysseus/README.md`; cross-link #134/#175. **Docs-only.**
3. **Doc review** (agent quality gate, ≤3 refinement loops) — only `.md` changed, no real secret value in diff, runbook correct, cross-links present.
4. **BP1 — secrets-rotation breakpoint** (owner, always-break) — owner deletes the live token object in Authentik UI and approves.
5. **Verify retired** (agent) — remove the dead `authentik_admin_api_token` key from the git-ignored `secrets.yml` (with `.bak`), confirm live 401/403, confirm no tracked git change.
6. **Finalize** (agent) — push branch, open PR per Epaflix merge policy with a test-plan checklist, open follow-up issues, update #175 (no close yet).
7. **BP2 — merge approval** (owner) — approve the docs-PR merge.
8. **Merge** (agent) — rebase onto `origin/main`, wait for `validate`, `gh pr merge --merge`, close #175.

## Guardrails honored
- `alwaysBreakOn` secrets-rotation + deploy/destructive-git → BP1 and BP2.
- Never commit `secrets.yml` (git-ignored); never print/commit real token values.
- Epaflix semi-linear merge policy (rebase + force-with-lease + `validate` + merge-commit).
- Follow-up items → `gh issue` before closing the thread.
