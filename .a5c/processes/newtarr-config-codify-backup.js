/**
 * @process specializations/devops-sre-platform/newtarr-config-codify-backup
 * @description Deliver Epaflix issue #137: the newtarr v1.0.0 JSON integration config
 *   (Sonarr/Sonarr2/Radarr connections + global hunt settings) lives ONLY in the
 *   `newtarr-config` local-path PVC — none of it is in git, so a fresh/empty PVC means a
 *   manual rebuild (exactly what #131 cost). This run DECIDES the durable approach
 *   (codify into GitOps as a SOPS-encrypted seed, vs a scheduled PVC-contents backup +
 *   restore doc) and IMPLEMENTS it end-to-end with GitOps guardrails.
 *
 *   Flow: read-only investigate (inventory the live /config, detect secrets/API keys,
 *   confirm the app writes /config at runtime, survey existing repo backup patterns +
 *   targets) → OWNER decision breakpoint on approach → atomic plan → implement on a branch
 *   (SOPS-encrypt any secrets; update 08.servarr kustomization) → validate (kustomize build
 *   + sops pre-commit hook) with a refine loop → OWNER deploy gate → push+PR+merge (servarr
 *   ArgoCD app selfHeal reconciles) → post-merge verify (app Synced/Healthy, backup artifact
 *   produced OR seed present, restore path proven) → closeout (#137 + PR test plan + links).
 *
 * @inputs { repoRoot, appName, appManifest, appPath, ns, deployment, pvc, masterSsh, issue, repo, branch }
 * @outputs { success, approach, merged, prUrl, verified, issueState, followUps }
 *
 * Risk: this touches the servarr stack manifests. The config JSON holds *arr API keys, so
 * any codify-into-git path MUST be SOPS-encrypted (the pre-commit hook refuses plaintext
 * `kind: Secret`). Deploy is gated by a mandatory OWNER breakpoint (profile alwaysBreakOn:
 * deploy) after kustomize-build + sops-hook validation proves the change is clean.
 *
 * @agent general-purpose (kubectl-over-ssh / kustomize / sops / git / gh executor + analysis/verification)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

// ---------------------------------------------------------------------------
// Task definitions
// ---------------------------------------------------------------------------

// Phase 0 — read-only investigation: facts + a recommended approach with tradeoffs.
const investigateTask = defineTask('investigate', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Inventory live newtarr /config + survey repo backup patterns; recommend approach',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Kubernetes/ArgoCD SRE working on the Epaflix k3s cluster (servarr stack)',
      task:
        'Gather the FACTS needed to choose how to make the newtarr JSON integration config (issue #' +
        args.issue + ') survivable, then recommend ONE of two approaches. DO NOT change anything (read-only).',
      context: { ...args },
      instructions: [
        'kubectl access is over SSH: prefix every cluster command with `' + args.masterSsh + ' \'<kubectl ...>\'`. Run git/render/inspect commands locally from repoRoot=' + args.repoRoot + '.',
        'INVENTORY the live PVC contents: exec into the newtarr pod and list /config recursively with sizes — `' + args.masterSsh + " 'kubectl -n " + args.ns + " exec deploy/" + args.deployment + " -- sh -c \"ls -laR /config\"'`. Identify which JSON files hold integration config (e.g. sonarr.json, radarr.json, hunt/global settings, general.json with proxy_auth_bypass). Capture total size.",
        'SECRET DETECTION: dump the relevant JSON files (e.g. `... exec ... -- sh -c \"cat /config/<file>\"`) and determine whether they embed *arr API keys / tokens / passwords. This decides whether a codify-to-git path REQUIRES SOPS encryption. Do NOT print full secret values in the result — report only WHICH files contain secret-like fields and the field names.',
        'WRITE-OWNERSHIP: confirm newtarr writes /config at runtime (it does — hunt settings are persisted live per #135/#177). This rules out a plain read-only ConfigMap mounted AT /config; note any viable seed-then-app-owns pattern (e.g. initContainer copy into an emptyDir/PVC, or a one-shot restore Job).',
        'REPO PATTERN SURVEY (local): find existing backup/seed patterns to reuse — look at 2-k3s for CronJobs (e.g. maintenance/ postgres-sequence-audit, containerd-cleanup), the SOPS+age `*.enc.yaml` convention (.github/instructions/sops.instructions.md), and how the servarr kustomization wires resources. Identify candidate backup TARGETS (NFS media mounts, TrueNAS datasets such as the encrypted-backups dataset from #57, or a dedicated backup PVC).',
        'APP STATE: record the servarr ArgoCD Application sync/health and its syncPolicy (automated/selfHeal/prune) via ' + args.masterSsh + " 'kubectl -n argocd get application " + args.appName + " -o json' — so we know merging will reconcile, and whether prune is a concern.",
        'RECOMMEND exactly one approach with tradeoffs and a concrete design sketch for BOTH so the owner can choose:',
        '  (A) BACKUP: a scheduled CronJob (matching the repo maintenance-CronJob pattern) that snapshots /config to a durable target + a documented/scriptable RESTORE path. Lowest risk, secrets never enter git, but config still rebuilt-from-snapshot not declaratively in git.',
        '  (B) CODIFY: capture the current config JSON as a SOPS-encrypted `*.enc.yaml` seed in git + a restore/seed mechanism (initContainer or one-shot Job) that repopulates a fresh PVC. Declarative + git-tracked, but config drifts as the app writes live and requires re-snapshotting + SOPS rotation discipline.',
        'State your single recommendation and WHY, given this is a general PVC-only servarr pattern and the owner values low-risk additive change.',
        'Return ONLY the structured JSON result, not a plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['configFiles', 'containsSecrets', 'secretFiles', 'appWritesConfig', 'totalSizeBytes', 'appSync', 'appHealth', 'syncPolicy', 'candidateTargets', 'optionA', 'optionB', 'recommendation', 'recommendationReason', 'summary'],
      properties: {
        configFiles: { type: 'array', items: { type: 'string' } },
        containsSecrets: { type: 'boolean' },
        secretFiles: { type: 'array', items: { type: 'string' } },
        appWritesConfig: { type: 'boolean' },
        totalSizeBytes: { type: 'number' },
        appSync: { type: 'string' },
        appHealth: { type: 'string' },
        syncPolicy: { type: 'string' },
        candidateTargets: { type: 'array', items: { type: 'string' } },
        optionA: { type: 'string' },
        optionB: { type: 'string' },
        recommendation: { type: 'string' },
        recommendationReason: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 2 — atomic implementation plan for the chosen approach.
const planTask = defineTask('plan', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Author an atomic, file-level implementation plan for the approved approach',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer planning a GitOps change to the Epaflix servarr stack',
      task:
        'Turn the APPROVED approach for issue #' + args.issue + ' into a concrete, atomic, file-level plan ' +
        '(exact files to add/edit under ' + args.appPath + ', kustomization wiring, SOPS handling, restore mechanism, ' +
        'and a post-merge verification checklist). Planning only — do NOT edit files yet.',
      context: { ...args },
      instructions: [
        'The approved approach is in context as approach (and the full investigation as investigation). Honor the owner feedback verbatim.',
        'List each new/edited file with a one-line purpose. If the approach codifies secrets, the seed MUST be a `*.enc.yaml` SOPS+age file (single cluster recipient) per .github/instructions/sops.instructions.md — never plaintext `kind: Secret`.',
        'Specify the kustomization edit (which resource entries get added to 2-k3s/08.servarr/kustomization.yaml) and confirm `kustomize build --enable-helm 2-k3s/08.servarr` will still build.',
        'Specify the RESTORE/seed mechanism precisely and a documented manual restore runbook (where the doc lives, e.g. a newtarr README or RECOVERY note alongside the existing 2-k3s/08.servarr/RECOVERY-newtarr-cleanuparr.md).',
        'If feedback from a failed validation attempt is present in context, revise the plan to address it.',
        'Produce a PR Test Plan (checkbox items) for post-merge verification: servarr app Synced/Healthy, the new resource present live, the backup artifact produced OR the seed restorable, and a restore dry-run.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['files', 'kustomizationEdit', 'sopsNeeded', 'restoreMechanism', 'restoreDocPath', 'testPlan', 'summary'],
      properties: {
        files: { type: 'array', items: { type: 'object' } },
        kustomizationEdit: { type: 'string' },
        sopsNeeded: { type: 'boolean' },
        restoreMechanism: { type: 'string' },
        restoreDocPath: { type: 'string' },
        testPlan: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 3 — implement on a branch + local commit (reversible, no push).
const implementTask = defineTask('implement', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Implement the approved plan on a branch (SOPS-encrypt secrets) + local commit',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer authoring Kustomize/ArgoCD manifests + docs in the Epaflix repo',
      task:
        'Implement the approved plan for issue #' + args.issue + ' on branch ' + args.branch + ': create/edit the ' +
        'files, wire the kustomization, SOPS-encrypt any secret material, write the restore runbook, and make ONE ' +
        'local commit. Do NOT push, do NOT open a PR, do NOT touch issues.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. The approved plan is in context as plan; the investigation as investigation; the approach as approach.',
        'CRITICAL (CLAUDE.md): never commit plaintext secrets. Any seed/snapshot that embeds *arr API keys MUST be encrypted as a `*.enc.yaml` SOPS+age file (single cluster age recipient) using the recipe in .github/instructions/sops.instructions.md. The pre-commit hook (.github/hooks/check-sops-encrypted.sh) will REFUSE plaintext `kind: Secret` — ensure `./.github/hooks/install-hooks.sh` has been run so the guard is active.',
        'Create/edit exactly the files in the plan under ' + args.appPath + '. If adding a CronJob/Job/initContainer, match the existing repo manifest style (labels, namespace ' + args.ns + ', resources, restartPolicy) and reference the maintenance CronJobs as a template.',
        'Update 2-k3s/08.servarr/kustomization.yaml per the plan (add the new resource entries in a sensible position; keep the existing comment conventions). Do NOT remove unrelated entries.',
        'Write/extend the restore runbook at the path the plan specifies (documented manual restore steps; if a script, make it idempotent).',
        'VALIDATE locally before committing: `kustomize build --enable-helm ' + args.appPath + '` must succeed and include the new resources; and confirm no plaintext `kind: Secret` is staged (run the sops check hook or `git diff --cached`).',
        'If feedback from a prior attempt is present in context, address it specifically.',
        'Create branch ' + args.branch + ' off current (reuse if exists). Stage ONLY the intended files. Make ONE commit referencing #' + args.issue + '. End the commit body with the Co-Authored-By trailer for Claude Opus 4.8 (1M context).',
        'Return ONLY the structured JSON result: branch, changedFiles, commitSha, the kustomize-build result, sops/secret-safety confirmation, and the proposed PR title + body (the PR body MUST embed the plan testPlan as checkboxes).',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'changedFiles', 'commitSha', 'kustomizeBuildOk', 'secretsSafe', 'prTitle', 'prBody'],
      properties: {
        branch: { type: 'string' },
        changedFiles: { type: 'array', items: { type: 'string' } },
        commitSha: { type: 'string' },
        kustomizeBuildOk: { type: 'boolean' },
        secretsSafe: { type: 'boolean' },
        prTitle: { type: 'string' },
        prBody: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 4 — independent validation (kustomize build + sops hook + plan coverage).
const validateTask = defineTask('validate', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Validate the committed change (kustomize build, sops/secret guard, plan coverage)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC reviewer gating a servarr-stack change before deploy',
      task:
        'Independently verify the local commit on branch ' + args.branch + ' for issue #' + args.issue +
        ' is build-clean, secret-safe, and fully covers the approved plan. Read-only review (no edits, no push).',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Inspect the branch commit (`git show --stat`) and the diff.',
        'BUILD: run `kustomize build --enable-helm ' + args.appPath + '` and confirm it succeeds AND renders the new resources from the plan.',
        'SECRET GUARD: confirm NO plaintext `kind: Secret` is present in the changed files; any secret seed is a `*.enc.yaml` (SOPS-encrypted — header shows sops metadata, values are ENC[...]). Run .github/hooks/check-sops-encrypted.sh against the staged/committed files if available.',
        'COVERAGE: confirm every file in the approved plan exists with sensible content, the kustomization edit matches, and the restore runbook/mechanism is present and coherent.',
        'Set valid=true ONLY if build succeeds AND secrets are safe AND plan coverage is complete. Otherwise list precise issues to feed back into a refine attempt.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['valid', 'kustomizeBuildOk', 'secretsSafe', 'planCovered', 'issues', 'summary'],
      properties: {
        valid: { type: 'boolean' },
        kustomizeBuildOk: { type: 'boolean' },
        secretsSafe: { type: 'boolean' },
        planCovered: { type: 'boolean' },
        issues: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 6 — push + PR + merge (the deploy: servarr ArgoCD app selfHeal reconciles).
const publishMergeTask = defineTask('publish-merge', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Push branch, open PR, merge per Epaflix policy (servarr app reconciles)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer publishing an OWNER-APPROVED change to SpyrosPsarras/epaflix',
      task:
        'Push branch ' + args.branch + ', open a PR, and merge it per the Epaflix policy (merge-commit + ' +
        'mandatory rebase / semi-linear, PR required, 0 approvals). Merging is the deploy step — the servarr ' +
        'ArgoCD Application then reconciles the new manifests.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Rebase branch ' + args.branch + ' onto origin/main and `push --force-with-lease` (the strict up-to-date block + required `validate` check reject stale branches).',
        'Open a PR to main with the approved title/body (in context as approvedPrTitle / approvedPrBody). Cross-link issue #' + args.issue + ' and related #131/#135/#177.',
        'Wait for the required `validate` check to pass (if it flakes on the unpinned kustomize install, `gh run rerun --failed` per the known flake). Then merge with `gh pr merge --merge` (merge commit — never squash/rebase-merge).',
        'Capture the PR URL and merge commit SHA. Confirm the PR is MERGED before returning.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'merged', 'mergeSha'],
      properties: {
        prUrl: { type: 'string' },
        merged: { type: 'boolean' },
        mergeSha: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 7 — post-merge verification (app reconciled, artifact/seed proven, restore works).
const postMergeVerifyTask = defineTask('post-merge-verify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify servarr reconciled the change + prove the backup/restore path works',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'ArgoCD SRE verifying a post-merge GitOps reconcile on the servarr stack',
      task:
        'Confirm the merged change reached the live `' + args.appName + '` Application, the app stays ' +
        'Synced+Healthy, the new resource is live, and the backup/restore path actually works (not just exists).',
      context: { ...args },
      instructions: [
        'kubectl access is over SSH: prefix cluster commands with `' + args.masterSsh + ' \'<kubectl ...>\'`.',
        'Let the servarr ArgoCD app reconcile (selfHeal automated). Confirm sync.status==Synced and health.status==Healthy: `' + args.masterSsh + " 'kubectl -n argocd get application " + args.appName + " -o json'`.",
        'Confirm the new resource is live in ns ' + args.ns + ' (e.g. the CronJob exists / the seed Secret is present / the Job ran). Use the approach in context to know what to look for.',
        'PROVE THE PATH (the important part): if a backup CronJob — trigger a manual run (`kubectl -n ' + args.ns + ' create job --from=cronjob/<name> <name>-verify`), wait for completion, and confirm a non-empty backup artifact was written to the target. If a codified seed — perform a NON-DESTRUCTIVE restore dry-run that proves the seed can repopulate a fresh /config (e.g. restore into a temp dir/scratch location and diff the file set), WITHOUT disturbing the live newtarr config.',
        'Clean up any verify-only Job/scratch artifact you created. Confirm live newtarr is still running and its config untouched.',
        'Set verified=true ONLY if: app Synced+Healthy AND the new resource is live AND the backup artifact/seed-restore was proven. List any anomaly.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['verified', 'appSync', 'appHealth', 'resourceLive', 'pathProven', 'anomalies', 'summary'],
      properties: {
        verified: { type: 'boolean' },
        appSync: { type: 'string' },
        appHealth: { type: 'string' },
        resourceLive: { type: 'boolean' },
        pathProven: { type: 'boolean' },
        anomalies: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 8 — closeout: close #137 w/ outcome, tick PR test plan, open follow-ups.
const closeoutTask = defineTask('closeout', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Close #137 with outcome, update PR test plan, open follow-ups',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer reconciling issues/PR after a verified GitOps change in SpyrosPsarras/epaflix',
      task:
        'Record the verified outcome: close issue #' + args.issue + ', tick the PR test-plan checkboxes by ' +
        'EDITING the PR body (never a new comment), and open follow-up issues for any deferred work.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Repo is ' + args.repo + '.',
        'Comment on issue #' + args.issue + ' summarizing the verified result (approach chosen, what was merged, the proven backup/restore path) and CLOSE it. Cross-link #131/#135/#177.',
        'Edit the PR body (`gh pr edit --body`) to check off the Test Plan items that passed, recording observed evidence inline. NEVER add a separate comment for the test plan (CLAUDE.md rule).',
        'Open follow-up `gh issue`s (repo enhancement shape: ## Finding / ## Current state / ## Desired outcome / ## Notes) for any deferred work surfaced — e.g. extend the same backup/codify pattern to the OTHER servarr apps whose configs are PVC-only (this issue noted newtarr is just the most recent example), or a re-snapshot/SOPS-rotation cadence if the codify approach was chosen. Cross-link #' + args.issue + '.',
        'Return ONLY the structured JSON result with issueState, prUpdated, followUpIssues (array of urls).',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['issueState', 'prUpdated', 'followUpIssues'],
      properties: {
        issueState: { type: 'string' },
        prUpdated: { type: 'boolean' },
        followUpIssues: { type: 'array', items: { type: 'string' } },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// Orchestration
// ---------------------------------------------------------------------------

export async function process(inputs, ctx) {
  const cfg = {
    repoRoot: '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
    appName: 'servarr',
    appManifest: '2-k3s/11.argocd/apps/app-servarr.yaml',
    appPath: '2-k3s/08.servarr',
    ns: 'servarr',
    deployment: 'newtarr',
    pvc: 'newtarr-config',
    masterSsh: 'ssh ubuntu@192.168.10.51',
    issue: '137',
    repo: 'SpyrosPsarras/epaflix',
    branch: 'newtarr-config-codify-backup-137',
    ...inputs,
  };

  ctx.log('info', 'newtarr config codify/backup (#137) — investigate → decide → plan → implement → validate → deploy gate → merge → verify → closeout');

  // PHASE 0 — read-only investigation + recommendation.
  const inv = await ctx.task(investigateTask, {
    repoRoot: cfg.repoRoot, appName: cfg.appName, appPath: cfg.appPath, ns: cfg.ns,
    deployment: cfg.deployment, pvc: cfg.pvc, masterSsh: cfg.masterSsh, issue: cfg.issue,
  });
  ctx.log('info', `Investigate: secrets=${inv.containsSecrets}; size=${inv.totalSizeBytes}B; app=${inv.appSync}/${inv.appHealth}; rec=${inv.recommendation}`);

  // GATE 1 (architecture decision) — owner chooses the approach. This IS the issue's core "decide".
  const decision = await ctx.breakpoint({
    question:
      'Issue #137 — choose how to make newtarr\'s PVC-only config survivable.\n\n' +
      'Config files: ' + JSON.stringify(inv.configFiles) + '\n' +
      'Contains secrets (API keys): ' + inv.containsSecrets + ' (' + JSON.stringify(inv.secretFiles) + ')\n' +
      'App writes /config at runtime: ' + inv.appWritesConfig + ' | size: ' + inv.totalSizeBytes + 'B\n' +
      'servarr app: ' + inv.appSync + '/' + inv.appHealth + ' | syncPolicy: ' + inv.syncPolicy + '\n' +
      'Candidate targets: ' + JSON.stringify(inv.candidateTargets) + '\n\n' +
      'Option A (BACKUP): ' + inv.optionA + '\n\n' +
      'Option B (CODIFY): ' + inv.optionB + '\n\n' +
      'Recommendation: ' + inv.recommendation + ' — ' + inv.recommendationReason + '\n\n' +
      'Which approach should I implement?',
    options: ['Approach A — scheduled backup + restore doc', 'Approach B — codify SOPS seed + restore', 'Abort'],
    expert: 'owner',
    tags: ['architecture', 'approval-gate'],
  });
  if (!decision.approved || /abort/i.test(decision.response || '')) {
    ctx.log('warn', 'No approach approved — aborting before any mutation.');
    return { success: false, approach: null, merged: false, reason: 'not-approved', feedback: decision.response || decision.feedback || '', investigation: inv };
  }
  const approach = decision.response || inv.recommendation;
  ctx.log('info', `Approved approach: ${approach}`);

  // PHASE 2 — atomic plan.
  const plan = await ctx.task(planTask, {
    repoRoot: cfg.repoRoot, appPath: cfg.appPath, ns: cfg.ns, issue: cfg.issue,
    approach, investigation: inv, feedback: decision.feedback || undefined,
  });
  ctx.log('info', `Plan: ${(plan.files || []).length} files; sops=${plan.sopsNeeded}; restore=${plan.restoreMechanism}`);

  // PHASE 3+4 — implement + validate, with a refine loop (up to 3 attempts).
  let impl = null;
  let val = { valid: false, issues: ['not-run'] };
  let feedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    impl = await ctx.task(implementTask, {
      repoRoot: cfg.repoRoot, appPath: cfg.appPath, ns: cfg.ns, branch: cfg.branch, issue: cfg.issue,
      approach, plan, investigation: inv, feedback: feedback || undefined, attempt: attempt + 1,
    });
    ctx.log('info', `Implement (attempt ${attempt + 1}): branch=${impl.branch} commit=${impl.commitSha} build=${impl.kustomizeBuildOk} secretsSafe=${impl.secretsSafe}`);

    val = await ctx.task(validateTask, {
      repoRoot: cfg.repoRoot, appPath: cfg.appPath, branch: cfg.branch, issue: cfg.issue, plan,
    });
    ctx.log('info', `Validate (attempt ${attempt + 1}): valid=${val.valid}; issues=${JSON.stringify(val.issues)}`);
    if (val.valid) break;
    feedback = 'Validation failed: ' + JSON.stringify(val.issues) + '. ' + (val.summary || '');
  }

  // If still invalid after retries, ask the owner how to proceed.
  if (!val.valid) {
    const recover = await ctx.breakpoint({
      question:
        'The change still fails validation after 3 attempts.\n' +
        'Build ok: ' + val.kustomizeBuildOk + ' | secrets safe: ' + val.secretsSafe + ' | plan covered: ' + val.planCovered + '\n' +
        'Issues: ' + JSON.stringify(val.issues) + '\n' + (val.summary || '') + '\n\nHow to proceed?',
      options: ['Retry once more', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    if (recover.approved && /retry/i.test(recover.response || '')) {
      impl = await ctx.task(implementTask, {
        repoRoot: cfg.repoRoot, appPath: cfg.appPath, ns: cfg.ns, branch: cfg.branch, issue: cfg.issue,
        approach, plan, investigation: inv, feedback: (recover.feedback || feedback || '') + ' (owner-requested extra attempt)', attempt: 99,
      });
      val = await ctx.task(validateTask, { repoRoot: cfg.repoRoot, appPath: cfg.appPath, branch: cfg.branch, issue: cfg.issue, plan });
    }
    if (!val.valid) {
      return { success: false, approach, merged: false, reason: 'validation-failed', validation: val, impl };
    }
  }

  // GATE 2 (deploy) — mandatory: approve push+PR+merge (servarr selfHeal reconciles).
  const gate = await ctx.breakpoint({
    question:
      'Validation PASSED. Approve push + PR + MERGE for issue #' + cfg.issue + '?\n\n' +
      'Approach: ' + approach + '\n' +
      'Branch: ' + impl.branch + ' (commit ' + impl.commitSha + ')\n' +
      'Changed files: ' + JSON.stringify(impl.changedFiles) + '\n' +
      'kustomize build ok: ' + val.kustomizeBuildOk + ' | secrets safe: ' + val.secretsSafe + '\n\n' +
      'Merging is the deploy: the servarr ArgoCD Application (selfHeal) will reconcile the new manifests. Proceed?',
    options: ['Approve push + merge', 'Abort'],
    expert: 'owner',
    tags: ['deploy', 'approval-gate'],
  });
  if (!gate.approved || /abort/i.test(gate.response || '')) {
    ctx.log('warn', 'Deploy not approved — local commit left on branch, nothing pushed.');
    return { success: false, approach, merged: false, reason: 'deploy-not-approved', branch: impl.branch, commitSha: impl.commitSha };
  }

  // PHASE 6 — push + PR + merge (the deploy).
  const pub = await ctx.task(publishMergeTask, {
    repoRoot: cfg.repoRoot, branch: impl.branch, issue: cfg.issue, repo: cfg.repo,
    approvedPrTitle: impl.prTitle, approvedPrBody: impl.prBody,
  });
  ctx.log('info', `Merged: ${pub.merged}; PR=${pub.prUrl}; sha=${pub.mergeSha}`);

  // PHASE 7 — post-merge verify, with an owner gate on anomaly.
  let post = await ctx.task(postMergeVerifyTask, {
    repoRoot: cfg.repoRoot, appName: cfg.appName, ns: cfg.ns, masterSsh: cfg.masterSsh,
    deployment: cfg.deployment, pvc: cfg.pvc, approach,
  });
  if (!pub.merged || !post.verified) {
    const recover = await ctx.breakpoint({
      question:
        'Post-merge verification found problems.\n' +
        'Merged: ' + pub.merged + '\n' +
        'App: ' + post.appSync + '/' + post.appHealth + ' | resource live: ' + post.resourceLive + ' | path proven: ' + post.pathProven + '\n' +
        'Anomalies: ' + JSON.stringify(post.anomalies) + '\n' + (post.summary || '') + '\n\nHow to proceed?',
      options: ['Re-verify (transient)', 'Continue to closeout (accept state)', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (recover.response || '').toLowerCase();
    if (recover.approved && r.includes('re-verify')) {
      post = await ctx.task(postMergeVerifyTask, {
        repoRoot: cfg.repoRoot, appName: cfg.appName, ns: cfg.ns, masterSsh: cfg.masterSsh,
        deployment: cfg.deployment, pvc: cfg.pvc, approach, attempt: 2,
      });
    } else if (!recover.approved || r.includes('stop')) {
      return { success: false, approach, merged: pub.merged, prUrl: pub.prUrl, reason: 'verification-stop', post };
    }
  }

  // PHASE 8 — closeout: close #137, tick PR test plan, open follow-ups.
  const close = await ctx.task(closeoutTask, {
    repoRoot: cfg.repoRoot, issue: cfg.issue, repo: cfg.repo, prUrl: pub.prUrl, approach,
  });
  ctx.log('info', `Closeout: #137=${close.issueState}; follow-ups=${JSON.stringify(close.followUpIssues)}`);

  return {
    success: true,
    approach,
    merged: pub.merged,
    prUrl: pub.prUrl,
    verified: post.verified,
    issueState: close.issueState,
    followUps: close.followUpIssues,
  };
}
