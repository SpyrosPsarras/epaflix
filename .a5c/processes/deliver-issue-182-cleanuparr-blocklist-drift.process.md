# Process — Deliver issue #182 (Cleanuparr Sonarr blocklist upstream-drift)

**Goal:** Decide and implement how the codified Cleanuparr Sonarr blocklist (a frozen
snapshot from #138/PR#181) stays current with upstream `flmorg/cleanuperr`.

This is primarily a **decision** issue, so the process gates on the owner picking the
mechanism, then implements + ships it under the standard guardrails.

## Phases

1. **Recon (read-only)** — capture the facts the decision needs: the live Sonarr
   `sonarr_blocklist_path` (file vs URL), the Radarr upstream URL (option-B precedent),
   whether Cleanuparr supports a URL **and** a local regex overlay for one arr, the live
   blocklist + committed snapshot + current upstream (drift count + churn), and existing
   git-write-back constraints (#192 blocks bot pushes to main).
2. **Design + recommend** — lay out options **A** (periodic re-snapshot job, mirrors #179),
   **B** (repoint to upstream URL + keep only the local seriesId 40 regex overlay), **C**
   (drift-detector that alerts loudly + manual re-snapshot). Recommend one, weighing churn vs
   stale-snapshot risk vs maintenance. Adversarial review loop.
3. **🚦 BP1 — Decision gate (architecture)** — owner picks A / B / C (or a variant). The core
   of the issue.
4. **Implement** the chosen option on a feature branch off origin/main; `kustomize build`
   validation loop. No silent live-config mutation — live repoints are documented.
5. **🚦 BP2 — Ship + deploy gate** — owner reviews the exact diff; approves commit → PR →
   rebase → wait `validate` → merge → ArgoCD deploy.
6. **PR + merge** per the Epaflix merge policy (rebase onto origin/main, force-with-lease,
   `gh pr merge --merge`).
7. **Closeout** — verify ArgoCD Synced/Healthy and the local seriesId 40 regex preserved;
   edit the issue body with outcomes (never a new comment), open follow-ups cross-linked to
   #179 / #180, close #182 if satisfied.

## Guardrails

- Two mandatory breakpoints (low breakpointTolerance): the **A/B/C decision** and the
  **ship/deploy** gate.
- SOPS `.enc.yaml` only — no plaintext `kind: Secret`.
- Media-title scrub: series referred to only as **seriesId 40**.
- Concurrent branch `scrub-media-titles-servarr-docs` may touch the same files — rebase
  cleanly at ship time.
