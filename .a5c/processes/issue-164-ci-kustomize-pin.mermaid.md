# Issue #164 — CI `validate` install-flake fix (pin + direct-download helm/kustomize)

Pure git/CI change (no live cluster op, no SSH, no secrets). One owner gate before merge,
because `validate` is a required+strict check — a bad edit would block every future PR.

```mermaid
flowchart TD
  P[Phase 1: plan (read-only)\nanalyse ci.yml, pick pinned versions,\nPROBE official asset URLs, design retry + Renovate manager] --> UC{asset URLs verified?}
  UC -- no --> GU[owner gate: abort / proceed]
  GU -- abort --> X1[stop: bad URLs]
  UC -- yes --> I[Phase 2: implement\nbranch off origin/main, edit ci.yml + renovate.json\n(+README), local commit, NO push]
  GU -- proceed --> I
  I --> V[Phase 3: local-verify\ndownload SAME pinned binaries to tmp,\nrun kustomize build over sops-free dirs + helm pull spot-check]
  V --> VOK{verified?}
  VOK -- no (x2) --> I
  VOK -- yes --> G[OWNER GATE (merge)\ndiff + local verify shown\napprove / request changes / abort]
  G -- request changes (x3) --> I
  G -- abort --> X2[stop: not approved]
  G -- approve --> PUB[Phase 4: publish+merge\nrebase onto origin/main, force-with-lease, push, open PR,\nWAIT for new validate green on PR (live proof), gh pr merge --merge]
  PUB --> PM{merged + validate green?}
  PM -- no --> GR[recovery gate: retry merge / stop]
  GR --> PUB
  PM -- yes --> PV[Phase 5: post-verify\nPR merged + main validate (push) success]
  PV --> PVOK{verified?}
  PVOK -- no --> GR2[recovery gate: re-verify / accept / stop]
  GR2 --> PV
  PVOK -- yes --> C[Phase 6: closeout\nclose #164, tick PR test plan (edit body),\nopen follow-ups: helm-4 review, Renovate-pin cadence]
  C --> DONE[done]
```

## Design choices
- **Direct official asset URLs** (`get.helm.sh/...`, `github.com/.../releases/download/...`) — no
  rate-limited GitHub API tag lookup, no `curl|bash` of a script from a moving branch → the root cause.
- **Bounded retry** around downloads so a transient blip self-heals instead of hard-failing the gate.
- **Renovate custom manager** keeps the two pins fresh (repo ethos = Renovate-manages-everything).
- **helm 3.x conservative pin** (helm v4 is now latest major) — CI only needs `helm pull`; helm-4
  adoption tracked as a follow-up.
- **PR's own `validate` run is the live proof** — we wait for it green before merging.
- Effects of the gate keep the change reversible (PR revert) and aligned to the Epaflix merge policy.
