# Odysseus TrueNAS GPU deploy — flow

```mermaid
flowchart TD
    A[Phase 1: analyze\nOdysseus repo + TrueNAS GPU host + Authentik/Traefik pattern\nread-only] --> B[Phase 2: plan\ncompose+GPU, local model, secrets, auth model, exposure set]
    B --> G1{Plan gate\narchitecture + authorizes live mutations}
    G1 -- Request changes --> B
    G1 -- Abort --> X1[stop]
    G1 -- Approve --> C[Phase 3: author artifacts\ncodified compose + k3s SSO manifests + runbook\nbranch + local commit, no push]
    C --> G2{Deploy gate\nalwaysBreakOn deploy + secrets}
    G2 -- Stop --> X2[stop, nothing deployed]
    G2 -- Approve --> D[Phase 4: deploy on TrueNAS\ndataset + secrets + build + Custom App + serve model on GPU]
    D --> E[Phase 5: verify deploy\nGPU in container + UI + GPU-backed completion]
    E -- issues --> GE{Anomaly gate\nre-verify / accept / stop}
    GE -- re-verify --> E
    GE -- stop --> X3[stop, LAN-only deployed]
    E -- ok --> G3{Expose gate\noutward-facing}
    GE -- accept --> G3
    G3 -- Skip/Abort --> X4[stop, leave LAN-only]
    G3 -- Approve --> F[Phase 6: expose\nAuthentik objects + GitOps PR merge + Cloudflare DNS-only]
    F --> H[Phase 7: verify SSO\n302 to Authentik, outpost route, no double login]
    H -- issues --> GH{Anomaly gate\nre-verify / accept / stop}
    GH -- re-verify --> H
    H -- ok --> Z[Phase 8: closeout\nPR test plan + follow-up issues + .history log]
    GH -- accept --> Z
    Z --> DONE[done]
```

Breakpoints (owner, low tolerance / alwaysBreakOn deploy+architecture+secrets):
1. **Plan gate** — architecture review; approval authorizes all later live mutations.
2. **Deploy gate** — build + Custom App create + serve model on the GPU (no public exposure yet).
3. **Expose gate** — Authentik objects + GitOps merge + Cloudflare record (outward-facing).
Plus two conditional anomaly/re-verify gates after the deploy and SSO verifications.
