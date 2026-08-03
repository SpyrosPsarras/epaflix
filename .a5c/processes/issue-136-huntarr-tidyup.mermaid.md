# Issue #136 — huntarr→newtarr post-migration tidy-up

```mermaid
flowchart TD
    A[PHASE 1: verify-safety<br/>read-only<br/>soak health + target safety] --> G1{GATE 1<br/>destructive approval<br/>owner}
    G1 -->|Abort / not approved| X[stop after read-only<br/>nothing deleted]
    G1 -->|Approve / Tarball only / Orphan dir only| B[PHASE 2: delete-remnants<br/>LIVE rm over SSH<br/>worker-61 tarball + worker-65 orphan dir]
    B --> C[PHASE 3: verify-done<br/>read-only<br/>targets gone + newtarr healthy + env state]
    C -->|done=false| G1b{recover gate<br/>re-verify / continue / stop}
    G1b -->|re-verify| C
    G1b -->|stop| X2[stop, report partial]
    C -->|done=true| G2{GATE 2<br/>close-out approval<br/>owner}
    G1b -->|continue| G2
    G2 -->|Approve| D[PHASE 4: closeout<br/>gh issue comment + close<br/>+ follow-up if deferred]
    G2 -->|Skip| E[finish without close-out]
    D --> E
```

## Gates
- **GATE 1 (destructive)** — fires only after read-only proof that newtarr soak is good and both targets are orphaned/safe. Options: Approve / Tarball only / Orphan dir only / Abort.
- **GATE 2 (outward-facing)** — approve closing issue #136 + opening any follow-up.

The newtarr pod is never restarted — stale `HUNTARR_*` env is inert and clears on its own next restart (owner decision: verification only).
