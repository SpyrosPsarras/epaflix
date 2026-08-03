# CNPG operator bump v1.28.0 → v1.29.1 (issue #128, re-targeted)

```mermaid
flowchart TD
    P1[Phase 1: Pre-flight\nsnapshot live operator + classify\nupstream 1.29.1 diff + CRD churn\nNO mutation]
    V130{v1.30 now exists?}
    GA[Gate A0: re-plan for 1.30?]
    P2[Phase 2: Re-vendor manifest\n+ bump notes + kustomize build\n+ local commit on branch\nNO push]
    G1{Gate 1 deploy + destructive-git\napprove push+PR+MERGE\nmerge auto-applies via selfHeal}
    P3[Phase 3: Push + open PR\nrebase per policy + wait validate\ngh pr merge --merge]
    M{merged?}
    P4[Phase 4: Reconcile + verify\noperator 1.29.1, CRDs Established\nApp Synced/Healthy, Cluster Healthy]
    H{healthy?}
    GR{Gate recover\nre-verify / accept / stop+revert}
    P5[Phase 5: Finalize\nPR test plan boxes\nre-target + close #128\nfollow-ups]
    DONE([success])
    STOP([stop / local-only / revert])

    P1 --> V130
    V130 -- no --> P2
    V130 -- yes --> GA
    GA -- proceed 1.29.1 --> P2
    GA -- abort --> STOP
    P2 --> G1
    G1 -- request changes --> P2
    G1 -- abort --> STOP
    G1 -- approve --> P3
    P3 --> M
    M -- no --> STOP
    M -- yes --> P4
    P4 --> H
    H -- yes --> P5
    H -- no --> GR
    GR -- re-verify --> P4
    GR -- continue anyway --> P5
    GR -- stop --> STOP
    P5 --> DONE
```

**Key facts**
- CNPG **1.30 does not exist** (latest stable v1.29.1, 2026-05-08). Issue re-targeted to v1.29.1.
- `cnpg-operator` ArgoCD App is **selfHeal ON + ServerSideApply** (#127 merged) → **merge = deploy**.
- Risk = CRD schema churn 1.28→1.29; rollback = revert the merge PR.
- Gates calibrated to low breakpoint tolerance: break only on deploy / destructive-git / regression.
