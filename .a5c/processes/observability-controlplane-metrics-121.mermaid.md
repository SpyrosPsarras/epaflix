# #121 — k3s control-plane metrics: flow

```mermaid
flowchart TD
    A[Phase 1: analyze READ-ONLY\nendpoints empty? loopback bind?\nderive master IPs + ports/scheme\nrecommend live method] --> B[Phase 2: prepare git locally\nprometheus-values endpoints\n+ k3s.instructions rebuild args\nbranch + commit, NO push]
    B --> G{GATE deploy + destructive-git\napprove live rollout + merge?}
    G -- Request changes --> B
    G -- Abort --> X[stop: no mutation]
    G -- Approve --> C[Phase 3: LIVE rollout\nmasters 51 to 52 to 53, ONE at a time\nconfig.yaml + restart k3s\nhealth-gate etcd quorum between each]
    C -- partial/unhealthy --> R1{recover: retry / stop}
    R1 -- retry --> C
    R1 -- stop --> X2[stop: not merged]
    C -- healthy --> D[Phase 4: push + PR + merge\nEpaflix policy rebase + merge-commit]
    D --> E[Phase 5: post-verify\nEndpoints populated\nup cm/scheduler/etcd = 1\nobservability Synced+Healthy]
    E -- incomplete --> R2{recover: re-verify / accept / stop}
    R2 -- re-verify --> E
    R2 -- stop --> X3[stop]
    E -- verified --> F[Phase 6: closeout\nclose #121, tick PR test plan\nfollow-ups #44 durability]
```

Order rationale: LIVE bind-address change goes FIRST so the metrics ports already answer before
the git endpoints change merges and Prometheus begins scraping them.
