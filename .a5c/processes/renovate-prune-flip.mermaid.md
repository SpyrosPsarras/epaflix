# renovate-prune-flip — flow

Resolve Epaflix issue **#54** (last of the 5 split prune-flips): flip the `renovate`
ArgoCD Application `prune:false → prune:true` after its overdue 48h soak.

```mermaid
flowchart TD
    A[Phase 1: pre-flip safety verify\nNO mutation] --> B{Deploy gate\nowner breakpoint}
    A -. proves .-> A1[app Synced+Healthy\nwould-be-pruned = empty\nrenovate-secrets UNTRACKED\nno transient Job/Pod tracked]
    B -- Abort --> X[Stop: no mutation]
    B -- Approve flip + merge --> C[Phase 2: edit prune:true\n+ branch + local commit]
    C --> D[Phase 3: rebase onto main\npush --force-with-lease\nPR + wait validate + gh pr merge --merge]
    D --> E[Phase 4: post-merge verify\nprune live, app Synced+Healthy\nCronJob + renovate-secrets survive\nno tracked resource pruned]
    E -->|verified| G[Phase 5: closeout]
    E -->|anomaly| F{Verify gate\nowner breakpoint}
    F -- Re-verify --> E
    F -- Continue --> G
    F -- Stop --> X2[Stop: accept/abort]
    G --> H[Close #54 + tick PR test plan\n+ open drift follow-up only if NEW orphan]
```

## Safety model
- **Deploy is gated** by a mandatory owner breakpoint (profile `alwaysBreakOn: deploy`).
- The renovate app tracks only `{Namespace, ServiceAccount, CronJob}` → prune surface is tiny.
- `renovate-secrets` (imperative GitHub PAT, out of git) must be **untracked** so prune ignores it — verified before AND after.
- CronJob Jobs/Pods are owner-referenced → not ArgoCD-tracked → prune leaves them alone.
- Merge is the deploy: app-of-apps (#48 closed) selfHeal reconciles the new spec — no manual `kubectl apply`.
