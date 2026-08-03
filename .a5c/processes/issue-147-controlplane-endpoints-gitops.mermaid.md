# Issue #147 — control-plane Endpoints GitOps durability

```mermaid
flowchart TD
    A["Phase 1 — design analysis<br/>(live + git, read-only)<br/>enumerate A / B / C + recommend"] --> G1{{"GATE 1 — architecture<br/>owner picks A / B / C"}}
    G1 -- "request changes" --> A
    G1 -- "abort" --> X1([abort: no change])
    G1 -- "A / B / C" --> B["Phase 2 — implement on branch<br/>(reversible local commit, no push)"]
    B --> C["Phase 3 — validate<br/>kustomize build / ArgoCD reasoning / secret-safety"]
    C -- "fail" --> B
    C -- "fail x3" --> GV{{"owner: retry / accept / stop"}}
    GV -- "retry" --> B
    GV -- "stop" --> X2([stop: local branch kept])
    C -- "ok" --> G2{{"GATE 2 — destructive-git + deploy<br/>approve push + PR + (auto-merge?)"}}
    GV -- "accept" --> G2
    G2 -- "request changes" --> B
    G2 -- "abort" --> X3([abort: local branch kept])
    G2 -- "approve" --> D["Phase 4 — publish<br/>rebase origin/main + push --force-with-lease<br/>open PR · wait validate · (merge --merge)<br/>open follow-up issues"]
    D -- "manual-merge chosen / merge blocked" --> X4([PR open, pending merge])
    D -- "merged" --> E["Phase 5 — verify (live)<br/>ArgoCD Synced/Healthy · Endpoints managed+populated<br/>Prometheus targets UP · execute PR test plan<br/>(edit PR body) · close #147"]
    E -- "incomplete" --> GVF{{"owner: re-verify / accept / stop"}}
    GVF -- "re-verify" --> E
    E -- "verified" --> Z([done: #147 closed])
    GVF -- "accept/stop" --> Z2([done: PR merged, verify partial])
```

**alwaysBreakOn gates (low breakpoint tolerance):** architecture-change (Gate 1), destructive-git + deploy (Gate 2). Verification gates only fire on failure.
