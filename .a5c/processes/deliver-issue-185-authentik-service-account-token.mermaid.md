# Issue #185 — Durable Authentik service-account token (process map)

Best-for-services delivery: a declarative, GitOps-managed machine identity (Authentik
blueprint + SOPS non-expiring token) that replaces ad-hoc personal tokens. Folds in #175
(retire the personal superuser token) and supersedes the contradictory PR #225. Two owner
gates: **design approval** and **deploy/merge** (alwaysBreakOn: deploy, destructive-git).

```mermaid
flowchart TD
    A[Phase 1: Assess + Design<br/>read-only, probe old token] --> B{Design approval<br/>owner breakpoint}
    B -- request changes --> A
    B -- approved --> C[Phase 2: Implement on fresh branch<br/>blueprint + SOPS Secret + wiring + secrets.yml + docs]
    C --> D{Validate gate<br/>kustomize build, SOPS decrypt,<br/>hook, no secret leak}
    D -- fail --> C
    D -- pass --> E{Adversarial review<br/>vs #185/#175, scope, docs}
    E -- fail --> C
    E -- pass --> F[Phase 4: Finalize<br/>push, open PR Closes #185+#175,<br/>supersede PR #225, follow-ups,<br/>tick pre-merge test-plan boxes]
    F --> G{Deploy + merge approval<br/>owner breakpoint}
    G -- request changes --> F
    G -- approved --> H[Phase 6: Rebase+merge per policy,<br/>ArgoCD sync app-authentik,<br/>blueprint applies SA + token]
    H --> I{Verify live token<br/>GET /api/v3 returns 200}
    I -- fail --> J[Report diagnostics,<br/>issues stay open]
    I -- pass --> K[Close #185 + #175<br/>done]
```

## Quality gates & loops
- **Validate** (Phase 2): repo's own checks — `kustomize build --enable-helm --enable-alpha-plugins --enable-exec`, `sops -d`, `check-sops-encrypted.sh`, secret-leak scan. Loops back to Implement.
- **Review** (Phase 3): adversarial correctness vs #185 + #175, blueprint schema, wiring, docs consistency, no leak. Loops back to Implement.
- **Verify** (Phase 6): live token authenticates against `/api/v3/`; issues only close on success.

## Owner breakpoints
1. **Design approval** — architecture + secrets decision; confirms mechanism, scope, and PR #225 supersession.
2. **Deploy + merge** — merges to main and changes live Authentik state (new service-account user + non-expiring token).
