# Babysitter instructions

Babysitter orchestrates complex multi-step workflows in this repo. The project profile lives at `.a5c/project-profile.json` (semi-autonomous, breakpoint tolerance `moderate` with `skipBreakpointsForKnownPatterns`, always break on destructive-git and deploy). Process definitions live under `.a5c/processes/`, runs under `.a5c/runs/`.

The **entire `.a5c/` tree is git-ignored** - it is local scaffolding, not repo content. Do not try to commit any of it, and do not treat a process definition as a durable record: a fresh clone has none of them. If a run produces something worth keeping (a runbook, a decision, a recipe), write it to `docs/` or `.github/instructions/` as part of the same PR.

Both profiles live under git-ignored trees (`~/.a5c/` and `.a5c/`), so they do not survive a fresh clone and the owner may reset them at any time. **They are a convenience, not the source of truth.** Anything that must hold regardless belongs in this file or in `.github/babysitter/`. Read profiles with `babysitter profile:read --user --json` / `--project --json`, never by importing SDK functions. Note that `profile:render` only prints known schema fields: free-form keys such as `preferences.notes` and `workflowPreferences` exist in the JSON but never appear in the rendered `.md`, so read the JSON when a rule seems to be missing.

Guardrails are in the root `CLAUDE.md` under `## Critical Rules` - merge policy, follow-up issues, no plaintext Secrets, ArgoCD adoption order. This file covers methodology and process selection only.

## Mandatory review gate

A run that writes, edits or deletes a tracked file must not finish on the implementer's own word. Every such process routes the write through the shared gate:

```
implement (agent A)
  -> verify   (shell task, real commands, stdout captured verbatim)
  -> review   (agent B: different agent name AND different model)
  -> pass? done : feed review.issues back as `feedback`, re-run implement
```

Four attempts. After that the run returns `{ success: false, stage: 'implementation-review' }` — it does not merge and does not open a PR. This is a hard gate, not advisory.

The loop lives in `.github/babysitter/review-gate.mjs` (tracked, so it survives a clone; it imports nothing, because the repo root has no `node_modules`). It is vendored from the canonical copy at `~/.pi/shared/skills/review-gate/`, which applies the same rule to every project — edit that one and re-run its `install.sh`, never the vendored copy. Import it rather than re-implementing the pattern:

```js
import { implementWithReview, buildReviewInstructions, REVIEW_OUTPUT_SCHEMA }
  from '../../.github/babysitter/review-gate.mjs';

const gate = await implementWithReview(ctx, {
  implementTask, verifyTask, reviewTask,
  args: { ...cfg, specVerbatim, approvedPlan },
});
if (!gate.passed) {
  return { success: false, stage: 'implementation-review', ...gate };
}
```

Fixture suite: `node --test .github/babysitter/test-review-gate.mjs`. Run it after any change to the gate.

Four rules the helper enforces so they cannot be skipped by accident:

- **The reviewer never sees the implementer's narrative.** `reviewerArgs` strips `implementation`, `summary`, `notes`, `narrative` and `design` from the argument bag. A reviewer reading a summary of the work is grading prose.
- **The verify step runs real commands.** `kustomize build`, `helm template`, hook fixtures, `kubectl --context epaflix`. This repo has no unit tests, so captured stdout is the only evidence that exists.
- **`failIf` is required.** `buildReviewInstructions` throws without a list of concrete, falsifiable failure conditions. A reviewer with no stated bar invents a low one.
- **The spec is interpolated verbatim at run time.** Read the issue body during the run and pass it through, so a later phase cannot quietly restate the acceptance criteria.

Worked example of the full shape, including an adversarial probe written by the process rather than by the implementer: `.a5c/processes/sops-hook-placeholder-secrets-800.js` (if still present locally — `.a5c` is git-ignored).

## Recommended Methodology

Evolutionary — mature IaC repo with no unit tests, so favor small reversible increments (adopt → soak → selfHeal-flip). Complemented by self-assessment + spec-driven processes for change gating.

## Recommended Processes

| Process path | When to use |
|--------------|-------------|
| `methodologies/gsd/map-codebase` | Onboard to the numbered `2-k3s` deploy-order + app-of-apps layout |
| `methodologies/gsd/plan-phase` + `execute-phase` + `verify-work` | Gated change workflow — verify = ArgoCD Synced/Healthy, zero live drift (compensates for no unit tests) |
| `methodologies/gsd/iterative-convergence` + `audit-milestone` | Model adopt → soak → selfHeal-flip pairs and periodic drift audits |
| `specializations/devops-sre-platform/iac-implementation` | Author/change Kustomize + Helm + ArgoCD manifests with GitOps guardrails |
| `specializations/devops-sre-platform/secrets-management` | Drives issue #29; preserve SOPS+age `*.enc.yaml` + pre-commit guard |
| `specializations/devops-sre-platform/incident-response` | Runbooks for recurring firefighting (Postgres sequence drift, servarr import races, qbittorrent VPN flap) |
| `specializations/devops-sre-platform/security-scanning` + `iac-testing` | Gate image-updater promotions; `kustomize build` / `helm template` validation |

## CI/CD

- Babysitter CI integration is **not configured** — no `ANTHROPIC_API_KEY` is available.
- To enable later: add an `ANTHROPIC_API_KEY` (or wire `CLAUDE_CODE_OAUTH_TOKEN` from a Claude subscription) and re-run `/babysitter:project-install`, or add a fork-guarded workflow that does NOT touch the existing secret-free `validate` gate.
