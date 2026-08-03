/**
 * @process specializations/devops-sre-platform/postgres-slot-wal-ceiling
 * @description Deliver Epaflix issue #304: bound `max_slot_wal_keep_size` on the CNPG
 *   `postgres-cluster` so a stale/invalidated HA replication slot can no longer retain
 *   unbounded WAL and fill a node disk (the 2026-06-14 worker-63 DiskPressure incident).
 *   One-line addition to postgresql.parameters in 2-k3s/06.postgres/cluster/postgres-cluster.yaml,
 *   delivered via the Epaflix branch -> PR -> rebase -> `validate` -> `gh pr merge --merge` flow;
 *   ArgoCD `postgres` app (selfHeal on) reconciles the Cluster CR; CNPG hot-reloads the param
 *   (reloadable, no failover). Verify the value is live on all 3 instances and no healthy
 *   replica slot was invalidated, then close #304.
 * @inputs { repoRoot, file, appPath, paramName, paramValue, repo, branch, issue, ns, clusterName }
 * @outputs { success, validated, merged, prUrl, paramLive, issueState }
 *
 * Risk: merging to main triggers ArgoCD selfHeal sync of the live Postgres cluster config
 * (a deploy). Gated by one mandatory deploy breakpoint before push/PR/merge. The param is
 * reloadable (ALTER SYSTEM / SIGHUP class) so no pod restart or failover is expected; a stale
 * slot may legitimately get invalidated by the new ceiling (that is the intended behaviour) —
 * the verify step distinguishes that from invalidating a HEALTHY replica's slot.
 *
 * @agent general-purpose (work executor for git/gh/kubectl/psql + classification/verification)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';

// ---------------------------------------------------------------------------
// Task definitions
// ---------------------------------------------------------------------------

// Phase 1 — analyze: validate the chosen ceiling is safe and produce the exact edit spec.
const analyzeTask = defineTask('analyze-change', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Validate max_slot_wal_keep_size ceiling + produce exact edit spec',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'CNPG/Postgres SRE on the Epaflix k3s cluster delivering issue #' + args.issue,
      task:
        'Validate that adding `' + args.paramName + ': ' + args.paramValue + '` to postgresql.parameters in ' +
        args.file + ' is correct and safe, and produce the exact, unambiguous edit to apply. DO NOT edit anything yet.',
      context: { ...args },
      instructions: [
        'Read ' + args.file + ' (the CNPG Cluster CR). Confirm postgresql.parameters currently sets wal_keep_size: 512MB and max_wal_size: 4GB and does NOT already set ' + args.paramName + '.',
        'Safety checks: the ceiling (' + args.paramValue + ') MUST be strictly greater than max_wal_size (4GB) so normal checkpoint WAL is never prematurely dropped; it must be well under a healthy node\'s free disk (~37GB free on worker-63 post-incident) so the ceiling protects rather than starves. Confirm ' + args.paramValue + ' satisfies both, or recommend a corrected value.',
        'Confirm the parameter is dynamically reloadable in PostgreSQL (postmaster reload context, no restart) so CNPG applies it without a failover. State the expected reconcile behaviour.',
        'Note the intended effect: a slot exceeding the ceiling becomes wal_status=lost/invalidated instead of retaining WAL unbounded — acceptable for a STALE slot, NOT for a healthy in-use replica. Healthy replica slots track ~0 bytes behind and will never approach the ceiling.',
        'Produce the exact edit: the YAML line to insert and the anchor line it should be inserted after (preserve the file\'s alphabetical-ish ordering within postgresql.parameters; place near max_wal_size / wal_keep_size). Quote values consistently with neighbours (CNPG renders sizes as bare or quoted strings — match the file).',
        'Return ONLY structured JSON, not a plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['safe', 'recommendedValue', 'reloadable', 'insertAfterLine', 'lineToInsert', 'risks', 'summary'],
      properties: {
        safe: { type: 'boolean' },
        recommendedValue: { type: 'string' },
        reloadable: { type: 'boolean' },
        insertAfterLine: { type: 'string' },
        lineToInsert: { type: 'string' },
        risks: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 2 — implement: branch off origin/main and apply the exact edit (reversible, local).
const implementTask = defineTask('implement-change', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Branch off origin/main + add the parameter to the Cluster CR',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'GitOps engineer on the Epaflix repo (merge-commit + mandatory-rebase policy)',
      task:
        'Create a feature branch off origin/main and add `' + args.paramName + ': ' + args.paramValue + '` to ' +
        'postgresql.parameters in ' + args.file + ' exactly as specified, then stage and commit ONLY that file.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. `git fetch origin` then create/switch branch `' + args.branch + '` off origin/main: `git switch -c ' + args.branch + ' origin/main` (if it exists, `git switch ' + args.branch + '` and reset to origin/main).',
        'Apply the edit from the spec: ' + (args.feedback ? '(REVISION REQUESTED: ' + args.feedback + ') ' : '') + 'insert `' + args.lineToInsert + '` after the line `' + args.insertAfterLine + '` in postgresql.parameters of ' + args.file + '. Match surrounding indentation (6 spaces) and quoting style exactly.',
        'Verify the YAML still parses and the parameter appears once: grep it back.',
        'Stage ONLY ' + args.file + ' (never `git add -A`, never stage .a5c/ or artifacts/). Confirm `git status --short` shows only that one file staged.',
        'Commit with message: "fix(postgres): bound max_slot_wal_keep_size to ' + args.paramValue + ' so a stale HA slot cannot fill a node disk (#' + args.issue + ')" and the required footer line: "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>".',
        'Return the branch name, commit sha, changed files, and the unified diff of the change.',
        'Return ONLY structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'commitSha', 'changedFiles', 'diff'],
      properties: {
        branch: { type: 'string' },
        commitSha: { type: 'string' },
        changedFiles: { type: 'array', items: { type: 'string' } },
        diff: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 3 — validate (shell, orchestrator-executed): render + secret guard. Quality gate.
const validateTask = defineTask('validate-render', (args, taskCtx) => ({
  kind: 'shell',
  title: 'kustomize build 06.postgres + plaintext-Secret guard (CI validate parity)',
  shell: {
    command:
      'cd ' + args.repoRoot + ' && ' +
      'kustomize build --enable-helm ' + args.appPath + ' > /tmp/render-304.yaml 2>/tmp/render-304.err; ' +
      'echo "RENDER_EXIT=$?"; ' +
      'grep -c "kind: Secret" /tmp/render-304.yaml || true; ' +
      'git --no-pager diff --staged --stat',
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 4 — deliver: push, open PR, rebase, wait validate, merge (Epaflix semi-linear policy).
const deliverTask = defineTask('deliver-pr', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Push + PR + rebase + wait validate + merge',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'GitOps engineer landing a PR under the Epaflix merge-commit + mandatory-rebase (semi-linear) policy',
      task:
        'Push branch ' + args.branch + ', open a PR to main on ' + args.repo + ', rebase onto origin/main + ' +
        'force-with-lease, wait for the required `validate` check to pass, then merge with a merge commit.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. `git push -u origin ' + args.branch + '` (use --force-with-lease if it already exists).',
        'Open PR: `gh pr create --repo ' + args.repo + ' --base main --head ' + args.branch + ' --title "fix(postgres): bound max_slot_wal_keep_size (#' + args.issue + ')" --body "..."`. Body: explain the #' + args.issue + ' root cause (stale HA slot _cnpg_postgres_cluster_10 pinned ~30G WAL -> worker-63 DiskPressure on 2026-06-14), the fix (bound ' + args.paramName + ' to ' + args.paramValue + ', > max_wal_size 4GB), reloadable so no failover, and "Closes #' + args.issue + '". End body with: 🤖 Generated with [Claude Code](https://claude.com/claude-code).',
        'Enforce semi-linear: `git fetch origin && git rebase origin/main` then `git push --force-with-lease`. Resolve trivially if needed (this is a 1-line addition; conflicts unlikely).',
        'Wait for the required `validate` check: poll `gh pr checks <num> --repo ' + args.repo + '` until validate is success (or fail). Give it generous time (CI clones + kustomize/helm pinned downloads can take a few min).',
        'When validate is green and the branch is up to date with main, merge: `gh pr merge <num> --repo ' + args.repo + ' --merge`. (Merge commit, NOT squash/rebase — the policy wants the "Merge pull request #N" marker.)',
        'Capture PR number, PR url, the validate check conclusion, and the merge result. If validate FAILS, do NOT merge — return merged=false with the failure detail.',
        'Return ONLY structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prNumber', 'prUrl', 'validateConclusion', 'merged', 'detail'],
      properties: {
        prNumber: { type: 'number' },
        prUrl: { type: 'string' },
        validateConclusion: { type: 'string' },
        merged: { type: 'boolean' },
        detail: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 5 — verify: ArgoCD synced + param live on all instances + no healthy-slot invalidation.
const verifyTask = defineTask('verify-live', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify ArgoCD synced + param live on all 3 instances + slot health',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'CNPG/ArgoCD SRE verifying the post-merge live state',
      task:
        'Confirm ArgoCD reconciled the Cluster CR change and CNPG applied `' + args.paramName + ' = ' + args.paramValue +
        '` to all running Postgres instances, with no regression to cluster health or healthy replica slots.',
      context: { ...args },
      instructions: [
        'You have kubectl + argocd + psql-via-kubectl-exec. The ArgoCD app owning ' + args.appPath + ' is `postgres` (selfHeal on).',
        'Trigger/await reconcile: `argocd app get postgres --refresh` (or `kubectl -n argocd get app postgres`). Wait until Synced+Healthy (give it a couple minutes; refresh if needed). Note if it needed a manual `argocd app sync postgres` (selfHeal should make it automatic).',
        'Confirm the Cluster is healthy: `kubectl get cluster ' + args.clusterName + ' -n ' + args.ns + '` shows 3/3 ready, healthy, with a primary. Record current primary.',
        'Verify the live value on EVERY instance pod (' + args.clusterName + '-9/-10/-11, whichever exist): `kubectl exec -n ' + args.ns + ' <pod> -c postgres -- psql -U postgres -tAc "SHOW ' + args.paramName + ';"`. ALL must return ' + args.paramValue + ' (Postgres may print as e.g. 8GB). If a pod still shows -1, the reload has not propagated — wait and re-check before failing.',
        'Confirm no HEALTHY replica slot was wrongly invalidated: on the primary, `psql -tAc "SELECT slot_name, active, wal_status, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) FROM pg_replication_slots;"`. Healthy HA slots should be active + reserved + ~0 bytes retained. wal_status=lost on an ACTIVE in-use slot = regression; lost on an already-dead/stale slot = acceptable (the intended protection).',
        'Confirm no unexpected pod restarts/failover happened due to the change (compare pod AGE / restart counts — the param is reloadable).',
        'Return ONLY structured JSON. If something is wrong, set healthy=false and explain.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['argoSynced', 'clusterHealthy', 'perInstanceValue', 'allInstancesMatch', 'slotsHealthy', 'healthy', 'summary'],
      properties: {
        argoSynced: { type: 'boolean' },
        clusterHealthy: { type: 'boolean' },
        perInstanceValue: { type: 'array', items: { type: 'object' } },
        allInstancesMatch: { type: 'boolean' },
        slotsHealthy: { type: 'boolean' },
        healthy: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 6 — closeout: comment + close the issue, tick any PR test-plan boxes.
const closeoutTask = defineTask('closeout-issue', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Comment + close issue #' + args.issue + ' with delivery evidence',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'GitOps engineer closing out a delivered Epaflix issue',
      task: 'Record the delivery on issue #' + args.issue + ' and close it; tick any unchecked test-plan boxes in the PR description (edit the PR body, never a new comment).',
      context: { ...args },
      instructions: [
        'If the PR (' + args.prUrl + ') description contains an unchecked test-plan/verification checklist, EDIT the PR description to tick the boxes whose steps were executed by the verify phase, appending the result inline. Use `gh pr edit` — NEVER add a new PR comment for this.',
        'Add a closing comment on issue #' + args.issue + ' on ' + args.repo + ' summarizing: PR merged (' + args.prUrl + '), ' + args.paramName + '=' + args.paramValue + ' confirmed live on all instances, cluster healthy, no failover. Then close the issue: `gh issue close ' + args.issue + ' --repo ' + args.repo + '` (if not already auto-closed by the "Closes #" in the merged PR — check first with `gh issue view`).',
        'Return ONLY structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['issueClosed', 'prBodyUpdated', 'detail'],
      properties: {
        issueClosed: { type: 'boolean' },
        prBodyUpdated: { type: 'boolean' },
        detail: { type: 'string' },
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
    file: '2-k3s/06.postgres/cluster/postgres-cluster.yaml',
    appPath: '2-k3s/06.postgres',
    paramName: 'max_slot_wal_keep_size',
    paramValue: '8GB',
    repo: 'SpyrosPsarras/epaflix',
    branch: 'postgres-slot-wal-ceiling-304',
    issue: '304',
    ns: 'postgres-system',
    clusterName: 'postgres-cluster',
    ...inputs,
  };

  ctx.log('info', `Delivering issue #${cfg.issue}: bound ${cfg.paramName}=${cfg.paramValue} on ${cfg.clusterName}`);

  // PHASE 1 — analyze (no mutation)
  const analysis = await ctx.task(analyzeTask, {
    repoRoot: cfg.repoRoot, file: cfg.file, paramName: cfg.paramName, paramValue: cfg.paramValue, issue: cfg.issue,
  });
  ctx.log('info', `Analysis: safe=${analysis.safe}; recommended=${analysis.recommendedValue}; reloadable=${analysis.reloadable}`);
  const effectiveValue = analysis.recommendedValue || cfg.paramValue;

  // PHASE 2/3 — implement + validate, with one refine loop on a failed validation gate.
  let impl, validation, validated = false, lastFeedback = null;
  for (let attempt = 0; attempt < 2 && !validated; attempt++) {
    impl = await ctx.task(implementTask, {
      repoRoot: cfg.repoRoot, file: cfg.file, paramName: cfg.paramName, paramValue: effectiveValue,
      branch: cfg.branch, issue: cfg.issue,
      insertAfterLine: analysis.insertAfterLine, lineToInsert: analysis.lineToInsert,
      feedback: lastFeedback || undefined,
    });
    validation = await ctx.task(validateTask, { repoRoot: cfg.repoRoot, appPath: cfg.appPath });
    // Validation passes when render exits 0 and no plaintext Secret leaked into the render
    // beyond the SOPS-managed ones (the orchestrator records pass/fail when posting the shell result).
    validated = validation.pass === true;
    if (!validated) lastFeedback = validation.detail || 'kustomize render or secret guard failed; fix the edit';
  }
  if (!validated) {
    ctx.log('error', 'Validation gate did not pass after retries — stopping before any push.');
    return { success: false, validated: false, reason: 'validation-failed', validation, impl };
  }
  ctx.log('info', `Validation passed. Branch ${impl.branch} @ ${impl.commitSha}`);

  // GATE 1 (deploy) — mandatory: merging to main triggers ArgoCD selfHeal sync of live Postgres config.
  const deployGate = await ctx.breakpoint({
    question:
      'Approve PUSH + PR + MERGE for issue #' + cfg.issue + '?\n\n' +
      'Change: add `' + cfg.paramName + ': ' + effectiveValue + '` to postgresql.parameters (> max_wal_size 4GB).\n' +
      'Branch: ' + impl.branch + ' (commit ' + impl.commitSha + ')\n' +
      'Files: ' + JSON.stringify(impl.changedFiles) + '\n\n' +
      'Diff:\n' + impl.diff + '\n\n' +
      'Safety: ' + analysis.summary + '\n' +
      'Validation: ' + (validation.detail || 'kustomize render OK, no plaintext Secret') + '\n\n' +
      'Merging to main makes the ArgoCD `postgres` app (selfHeal ON) reconcile the live Cluster config. ' +
      'The param is reloadable — no failover expected. Approve full delivery (push -> PR -> rebase -> validate -> merge)?',
    options: ['Approve delivery', 'PR-only (do not merge)', 'Abort'],
    expert: 'owner',
    tags: ['deploy', 'destructive-git', 'approval-gate'],
  });
  const gateResp = (deployGate.response || '').toLowerCase();
  if (!deployGate.approved || gateResp.includes('abort')) {
    ctx.log('warn', 'Delivery not approved — local branch/commit retained, nothing pushed.');
    return { success: false, validated: true, merged: false, reason: 'delivery-not-approved',
      feedback: deployGate.response || deployGate.feedback || '', branch: impl.branch };
  }
  const prOnly = gateResp.includes('pr-only') || gateResp.includes('pr only');

  // PHASE 4 — deliver (push + PR; merge unless PR-only)
  const delivery = await ctx.task(deliverTask, {
    repoRoot: cfg.repoRoot, branch: impl.branch, repo: cfg.repo, issue: cfg.issue,
    paramName: cfg.paramName, paramValue: effectiveValue, prOnly,
  });
  ctx.log('info', `Delivery: PR #${delivery.prNumber} validate=${delivery.validateConclusion} merged=${delivery.merged}`);

  if (prOnly || !delivery.merged) {
    return { success: !!delivery.prUrl, validated: true, merged: delivery.merged === true,
      prUrl: delivery.prUrl, reason: prOnly ? 'pr-only-by-owner' : 'not-merged', delivery };
  }

  // PHASE 5 — verify live, with an owner recovery gate on problems.
  let verify = await ctx.task(verifyTask, {
    repoRoot: cfg.repoRoot, appPath: cfg.appPath, paramName: cfg.paramName, paramValue: effectiveValue,
    ns: cfg.ns, clusterName: cfg.clusterName,
  });
  if (!verify.healthy) {
    const recover = await ctx.breakpoint({
      question:
        'Post-merge verification found problems for #' + cfg.issue + '.\n' +
        'ArgoCD synced: ' + verify.argoSynced + '; cluster healthy: ' + verify.clusterHealthy + '\n' +
        'All instances show ' + effectiveValue + ': ' + verify.allInstancesMatch + '\n' +
        'Slots healthy: ' + verify.slotsHealthy + '\n' +
        'Summary: ' + verify.summary + '\n\nProceed how?',
      options: ['Re-verify (transient/propagation lag)', 'Continue anyway', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (recover.response || '').toLowerCase();
    if (recover.approved && r.includes('re-verify')) {
      verify = await ctx.task(verifyTask, {
        repoRoot: cfg.repoRoot, appPath: cfg.appPath, paramName: cfg.paramName, paramValue: effectiveValue,
        ns: cfg.ns, clusterName: cfg.clusterName, attempt: 2,
      });
    } else if (!recover.approved || r.includes('stop')) {
      return { success: false, validated: true, merged: true, paramLive: false, reason: 'verification-stop', verify, delivery };
    }
  }

  // PHASE 6 — closeout
  const closeout = await ctx.task(closeoutTask, {
    repo: cfg.repo, issue: cfg.issue, prUrl: delivery.prUrl,
    paramName: cfg.paramName, paramValue: effectiveValue,
  });

  ctx.log('info', `Done. issueClosed=${closeout.issueClosed}; paramLive=${verify.allInstancesMatch}`);

  return {
    success: true,
    validated: true,
    merged: delivery.merged,
    prUrl: delivery.prUrl,
    paramLive: verify.allInstancesMatch === true,
    issueState: closeout.issueClosed ? 'closed' : 'open',
    paramName: cfg.paramName,
    paramValue: effectiveValue,
    verify,
    metadata: { processId: 'devops-sre-platform/postgres-slot-wal-ceiling', issue: cfg.issue, timestamp: ctx.now() },
  };
}
