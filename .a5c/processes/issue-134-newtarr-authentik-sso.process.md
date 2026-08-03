# Process: Issue #134 — newtarr Authentik SSO (Option B)

## Goal
Front `newtarr.epaflix.com` with Authentik forward-auth SSO using the established
single-application pattern (as used by `traefik.epaflix.com`). newtarr becomes the **first**
servarr app on this pattern.

## Owner decisions (interview 2026-06-06)
1. **SSO-only** — Authentik gates both local and internet; remove the in-app LAN bypass.
2. Access gated by the existing **"Servarr users"** Authentik group.
3. **All Authentik objects created via the API** by the run (admin token in git-ignored
   `secrets.yml` → `authentik_admin_api_token`).

## Two surfaces, ordered
1. **Authentik (live, API, FIRST):** Proxy Provider (`forward_single`, external host
   `https://newtarr.epaflix.com`) + Application (slug `newtarr`) + group-policy binding to
   "Servarr users" + add provider to the embedded outpost (preserving existing providers).
2. **GitOps (branch + PR + merge):** attach `traefik-system/authentik-forwardauth` (+`priority:10`)
   to `newtarr-https`, add `newtarr-outpost-https` IngressRoute (`priority:15`,
   `PathPrefix(/outpost.goauthentik.io/)` → `authentik-server@app-authentik:80`), apply the
   SSO-only env change to `newtarr.yaml`, update the Authentik README forward-auth list.

Authentik **before** git merge so the outpost has a provider for the host when the middleware
goes live.

## Phases
1. **Analyze** — live + git state, including what source IP newtarr sees behind Traefik (drives
   the double-login analysis).
2. **Plan** — concrete Authentik + git change set; explicitly resolves the SSO-only / double-login
   question. **Breakpoint BP1** (owner; also authorizes the live Authentik creation).
3. **Create Authentik** — provider/app/binding/outpost via idempotent API calls + read-back.
4. **Author manifests** + `kustomize build` validation (refine loop on failure).
5. **Deploy** — **Breakpoint BP2** (owner) → push/PR/rebase/`validate`/`gh pr merge --merge`.
6. **Verify** — ArgoCD Synced/Healthy, live middleware + outpost route, unauthenticated `curl`
   returns a 302 to Authentik. Conditional verification gate.
7. **Closeout** — tick the PR test plan in the PR body (never a new comment), open warranted
   follow-ups, close #134.

## Breakpoints (low tolerance; alwaysBreakOn architecture/deploy/secrets)
- **BP1** plan approval (+ authorize Authentik creation)
- **BP2** deploy approval (push/PR/merge)
- conditional anomaly gate (Authentik read-back) and verification gate

## Rollback
- Authentik: delete the provider/app/binding and remove the provider from the outpost.
- Git: revert the merged PR.
