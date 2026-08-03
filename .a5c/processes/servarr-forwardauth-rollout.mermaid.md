# Issue #176 — Servarr forward-auth rollout

Gate the 10 drifted, public, unauthenticated servarr UIs (sonarr, sonarr2, radarr,
prowlarr, bazarr, cleanuparr, homarr, lingarr, qbittorrent, wizarr) behind the
newtarr Authentik forward-auth pattern. jellyfin + seerr untouched. Full GitOps:
codify drift + middleware + declarative Authentik blueprint. Two-stage deploy with
human merge gates. All edits in an isolated worktree.

```mermaid
flowchart TD
  D[Discover live Authentik + blueprint format] --> DS[Design rollout -> 176-design.md]
  DS --> G1{Design Gate}
  G1 -->|approve| A[Author IngressRoutes x10 fan-out]
  A --> K[Wire kustomization + kustomize build]
  DS --> BP[Author SOPS blueprint: providers+apps+outpost]
  K --> V{Validate: kustomize/helm/sops guard}
  BP --> V
  V -->|fail| V
  V -->|pass| PA[Prep PR-A blueprint, validate green]
  PA --> GA{Deploy Gate A — merge PR-A}
  GA -->|merge| MA[Merge PR-A, verify providers/apps/outpost LIVE]
  MA --> PB[Prep PR-B ingressroutes, validate green]
  PB --> GB{Deploy Gate B — merge PR-B middleware live}
  GB -->|merge| MB[Merge PR-B, verify routes reconciled]
  MB --> T[Test x10 fan-out: anon -> 302 auth.epaflix.com]
  T -->|some fail| GT{Fix & re-test gate}
  T --> W[Wrap-up: close #176, tick PR test plans, follow-ups, docs]
```

Key safety properties:
- Stage A (Authentik objects) is fully live + verified before Stage B (middleware) merges — prevents lockout/500.
- Outpost binding must preserve existing newtarr (pk 82) + traefik bindings — never clobber.
- Deploy/merge steps are `alwaysBreakOn` human gates (owner breakpointTolerance=low).
