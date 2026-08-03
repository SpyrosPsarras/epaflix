# Deliver open issues — oldest first (loop)

```mermaid
flowchart TD
  S[Select oldest open issue<br/>skip #31 Dependency Dashboard + processed] -->|done| END[Run complete]
  S -->|issue| T[Triage + design read-only<br/>classify deliveryMode + riskClass]
  T --> G1{{Owner TRIAGE gate<br/>proceed / skip / stop / change}}
  G1 -->|change| T
  G1 -->|skip| S
  G1 -->|stop| END
  G1 -->|proceed| MODE{deliveryMode?}

  MODE -->|code-change-pr| IMP[Implement on branch] --> VAL[Validate render/scope]
  VAL -->|fail| IMP
  VAL -->|pass| REV[Adversarial review]
  REV -->|fail| IMP
  REV -->|pass| FIN[Push + open PR + follow-ups]
  FIN --> G2{{Owner DEPLOY/MERGE gate<br/>merge / hold / stop}}
  G2 -->|hold| S
  G2 -->|stop| END
  G2 -->|merge| DEP[Rebase+merge, verify ArgoCD, close issue] --> S

  MODE -->|cluster-op / owner-decision /<br/>record / already-done / wont-fix| G3{{Owner EXECUTE gate<br/>if live/destructive}}
  G3 -->|skip| S
  G3 -->|stop| END
  G3 -->|execute| EX[Execute op / record decision<br/>verify + close + follow-ups] --> S
```

Two human gates per delivered issue (triage/approach + deploy/execute), matching the
profile `breakpointTolerance=low`, `alwaysBreakOn=[destructive-git, deploy]`. Everything
between the gates (analysis, branch edits, validate, review, PR authoring) is autonomous.
