# cleanuparr-s04e07-triage

Triage + remediation for the stalled Sonarr seriesId 40 S04E07 torrent (hash `828ea9eb…`, `stalledDL`) — issue #139.

```mermaid
flowchart TD
    A[Phase 1: Triage read-only<br/>qbt state · Sonarr wants? · queue · Cleanuparr · re-arm risk] --> B[Phase 2: Adversarially verify<br/>recommended action]
    B --> C{finalAction == no-op?}
    C -- yes --> G2
    C -- no --> G1{{GATE 1 deploy<br/>approve live remediation?}}
    G1 -- Abort/Reject --> STOP1[Stop after read-only triage]
    G1 -- Approve --> D[Phase 3: Remediate live<br/>remove torrent · clear queue · close re-arm]
    D --> E[Phase 4: Verify fix read-only<br/>gone + not re-arming]
    E --> F{fixed?}
    F -- no --> RG{{Verification gate<br/>re-verify / accept / stop}}
    RG -- re-verify --> E
    RG -- stop --> STOP2[Stop, state recorded]
    F -- yes --> G2
    RG -- accept --> G2
    G2{{GATE 2 outward-facing<br/>close #139 + follow-ups?}}
    G2 -- Approve --> H[Phase 5: Wrap-up<br/>close #139 + gh follow-up + optional PR]
    G2 -- Skip --> END
    H --> END[Done]
```

## Gates (breakpointTolerance = low / expert)

- **GATE 1 — deploy**: mandatory before any live mutation of qbittorrent / Sonarr (remove download, clear queue, unmonitor/blocklist). Skipped only when the verified action is `no-op`.
- **GATE 2 — outward-facing**: mandatory before closing issue #139 and opening any gh follow-up / doc / git change.
- **Verification gate**: only if post-remediation verify fails (re-verify / accept / stop).

All other steps are read-only investigation or the single approved live change — no extra breakpoints, per the user's low tolerance.
