# Process: traefik drift #108 — codify-or-remove `https-redirect-epaflix`

**Issue:** [#108](https://github.com/SpyrosPsarras/epaflix/issues/108) — traefik: codify-or-remove untracked IngressRoute/https-redirect-epaflix (drift)

## Problem

`IngressRoute/https-redirect-epaflix` lives in `traefik-system` (~103d, imperatively
applied 2026-02-17) but is **not in git** and **not ArgoCD-tracked**. It is a catch-all
HTTP→HTTPS redirect on the `web` entrypoint (`HostRegexp({subdomain}.epaflix.com) ||
Host(epaflix.com)` → `redirect-https` middleware → `noop@internal`). The traefik App now
runs `prune:true` (#51, PR #107), but prune ignores **untracked** resources, so it
survives — leaving traefik-system with non-zero untracked drift.

## Key decision input (pre-flight finding)

`values/traefik-values.yaml` already sets `ports.web.http.redirections.entryPoint`
(`permanent:true, scheme:https, to:websecure`) — a **global entrypoint-level** redirect that
rewrites all :80 → :443 before routing. This makes the catch-all IngressRoute **redundant**.
→ Recommended path: **DELETE** + doc note. Alternative: **CODIFY** into git.

## Phases

1. **Analyze + decide** (read-only): prove orphan is untracked, prove the entrypoint redirect
   is configured *and live* (curl an http host with no dedicated route → expect 30x→https),
   confirm no host depends solely on the orphan; recommend `delete` or `codify`.
2. **Owner gate** (`destructive-git` + `deploy`): owner chooses **Delete** / **Codify** / **Abort**.
3. **Execute chosen path** → branch + local commit:
   - *Delete:* backup YAML → `kubectl delete` the untracked orphan (safe & permanent — prune
     and selfHeal ignore untracked) → doc-note commit (entrypoint redirect supersedes catch-all).
   - *Codify:* author `ingress/https-redirect.yaml` matching the live spec + add to
     `kustomization.yaml` → commit (ArgoCD adopts the existing live object, no diff).
4. **Push + PR + merge** per Epaflix policy (merge-commit + mandatory rebase, `validate` gate).
5. **Post-merge verify**: orphan gone (delete) or tracked (codify); traefik-system zero
   untracked drift; HTTP→HTTPS still 30x→https; traefik app Synced+Healthy. Owner gate on anomaly.
6. **Closeout**: close #108 with outcome, tick PR test-plan boxes (edit body, not a comment),
   open a follow-up issue only if new drift surfaced.

## Guardrails

- Never touch the in-git `redirect-https` Middleware.
- kubectl is over SSH to master .51; no local cluster context.
- Mandatory owner breakpoint before any mutation (destructive + deploy).
- All git changes go through PR+merge; live delete is a separate, gated, reversible (backup) step.
