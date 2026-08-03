# Deliver issue #304 — bound max_slot_wal_keep_size on CNPG

```mermaid
flowchart TD
  A[Phase 1: analyze\nvalidate 8GB > max_wal_size 4GB,\nreloadable, exact edit spec] --> B[Phase 2: implement\nbranch off origin/main,\nadd param, commit only the CR]
  B --> C{Phase 3: validate gate\nkustomize build 06.postgres\n+ no plaintext Secret}
  C -- fail --> B
  C -- pass --> D{{GATE: deploy breakpoint\napprove push+PR+merge}}
  D -- abort --> X[stop, local branch kept]
  D -- PR-only --> P[push+PR, no merge]
  D -- approve --> E[Phase 4: deliver\npush, PR, rebase,\nwait validate, gh pr merge --merge]
  E --> F[Phase 5: verify\nArgoCD postgres Synced/Healthy,\nSHOW param on all 3 instances,\nno healthy-slot invalidation]
  F -- problems --> G{{GATE: verification\nre-verify / continue / stop}}
  G --> H
  F -- ok --> H[Phase 6: closeout\ntick PR test-plan, close #304]
```

- **Breakpoints (low tolerance):** one mandatory **deploy** gate before push/merge; one conditional verification gate only if live verify fails.
- **Risk:** merge→main triggers ArgoCD selfHeal sync of live Postgres config. Param is reloadable → no failover.
