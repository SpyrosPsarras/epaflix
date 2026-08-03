# system-upgrade Plans onboard (#74, ALL APPS AUTOMATED) — flow

```mermaid
flowchart TD
    A[analyze + design<br/>render, node versions, channel target] --> G1{GATE 1<br/>approve REVISED design?<br/>all Apps automated · architecture-change}
    G1 -- request changes --> A
    G1 -- abort --> X1([abort])
    G1 -- approve --> B[author manifests<br/>plans/ kustomization + AUTOMATED App<br/>+ flip controller App · validate · commit]
    B --> G2a{GATE 2a<br/>review authored change?}
    G2a -- request changes --> B
    G2a -- abort --> X2([abort, branch retained])
    G2a -- proceed --> S[pre-deploy: etcd snapshot<br/>+ baseline versions/readiness]
    S --> G2{GATE 2 — THE BIG ONE<br/>approve push+PR+MERGE?<br/>MERGE auto-starts rollout · destructive-git+deploy}
    G2 -- hold/abort --> X3([branch local, nothing pushed])
    G2 -- approve --> C[publish: rebase, PR,<br/>validate gate, merge-commit]
    C --> E[watch app-of-apps create+selfHeal-sync App<br/>then rolling upgrade:<br/>masters 1-at-a-time → workers 2-at-a-time]
    E --> R{rollout complete?}
    R -- no --> GR{recovery gate<br/>resume / accept / stop<br/>lever: Plan concurrency:0}
    GR -- resume --> E
    GR -- stop --> X4([stopped, incomplete])
    R -- yes --> F[post-upgrade verify<br/>all 7 nodes on target + Ready]
    GR -- accept --> F
    F --> GV{verified?}
    GV -- no --> GVR{re-verify / accept / stop}
    GVR -- re-verify --> F
    GVR -- stop --> X5([stopped])
    GV -- yes --> Z[closeout: close #74,<br/>tick PR test plan,<br/>follow-up: future auto-upgrade SOP/alert]
    GVR -- accept --> Z
    Z --> DONE([done])
```

**Owner revision:** ALL Apps automated (selfHeal) — no manual sync. The `system-upgrade-plans` App is automated AND the existing `system-upgrade-controller` App is flipped manual→automated in the same PR. **Therefore MERGE is the deploy** (selfHeal auto-applies Plans → upgrade fires). ONE hard deploy gate at merge, after an etcd snapshot. homarr is disposable.

**Gates:** G1 design (architecture-change), G2a authored-change review, G2 merge=live-rollout (destructive-git+deploy) + recovery/verify gates.
