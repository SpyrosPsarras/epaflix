# Deliver issue #138 — Cleanuparr blocklist durability + soak-confirm

```mermaid
flowchart TD
  R[Phase 1: Recon read-only\ncapture live blocklist + soak evidence] --> S[Phase 2: Soak-confirm part a\nS04E13 stayed dead since 2026-05-31]
  S --> P[Phase 3: Plan codify part b\n+ part c recommendation]
  P --> PC{plan-check\napproved?}
  PC -- no --> P
  PC -- yes --> BP1{{BP1 owner gate\nsoak + plan + part c decision}}
  BP1 -- changes --> P
  BP1 -- approve --> I[Phase 4: Implement\nSOPS seed + initContainer + ksops + docs]
  I --> B{kustomize build ok?}
  B -- no --> I
  B -- yes --> BP2{{BP2 owner gate\ndiff review -> commit/push/PR/merge/DEPLOY}}
  BP2 -- changes --> I
  BP2 -- approve --> PR[Phase 5: PR\nrebase + validate + gh pr merge --merge]
  PR --> C[Phase 6: Closeout\nArgoCD healthy + follow-ups + close #138]
```

- **BP1** (architecture-change gate): approve soak interpretation + codify plan, decide part (c).
- **BP2** (deploy + secrets-rotation + destructive-git gate): review exact diff, authorize commit→merge→ArgoCD deploy.
