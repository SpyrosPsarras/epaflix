# seerr OIDC research — flow

```mermaid
flowchart TD
    A[research: confirm live images READ-ONLY + research seerr-team/seerr<br/>discussion #1529, latest release, official OIDC image] --> B[analyze: compare deployed preview fork<br/>vs latest stable OIDC; recommend]
    B --> C{owner decision gate}
    C -->|Report only / Stop| E[return: report-only]
    C -->|Draft migration plan| D[draft-plan: write .history/seerr-oidc-migration-plan.md<br/>NO deploy, NO git push]
    D --> F[return: draft-plan]
```

- **No cluster mutation, no deploy, no git push** anywhere in this run.
- All work via `general-purpose` agent (opus), read-only kubectl `--context epaflix` + web research.
- One owner decision gate (deploy-adjacent → matches low breakpoint tolerance).
