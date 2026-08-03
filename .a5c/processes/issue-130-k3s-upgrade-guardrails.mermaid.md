# Issue #130 — K3s unsupervised-upgrade guardrails (flow)

```mermaid
flowchart TD
    A[Phase 1: Analyze state\n+ version-vs-channel recommendation] --> B{Breakpoint:\nApprove plan + decision?}
    B -- Request changes --> A
    B -- Approve --> C[Phase 2: Implement\nSOP doc + Plans change + alert]
    C --> D[Phase 3: Validate\nkustomize build + YAML lint]
    D --> E[Phase 4: Verify vs issue #130\nscore >=90, no scope creep]
    E -- Fail --> C
    E -- Pass --> F{Breakpoint:\nBranch + commit + push + PR?\n(deploy / destructive-git)}
    F -- Reject --> G[Stop: changes left local]
    F -- Approve --> H[Phase 5: Integrate\nbranch from origin/main, PR closes #130]
    H --> I[Phase 6: Follow-ups\nrun PR test plan + open gh issues]
    I --> J[Done]
```
