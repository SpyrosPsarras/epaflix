# Odysseus TrueNAS → k3s migration — flow

```mermaid
flowchart TD
    A[Phase 1: analyze\nTrueNAS state, data sizes, GHCR push, k3s patterns, route\nread-only] --> SFG{Search-fix gate\nlive TrueNAS}
    SFG -- Apply --> SF[Phase 2: search fix on TrueNAS\nSearXNG json + disable code/shell tools]
    SFG -- Skip --> B
    SF --> B[Phase 3: plan migration\nimage/manifests/route/argocd/data]
    B --> G1{Plan gate\narchitecture + authorizes live mutations}
    G1 -- Request changes --> B
    G1 -- Abort --> X1[stop]
    G1 -- Approve --> C[Phase 4: build + push image\nGHCR public ghcr.io/.../odysseus:73673258]
    C --> D[Phase 5: author k3s manifests\nDeploys/Svc/PVC/ConfigMap/SOPS Secret + route cutover + ArgoCD app\nbranch + commit, no push]
    D --> G2{Cutover gate\nalwaysBreakOn deploy + secrets}
    G2 -- Stop --> X2[stop, nothing deployed]
    G2 -- Approve --> E[Phase 6: deploy + migrate + cutover\nmerge PR, ArgoCD sync, migrate data, re-point route]
    E --> F[Phase 7: verify k3s\npods, Ollama, SSO, search, migrated data]
    F -- issues --> GV{Anomaly gate\nre-verify / rollback-to-TrueNAS / accept}
    GV -- re-verify --> F
    GV -- rollback --> X3[stop, route stays/returns to TrueNAS fallback]
    F -- ok --> G3{Decommission gate\ndestructive}
    GV -- accept --> G3
    G3 -- Keep/skip --> Z
    G3 -- Approve --> DEC[Phase 8: decommission TrueNAS app\nremove app, KEEP /mnt/pool1/odysseus backup]
    DEC --> Z[Phase 9: closeout\nPR test plan + close #184 + follow-ups + .history]
    Z --> DONE[done]
```

Breakpoints (owner; alwaysBreakOn deploy+destructive+architecture+secrets):
1. **Search-fix gate** — apply the live fix on TrueNAS now (quick win).
2. **Plan gate** — architecture; approval authorizes GHCR push + GitOps merge + k3s deploy + data migration + route cutover + (later) decommission.
3. **Cutover gate** — merge + deploy + migrate + re-point the route to k3s (TrueNAS kept as fallback).
4. **Decommission gate** — destructive; remove the TrueNAS app, keep the dataset as backup.
Plus a conditional verify/anomaly gate (re-verify / roll back to TrueNAS / accept).
