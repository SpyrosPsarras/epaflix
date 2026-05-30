# traefik prune flip (#51) — process flow

```mermaid
flowchart TD
  A[Phase 1: preflight-verify<br/>app Synced/Healthy?<br/>any tracked-but-not-in-git?<br/>classify orphans] --> G1{Deploy gate<br/>approve flip + merge?}
  G1 -- Abort --> X[stop, no mutation]
  G1 -- Approve --> B[Phase 2: prepare-change<br/>branch + edit prune:true<br/>+ header comment + commit]
  B --> C[Phase 3: publish-merge<br/>push + PR + gh pr merge --admin --merge]
  C --> D[Phase 4: post-merge-verify<br/>app-of-apps reconciled?<br/>prune:true live?<br/>nothing pruned? orphan survives?]
  D -- anomaly --> G2{Verify gate<br/>re-verify / accept / stop}
  D -- verified --> E[Phase 5: closeout<br/>close #51 + tick PR test plan<br/>+ open orphan drift follow-up]
  G2 -- re-verify --> D
  G2 -- stop --> X
  G2 -- accept --> E
  E --> Z[done]
```
