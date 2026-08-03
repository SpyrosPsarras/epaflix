# Issue #134 — newtarr Authentik SSO (Option B, SSO-only)

```mermaid
flowchart TD
    A[Phase 1: Analyze live + git state\nnewtarr env, IngressRoutes, source-IP,\nAuthentik outpost/group/flows] --> B[Phase 2: Plan\nAuthentik objects + git change set\n+ SSO-only double-login resolution]
    B --> BP1{{"BP1 — owner: approve plan\n(also authorizes live Authentik creation)\narchitecture-change"}}
    BP1 -- Request changes --> B
    BP1 -- Abort --> X1[stop: plan-aborted]
    BP1 -- Approve --> C[Phase 3: Create Authentik\nProxy Provider + Application\n+ group binding + outpost assign\nvia API]
    C --> Cok{readBack ok?}
    Cok -- no --> BPc{{"anomaly gate"}}
    BPc -- stop --> X2[stop: authentik-create-failed]
    BPc -- continue --> D
    Cok -- yes --> D[Phase 4: Author manifests\nmiddleware+priority on newtarr-https,\nnewtarr-outpost-https route, env change, README\n+ local commit]
    D --> V[Phase 4b: kustomize build]
    V -- RENDER_FAIL --> D
    V -- ok --> BP2{{"BP2 — owner: approve deploy\npush+PR+rebase+merge\ndeploy / destructive-git"}}
    BP2 -- Skip/Abort --> X3[stop: deploy-not-approved]
    BP2 -- Approve --> E[Phase 5: Publish + merge\ngh pr create, rebase, validate, merge --merge]
    E -- not merged --> X4[stop: merge-failed]
    E -- merged --> F[Phase 6: Verify\nArgoCD Synced/Healthy,\nlive middleware+outpost route,\nSSO 302 redirect E2E]
    F -- not verified --> BP3{{"verification gate"}}
    BP3 -- re-verify --> F
    BP3 -- stop --> X5[stop: verification-stop]
    BP3 -- accept --> G
    F -- verified --> G[Phase 7: Closeout\ntick PR test plan, follow-up issues,\nclose #134]
    G --> Z[done]
```

**Order rationale:** Authentik objects are created *before* the git merge so the embedded
outpost already has a provider for `newtarr.epaflix.com` when Traefik forward-auth goes live —
otherwise authenticated requests would 400 / redirect-loop.
