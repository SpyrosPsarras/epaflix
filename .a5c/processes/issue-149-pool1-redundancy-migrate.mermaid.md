# Issue #149 — Make the SOPS master age-key backup redundant (Option C)

Move `pool1/encrypted-backups` (non-redundant stripe) → `apps/encrypted-backups` (RAIDZ1, redundant).
Additive-first: create + copy + verify **before** anything is destroyed. Three owner gates.

```mermaid
flowchart TD
  A[Preflight READ-ONLY<br/>facts + exact additive plan + rollback] --> B{Dst pool redundant<br/>+ healthy?}
  B -- no --> Babort[Owner: abort / override]
  B -- yes --> G1{{GATE 1 — secrets+destructive<br/>approve exact additive plan}}
  G1 -- request changes --> A
  G1 -- abort --> X1[stop: nothing mutated]
  G1 -- approve --> M[Additive migrate<br/>create encrypted apps dataset same passphrase<br/>copy ~236K file + repoint TrueNAS refs<br/>OLD untouched]
  M --> V[Independent verify<br/>on RAIDZ1 + checksum + unlock/decrypt + recoverable]
  V -- fail --> RV{{Re-verify / stop}}
  RV --> V
  V -- pass --> G2{{GATE 2 — destructive<br/>approve retiring OLD pool1 dataset}}
  G2 -- keep old --> D
  G2 -- approve --> R[Destroy old pool1 dataset + snapshot]
  R --> D[Docs + PR in ISOLATED worktree<br/>no merge; cross-link #124 #57]
  D --> G3{{GATE 3 — deploy/git<br/>approve rebase + merge + close}}
  G3 -- request changes --> D
  G3 -- abort --> X3[stop: PR left open]
  G3 -- approve --> P[Rebase + merge per policy<br/>close #149 + tick test plan + follow-ups]
  P --> Z[Done]
```

**Why Option C:** no spare disks exist (A/B need new 10TB+14TB hardware); payload is only ~236K;
`apps` is already RAIDZ1-redundant with ~230G free; the age key keeps two independent copies
(workstation + in-cluster KSOPS) so relocating this tertiary copy is low-risk.
