/**
 * @process specializations/devops-sre-platform/newtarr-auth-bypass-declarative
 * @description Deliver issue #174 — make newtarr's "No Login Mode" flag
 *   (general.proxy_auth_bypass=true) declarative/enforced instead of living only
 *   in the live /config PVC.
 *
 *   Context (already true in git):
 *   - #134 put Authentik forward-auth in front of newtarr.epaflix.com (SSO-only);
 *     newtarr's own in-app login is disabled via general.proxy_auth_bypass=true.
 *   - #137 codified the whole /config as a SOPS seed Secret (newtarr-config-seed)
 *     consumed by a NON-CLOBBER `seed-config` initContainer. The seed's
 *     general.json ALREADY carries proxy_auth_bypass=true, so a FRESH PVC already
 *     restores the flag.
 *
 *   Remaining gap (this issue): the non-clobber seed SKIPS an existing
 *   general.json, so if a future image flips the default or the file already
 *   exists with the flag off, the SSO-only posture is not re-asserted. Fix =
 *   add a tiny, additive `enforce-auth-bypass` initContainer that runs AFTER
 *   seed-config and idempotently sets ONLY general.proxy_auth_bypass=true
 *   (merging, never clobbering other keys), plus a documented restore step in
 *   2-k3s/08.servarr/RECOVERY-newtarr-cleanuparr.md. NO Secret change is needed
 *   (seed already correct) → no secrets-rotation gate.
 *
 *   Live-change risk: the change adds a pod initContainer and lands on main,
 *   which ArgoCD (servarr App, selfHeal=true) auto-syncs → a newtarr pod
 *   restart. Gated by ONE mandatory deploy breakpoint before push/PR/merge.
 *   All prior phases (plan, implement-on-branch, validate, review) are
 *   non-destructive (feature branch only, nothing pushed).
 *
 * @inputs { repoRoot, repo, namespace, issue, branch }
 * @outputs { success, planConfirmed, implemented, validated, prUrl, merged, liveVerified, issueClosed, summary }
 *
 * @agent general-purpose (repo edits, kustomize/yaml validation, git/gh, kubectl live verify)
 * @skill requesting-code-review superpowers:requesting-code-review
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

// ---------------------------------------------------------------------------
// PHASE 1 — plan the exact change (read-only)
// ---------------------------------------------------------------------------
const planTask = defineTask('plan', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Confirm current state + author the EXACT enforce-auth-bypass change (read-only)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'GitOps/Kubernetes SRE on the Epaflix k3s cluster',
      task:
        'Produce the precise, minimal, additive change to make newtarr ' +
        'general.proxy_auth_bypass=true DECLARATIVE/ENFORCED for issue #' + args.issue + '. DO NOT edit files — read-only planning.',
      context: { ...args },
      instructions: [
        'Repo root: ' + args.repoRoot + '. Work read-only this phase.',
        'Read 2-k3s/08.servarr/newtarr/newtarr.yaml — note the existing NON-CLOBBER `seed-config` initContainer (busybox, only copies absent files) added by #137, and confirm the main container image is ghcr.io/elfhosted/newtarr:rolling (a python-based image).',
        'Read 2-k3s/08.servarr/RECOVERY-newtarr-cleanuparr.md to find where the #137 newtarr seed/restore section lives (so the doc note slots in next to it).',
        'Confirm via the decrypted seed (SOPS_AGE_KEY_FILE is set in env; `sops -d 2-k3s/08.servarr/_shared/secrets/newtarr-config-seed.enc.yaml`) that general.json already contains "proxy_auth_bypass": true. This means the SEED Secret needs NO change — DO NOT plan any .enc.yaml edit (avoids the secrets gate).',
        'Design a NEW initContainer named `enforce-auth-bypass` to be inserted in newtarr.yaml immediately AFTER `seed-config` (so it runs after the seed has populated a fresh PVC). Requirements: idempotent; merges into the existing /config/general.json setting ONLY proxy_auth_bypass=true (never clobbering other keys); creates a minimal file only if general.json is truly absent/corrupt; chowns the file back to 568:568; runs as root (runAsUser 0); mounts the `config` PVC at /config. Prefer reusing the app image ghcr.io/elfhosted/newtarr:rolling (guarantees python3 is present — busybox lacks json tooling) with a `python3` one-liner/heredoc, OR justify an alternative. Provide the FULL exact YAML block to insert, correctly indented to match the file.',
        'Design the RECOVERY doc addition: a short "Re-assert No Login Mode (proxy_auth_bypass)" note explaining the enforce-auth-bypass initContainer makes it self-healing on pod restart, plus the manual one-liner to force it live if ever needed (kubectl exec into the newtarr pod / patch general.json) — point-in-time drift caveat consistent with the #137 note.',
        'State explicitly why this is safe & minimal: additive only, no behavioural change on a correctly-configured PVC (logs "ok: already true"), and it strengthens (never weakens) the SSO-only posture.',
        'Return ONLY structured JSON. Save any captured file excerpts under tasks/' + taskCtx.effectId + '/ if useful.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['seedAlreadyCorrect', 'initContainerYaml', 'docNote', 'filesToChange', 'safetyRationale', 'summary'],
      properties: {
        seedAlreadyCorrect: { type: 'boolean' },
        currentState: { type: 'object' },
        initContainerYaml: { type: 'string' },
        docNote: { type: 'string' },
        filesToChange: { type: 'array', items: { type: 'string' } },
        secretChangeNeeded: { type: 'boolean' },
        safetyRationale: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 2 — adversarial verify of the plan (read-only)
// ---------------------------------------------------------------------------
const verifyPlanTask = defineTask('verify-plan', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Adversarially verify the proposed change is correct, minimal & non-clobbering (read-only)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Skeptical staff SRE reviewing a GitOps change before implementation',
      task: 'Try to REFUTE the proposed enforce-auth-bypass change. Default to confirmed=false if anything is unsound.',
      context: { plan: args.plan, repoRoot: args.repoRoot },
      instructions: [
        'Verify the proposed initContainer YAML actually parses & indents correctly into newtarr.yaml (mentally render the surrounding initContainers list).',
        'Verify ordering: enforce-auth-bypass MUST come AFTER seed-config so a fresh PVC is seeded first.',
        'Verify NON-CLOBBER: it sets ONLY proxy_auth_bypass and preserves all other general.json keys; it does not truncate/replace the file on a populated PVC; on a correct PVC it is a no-op (logs already-true).',
        'Verify the chosen image really provides the json tooling used (python3 in ghcr.io/elfhosted/newtarr:rolling). If busybox was proposed for JSON editing, REFUTE it.',
        'Verify file ownership ends at 568:568 and runAsUser 0 is used for the write.',
        'Verify NO Secret/.enc.yaml change is bundled in (seed already correct) — if one is, flag it (it would trip the SOPS pre-commit guard / secrets gate unnecessarily).',
        'Verify the change cannot WEAKEN security (it only ever forces bypass=true, i.e. keeps Authentik in front; it never re-enables newtarr login behind the trusted route).',
        'If sound, set confirmed=true and echo the final initContainer YAML + doc note (lightly corrected if needed). If not, list precise required changes.',
        'Return ONLY structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['confirmed', 'issues', 'finalInitContainerYaml', 'finalDocNote', 'summary'],
      properties: {
        confirmed: { type: 'boolean' },
        issues: { type: 'array', items: { type: 'string' } },
        finalInitContainerYaml: { type: 'string' },
        finalDocNote: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 3 — implement on a feature branch (NOT pushed) + local validation
// ---------------------------------------------------------------------------
const implementTask = defineTask('implement', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Create feature branch, apply the change, validate locally, commit (no push)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'GitOps engineer applying an approved manifest change on the Epaflix repo',
      task: 'Implement the verified enforce-auth-bypass change on a feature branch and commit it locally. DO NOT push.',
      context: { ...args },
      instructions: [
        'Repo root: ' + args.repoRoot + '. Ensure a clean tree, then create branch `' + args.branch + '` off origin/main (`git fetch origin && git checkout -B ' + args.branch + ' origin/main`).',
        'Edit 2-k3s/08.servarr/newtarr/newtarr.yaml: insert the verified `enforce-auth-bypass` initContainer block immediately AFTER the existing `seed-config` initContainer (and before `containers:`). Use EXACTLY this YAML block (fix only indentation to match the file):\n\n' + args.initContainerYaml,
        'Update the comment near the main container env (which references general.proxy_auth_bypass) so it notes the flag is now ENFORCED by the enforce-auth-bypass initContainer (declarative), cross-referencing #' + args.issue + '.',
        'Edit 2-k3s/08.servarr/RECOVERY-newtarr-cleanuparr.md: add the verified doc note next to the existing #137 newtarr seed/restore section. Doc note content to incorporate:\n\n' + args.docNote,
        'Do NOT touch any *.enc.yaml — no Secret change is needed.',
        'VALIDATE locally before committing: (a) `kustomize build` the servarr overlay if the ksops exec plugin + age key are available locally (flags: --enable-alpha-plugins --enable-exec; ksops binary may be at /opt/kustomize or absent locally — if absent, say so); (b) if a full ksops build is not possible locally, at minimum run a YAML syntax check on newtarr.yaml (e.g. `python3 -c "import yaml,sys; list(yaml.safe_load_all(open(...)))"`) and confirm the rendered Deployment is well-formed. Record exactly what validation ran and its result; the authoritative gate is the CI `validate` check at PR time.',
        'Run the SOPS pre-commit guard sanity (no plaintext kind: Secret introduced — there are none here).',
        'Stage ONLY the two changed files and commit with a clear message referencing #' + args.issue + '. End the commit body with the required trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. DO NOT push.',
        'Return ONLY structured JSON with the git diff summary, validation evidence (commands + output), the commit sha, and any problems.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'filesModified', 'validation', 'commitSha', 'diffSummary', 'success', 'summary'],
      properties: {
        branch: { type: 'string' },
        filesModified: { type: 'array', items: { type: 'string' } },
        validation: { type: 'object' },
        commitSha: { type: 'string' },
        diffSummary: { type: 'string' },
        success: { type: 'boolean' },
        problems: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 4 — code review (skill)
// ---------------------------------------------------------------------------
const reviewTask = defineTask('review', (args, taskCtx) => ({
  kind: 'skill',
  title: 'Code-review the committed diff for correctness, minimalism & GitOps safety',
  skill: {
    name: 'superpowers:requesting-code-review',
    context: {
      scope: 'The committed change on branch ' + args.branch + ' for issue #' + args.issue + ' (enforce-auth-bypass initContainer + RECOVERY doc note).',
      repoRoot: args.repoRoot,
      diffSummary: args.diffSummary,
      criteria: [
        'Change is additive, idempotent and non-clobbering of /config/general.json (only proxy_auth_bypass forced true).',
        'enforce-auth-bypass runs AFTER seed-config; correct image/json-tooling; chown 568:568.',
        'No Secret/.enc.yaml change; no plaintext secret.',
        'Cannot weaken the SSO-only posture; YAML valid; comments/doc accurate.',
        'Scope strictly matches issue #' + args.issue + ' (no unrelated edits).',
      ],
      instructions: [
        'Review the actual committed diff on the branch (git show / git diff origin/main...' + args.branch + ').',
        'Return findings with severity; explicitly state APPROVE or REQUEST-CHANGES with reasons.',
      ],
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 5 — ship: push, open PR, rebase, wait for `validate`, merge (DEPLOY)
// ---------------------------------------------------------------------------
const shipTask = defineTask('ship', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Push branch, open PR, rebase onto origin/main, await validate, merge (per Epaflix merge policy)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Release engineer following the Epaflix merge-commit + mandatory-rebase policy',
      task: 'Ship the approved change for issue #' + args.issue + ' to main via a PR and merge it.',
      context: { ...args },
      instructions: [
        'Repo root: ' + args.repoRoot + '. Repo: ' + args.repo + '. Branch: ' + args.branch + '.',
        'Push the branch: `git push -u origin ' + args.branch + '`.',
        'Open a PR against main with `gh pr create`. Title references #' + args.issue + '. Body: ## Summary (what + why, cross-link #134 SSO and #137 seed), ## Changes (enforce-auth-bypass initContainer + RECOVERY doc note; no Secret change), and a ## Test plan checklist with unchecked boxes: kustomize build / CI validate passes; ArgoCD servarr Synced/Healthy post-merge; newtarr pod has enforce-auth-bypass initContainer and it logs ok/enforced; live general.proxy_auth_bypass still true; newtarr.epaflix.com still behind Authentik (no double login / no open access). End body with the required PR footer: 🤖 Generated with [Claude Code](https://claude.com/claude-code).',
        'Enforce the merge policy: rebase the branch onto origin/main and `git push --force-with-lease` so the strict up-to-date branch protection is satisfied. Wait for the required `validate` check to pass (`gh pr checks <n> --watch` or poll `gh pr view <n> --json statusCheckRollup`). If `validate` fails, capture the log, fix the cause on the branch, re-push, and re-wait — do NOT merge on red.',
        'When `validate` is green and the branch is up to date, merge with `gh pr merge <n> --merge` (merge commit, NOT squash/rebase-merge). Confirm main now has the `Merge pull request #<n>` marker.',
        'Return ONLY structured JSON: prUrl, prNumber, validateStatus, merged (bool), mergeCommit, and any problems.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'prNumber', 'validateStatus', 'merged', 'summary'],
      properties: {
        prUrl: { type: 'string' },
        prNumber: { type: 'number' },
        validateStatus: { type: 'string' },
        merged: { type: 'boolean' },
        mergeCommit: { type: 'string' },
        problems: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 6 — verify live after ArgoCD sync (read-only) + tick PR test plan
// ---------------------------------------------------------------------------
const verifyLiveTask = defineTask('verify-live', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify ArgoCD sync + enforce-auth-bypass behaviour live; tick PR test-plan boxes',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE verifying a GitOps deploy on the Epaflix cluster',
      task: 'Confirm the merged change is reconciled and behaving, then record results in the PR test plan.',
      context: { ...args },
      instructions: [
        'Wait for ArgoCD to reconcile (servarr Application selfHeal=true). Confirm the servarr App is Synced + Healthy at/after the merge commit (argocd CLI or kubectl on the argocd Application).',
        'Confirm the newtarr Deployment rolled a new pod that includes the `enforce-auth-bypass` initContainer; fetch its init logs and confirm it printed "ok: ... already true" (expected on the live populated PVC) or "enforced: ...".',
        'Confirm live general.proxy_auth_bypass is still true: `kubectl -n ' + args.namespace + ' exec deploy/newtarr -- sh -c "cat /config/general.json"` and check the flag.',
        'Confirm newtarr.epaflix.com still sits behind Authentik forward-auth (no double login and not openly accessible). A quick unauthenticated curl should redirect to Authentik, not show the newtarr UI.',
        'Edit the PR #' + args.prNumber + ' DESCRIPTION (NEVER add a new comment) to tick each Test plan box with the result inline; strike through + annotate any box that is N/A.',
        'Return ONLY structured JSON: argoSynced, argoHealthy, initContainerPresent, initLog, flagLive, authentikInFront, testPlanUpdated, evidence, summary.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['argoSynced', 'argoHealthy', 'initContainerPresent', 'flagLive', 'authentikInFront', 'testPlanUpdated', 'summary'],
      properties: {
        argoSynced: { type: 'boolean' },
        argoHealthy: { type: 'boolean' },
        initContainerPresent: { type: 'boolean' },
        initLog: { type: 'string' },
        flagLive: { type: 'boolean' },
        authentikInFront: { type: 'boolean' },
        testPlanUpdated: { type: 'boolean' },
        evidence: { type: 'object' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 7 — wrap up: close #174, open any follow-ups (destructive-git/outward)
// ---------------------------------------------------------------------------
const wrapupTask = defineTask('wrapup', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Close issue #' + args.issue + ' with cross-links; open follow-ups if any',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Maintainer closing out delivered work per Epaflix repo policy',
      task: 'Close issue #' + args.issue + ' and queue any genuine follow-ups as gh issues.',
      context: { ...args },
      instructions: [
        'Repo: ' + args.repo + '. Close issue #' + args.issue + ' with a comment summarising the delivery: enforce-auth-bypass initContainer makes proxy_auth_bypass=true declarative/self-healing (PR ' + args.prUrl + ', merge ' + (args.mergeCommit || '') + '); cross-link #134 (SSO) and #137 (seed). Note the live-verify result: ' + args.liveSummary + '.',
        'Only open a follow-up gh issue if a REAL deferred item surfaced (use the ## Finding / ## Current state / ## Desired outcome / ## Notes shape and cross-link). The point-in-time-drift refresh cadence is already tracked by #179 — reference it, do NOT duplicate. Do not invent busy-work follow-ups.',
        'Return ONLY structured JSON: issueClosed (bool), followUps (array of urls or []), summary.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['issueClosed', 'followUps', 'summary'],
      properties: {
        issueClosed: { type: 'boolean' },
        followUps: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ===========================================================================
// ORCHESTRATOR
// ===========================================================================
export async function process(inputs, ctx) {
  const cfg = {
    repoRoot: '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
    repo: 'SpyrosPsarras/epaflix',
    namespace: 'servarr',
    issue: 174,
    branch: 'issue-174-newtarr-auth-bypass-declarative',
    ...inputs,
  };

  ctx.log('info', `Deliver #${cfg.issue}: make newtarr proxy_auth_bypass declarative/enforced`);

  // PHASE 1 — plan (read-only)
  let plan = await ctx.task(planTask, {
    repoRoot: cfg.repoRoot, namespace: cfg.namespace, issue: cfg.issue,
  });
  ctx.log('info', `Plan: seedAlreadyCorrect=${plan.seedAlreadyCorrect}; files=${(plan.filesToChange || []).join(', ')}`);

  // PHASE 2 — adversarial verify of the plan (read-only); one refine loop
  let vp = await ctx.task(verifyPlanTask, { repoRoot: cfg.repoRoot, plan });
  if (!vp.confirmed) {
    ctx.log('warn', `Plan not confirmed: ${(vp.issues || []).join('; ')} — replanning once.`);
    plan = await ctx.task(planTask, {
      repoRoot: cfg.repoRoot, namespace: cfg.namespace, issue: cfg.issue,
      previousIssues: vp.issues,
    });
    vp = await ctx.task(verifyPlanTask, { repoRoot: cfg.repoRoot, plan });
  }

  const initContainerYaml = vp.finalInitContainerYaml || plan.initContainerYaml;
  const docNote = vp.finalDocNote || plan.docNote;

  // PHASE 3 — implement on a feature branch (non-destructive; nothing pushed)
  const impl = await ctx.task(implementTask, {
    repoRoot: cfg.repoRoot, issue: cfg.issue, branch: cfg.branch,
    initContainerYaml, docNote,
  });
  ctx.log('info', `Implemented on ${impl.branch}: files=${(impl.filesModified || []).join(', ')}; commit=${impl.commitSha}`);

  // PHASE 4 — code review
  const review = await ctx.task(reviewTask, {
    repoRoot: cfg.repoRoot, issue: cfg.issue, branch: cfg.branch, diffSummary: impl.diffSummary,
  });
  ctx.log('info', `Review: ${review.verdict || review.decision || review.summary}`);

  // GATE — DEPLOY (push + PR + merge to main → ArgoCD auto-sync → pod restart).
  const gate = await ctx.breakpoint({
    question:
      'Approve SHIPPING the #' + cfg.issue + ' change (push branch → PR → rebase → wait for `validate` → merge to main)?\n\n' +
      'WHAT: add a tiny additive `enforce-auth-bypass` initContainer to newtarr.yaml that, after seed-config, idempotently forces ' +
      'general.proxy_auth_bypass=true (merge-only, never clobbers other keys) + a RECOVERY doc note. NO Secret change.\n\n' +
      'Files: ' + (impl.filesModified || []).join(', ') + '\n' +
      'Local validation: ' + JSON.stringify(impl.validation) + '\n' +
      'Review: ' + (review.summary || review.verdict || 'see review output') + '\n\n' +
      'Merging lands on main; ArgoCD (servarr selfHeal) will auto-sync and roll a newtarr pod. Proceed?',
    options: ['Approve & ship', 'Request changes', 'Abort'],
    expert: 'owner',
    tags: ['deploy', 'gitops', 'servarr', 'approval-gate'],
  });
  if (!gate.approved || (gate.response || '').toLowerCase().includes('abort')) {
    ctx.log('warn', 'Ship not approved — change committed on branch only, not pushed.');
    return {
      success: false, planConfirmed: vp.confirmed, implemented: impl.success, validated: false,
      merged: false, reason: 'ship-not-approved', branch: cfg.branch, diffSummary: impl.diffSummary,
      feedback: gate.response || gate.feedback || '',
    };
  }

  // PHASE 5 — ship (deploy)
  const ship = await ctx.task(shipTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo, issue: cfg.issue, branch: cfg.branch,
  });
  ctx.log('info', `Ship: pr=${ship.prUrl}; validate=${ship.validateStatus}; merged=${ship.merged}`);
  if (!ship.merged) {
    return {
      success: false, planConfirmed: vp.confirmed, implemented: impl.success, validated: true,
      prUrl: ship.prUrl, merged: false, reason: 'merge-blocked',
      problems: ship.problems || [], summary: ship.summary,
    };
  }

  // PHASE 6 — verify live + tick PR test plan
  let live = await ctx.task(verifyLiveTask, {
    namespace: cfg.namespace, issue: cfg.issue, prNumber: ship.prNumber, mergeCommit: ship.mergeCommit,
  });
  const liveOk = live.argoSynced && live.argoHealthy && live.flagLive && live.authentikInFront && live.initContainerPresent;
  if (!liveOk) {
    const recover = await ctx.breakpoint({
      question:
        'Post-merge live verification is NOT fully green.\n' +
        'argoSynced=' + live.argoSynced + ' argoHealthy=' + live.argoHealthy + ' initContainer=' + live.initContainerPresent +
        ' flagLive=' + live.flagLive + ' authentikInFront=' + live.authentikInFront + '\n' +
        'Summary: ' + live.summary + '\n\nHow to proceed?',
      options: ['Re-verify (transient/sync still settling)', 'Accept current state', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (recover.response || '').toLowerCase();
    if (recover.approved && r.includes('re-verify')) {
      live = await ctx.task(verifyLiveTask, {
        namespace: cfg.namespace, issue: cfg.issue, prNumber: ship.prNumber, mergeCommit: ship.mergeCommit, attempt: 2,
      });
    } else if (!recover.approved || r.includes('stop')) {
      return {
        success: false, planConfirmed: vp.confirmed, implemented: true, validated: true,
        prUrl: ship.prUrl, merged: true, liveVerified: false, reason: 'live-verify-stop', live,
      };
    }
  }

  // PHASE 7 — wrap up (close issue + follow-ups)
  const wrap = await ctx.task(wrapupTask, {
    repo: cfg.repo, issue: cfg.issue, prUrl: ship.prUrl, mergeCommit: ship.mergeCommit,
    liveSummary: live.summary,
  });
  ctx.log('info', `Wrap-up: issueClosed=${wrap.issueClosed}; followUps=${(wrap.followUps || []).join(', ') || 'none'}`);

  return {
    success: true,
    planConfirmed: vp.confirmed,
    implemented: true,
    validated: true,
    prUrl: ship.prUrl,
    merged: true,
    liveVerified: live.argoSynced && live.argoHealthy && live.flagLive && live.authentikInFront,
    issueClosed: wrap.issueClosed,
    followUps: wrap.followUps || [],
    summary:
      'Delivered #' + cfg.issue + ': enforce-auth-bypass initContainer makes newtarr proxy_auth_bypass=true ' +
      'declarative/self-healing. PR ' + ship.prUrl + ' merged; live verified=' + liveOk + '.',
    metadata: { processId: 'newtarr-auth-bypass-declarative', timestamp: ctx.now() },
  };
}
