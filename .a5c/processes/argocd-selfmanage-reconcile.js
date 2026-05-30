/**
 * @process specializations/devops-sre-platform/argocd-selfmanage-reconcile
 * @description Deliver Epaflix issue #46 via option B: resolve accumulated drift on the
 *   self-managed `argocd` ArgoCD Application, land the dormant argo-cd 9.5.17 chart bump
 *   (PR #91) through a deliberate maintenance-window sync (control-plane rollout), verify
 *   every sibling Application reconciles, then keep selfHeal OFF permanently (adopt #96's
 *   safety analysis) — documenting the decision and reconciling issues #46 / #96.
 * @inputs { repoRoot, appName, appPath, chartVersion, expectedAppVersion, appManifest,
 *           kustomization, issue46, issue96, branch, alwaysOutOfSyncNotes }
 * @outputs { success, synced, allAppsHealthy, prUrl, issue46State, issue96Action }
 *
 * Control-plane risk: syncing this app re-applies ArgoCD's own manifests and can restart
 * argocd-server / repo-server / application-controller. Gated by a mandatory deploy
 * breakpoint before the live sync, and a destructive-git breakpoint before push/PR/issue
 * mutation. selfHeal NEVER turns on here.
 *
 * @agent general-purpose (work executor for kubectl/argocd/git/gh + classification/verification)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';

// ---------------------------------------------------------------------------
// Task definitions
// ---------------------------------------------------------------------------

// Phase 1 — pre-flight snapshot + classified diff of the pending 9.5.17 render.
const preflightDiffTask = defineTask('preflight-diff', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Pre-flight snapshot + classify argocd app diff (9.5.17 vs live)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Kubernetes/ArgoCD SRE working on the Epaflix k3s cluster',
      task:
        'Capture the current state of the self-managed `argocd` Application and classify the ' +
        'OutOfSync diff between the repo chart render (argo-cd ' + args.chartVersion + ') and live, ' +
        'separating the REAL upgrade delta from the known always-OutOfSync noise. DO NOT sync anything.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. You have kubectl + argocd CLI configured.',
        'Capture: `kubectl -n argocd get applications` (all 16, record current SYNC/HEALTH per app as the pre-sync baseline).',
        'Capture live argocd-server image: `kubectl -n argocd get deploy argocd-server -o jsonpath="{.spec.template.spec.containers[0].image}"` (expect v3.4.2 now).',
        'Run `argocd app get ' + args.appName + ' --refresh` and `argocd app diff ' + args.appName + ' --core` (or `argocd app manifests`/kubectl diff if --core unavailable). Capture the full diff text.',
        'Render the repo target locally to confirm what 9.5.17 produces: `kustomize build --enable-helm ' + args.appPath + '` (or `kustomize build --enable-helm ' + args.appPath + ' | head` if huge). Note appVersion / image tag the chart resolves to (expected ~' + args.expectedAppVersion + ').',
        'CLASSIFY every diffed resource into: (a) NOISE — covered by the app ignoreDifferences (Service clusterIP/clusterIPs/status, Deployment/StatefulSet spec.replicas, and the three Secrets argocd-secret / argocd-initial-admin-secret / argocd-redis data/stringData) or otherwise runtime-generated; vs (b) REAL — genuine manifest changes that the sync will apply (image tags, env, RBAC, ConfigMap content, new/removed resources).',
        'Explicitly flag any CRD or ClusterRole/RBAC changes in the REAL set (CRDs are includeCRDs:false so should be none — confirm).',
        'Assess control-plane rollout impact: which Deployments/StatefulSet (server, repo-server, application-controller, redis, applicationset, notifications, dex) will the REAL diff restart.',
        'Save raw captures (app list, diff, render head) under tasks/' + taskCtx.effectId + '/ for traceability if helpful.',
        'Return ONLY the structured JSON result, not a plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['liveImage', 'preSyncApps', 'realChanges', 'noiseChanges', 'crdOrRbacChanges', 'rolloutImpact', 'summary'],
      properties: {
        liveImage: { type: 'string' },
        renderedAppVersion: { type: 'string' },
        preSyncApps: { type: 'array', items: { type: 'object' } },
        realChanges: { type: 'array', items: { type: 'string' } },
        noiseChanges: { type: 'array', items: { type: 'string' } },
        crdOrRbacChanges: { type: 'array', items: { type: 'string' } },
        rolloutImpact: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 2 — execute the live control-plane sync + wait for rollouts.
const liveSyncTask = defineTask('live-sync', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Sync argocd app (9.5.17) + wait for control-plane rollout',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Kubernetes/ArgoCD SRE performing an approved control-plane maintenance sync',
      task:
        'Perform the approved live sync of the self-managed `argocd` Application with ServerSideApply, ' +
        'then wait for all ArgoCD control-plane workloads to roll out cleanly.',
      context: { ...args },
      instructions: [
        'You have kubectl + argocd CLI. This is an APPROVED maintenance-window action.',
        'Sync: `argocd app sync ' + args.appName + ' --server-side --timeout 600` (server-side apply; the app uses ServerSideApply=true convention). If field-manager conflicts block it, retry with `--force` on chart-rendered resources only — never with --prune.',
        'NEVER pass --prune. Do not enable selfHeal. Do not touch CRDs.',
        'After sync, wait for rollouts (best-effort, ignore ones that do not exist): ' +
          '`kubectl -n argocd rollout status deploy/argocd-server --timeout=300s`, ' +
          '`deploy/argocd-repo-server`, `deploy/argocd-redis` (or statefulset), ' +
          '`statefulset/argocd-application-controller`, and applicationset/notifications/dex if present.',
        'Capture `argocd app get ' + args.appName + '` sync+health after the dust settles and the new argocd-server image tag.',
        'If the sync errors or a rollout does not converge within timeout, do NOT loop forever — capture the failure detail and return synced=false with the error.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['synced', 'newImage', 'rollouts', 'appSyncStatus', 'appHealthStatus', 'detail'],
      properties: {
        synced: { type: 'boolean' },
        newImage: { type: 'string' },
        rollouts: { type: 'array', items: { type: 'object' } },
        appSyncStatus: { type: 'string' },
        appHealthStatus: { type: 'string' },
        detail: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 3 — verify reconciliation across the whole cluster.
const verifyTask = defineTask('verify-reconcile', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify all Applications reconcile + argocd on 9.5.17',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'ArgoCD SRE verifying post-sync cluster health',
      task:
        'Verify the control-plane sync did not regress GitOps: every sibling Application is still ' +
        'Synced+Healthy (vs the pre-sync baseline), and the argocd app + control plane are healthy on the new version.',
      context: { ...args },
      instructions: [
        'Run `kubectl -n argocd get applications` and compare each app vs the preSyncApps baseline in context.',
        'KNOWN pre-existing states that are NOT regressions: `maintenance` Degraded and `renovate` Degraded were Degraded BEFORE the sync; `observability` was OutOfSync before. The `argocd` app itself may remain OutOfSync due to always-OutOfSync runtime noise — that is acceptable IF only-noise.',
        'A REGRESSION = any app that was Synced+Healthy pre-sync and is now OutOfSync/Degraded/Missing/Unknown, OR control-plane pods (argocd-server/repo-server/application-controller/redis) not Running/Ready.',
        'Confirm `kubectl -n argocd get pods` all Ready and the argocd-server image == ' + args.expectedImageHint + ' (the 9.5.17 render). Confirm argocd CLI/server still reachable: `argocd app list` returns.',
        'Set healthy=true ONLY if there are zero regressions. List any regressions precisely.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['healthy', 'regressions', 'appStates', 'controlPlaneReady', 'summary'],
      properties: {
        healthy: { type: 'boolean' },
        regressions: { type: 'array', items: { type: 'string' } },
        appStates: { type: 'array', items: { type: 'object' } },
        controlPlaneReady: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 4 — author doc/decision changes + local commit on a branch (reversible).
const prepareDocsTask = defineTask('prepare-docs', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Document selfHeal-stays-manual decision + local commit on branch',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer recording an ArgoCD self-management decision in the Epaflix repo',
      task:
        'Record the option-B decision (selfHeal stays OFF permanently on the self-managed argocd app, ' +
        'adopting #96) in git: update the app manifest header + any stale chart-version references, then ' +
        'create a branch and a single local commit. DO NOT push, DO NOT open a PR, DO NOT touch issues yet.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Respect CLAUDE.md: never commit secrets; this is docs-only.',
        'Edit ' + args.appManifest + ': the existing header still describes a future selfHeal flip. Rewrite the relevant header lines to state the SETTLED decision — selfHeal stays MANUAL/OFF permanently (control-plane safety, per #96); Renovate keeps the repo chart ahead of live, so chart bumps require a deliberate maintenance-window `argocd app sync`. Keep syncPolicy MANUAL (do NOT add an automated block). Fix any stale chart-version mentions (header says 9.5.14; repo is now ' + args.chartVersion + ').',
        'Check ' + args.kustomization + ' and the helm-values.yaml header for stale version comments; align them to ' + args.chartVersion + ' if they drifted. Record in the result that the 9.5.17 bump is now applied live.',
        'If feedback is present in context (a prior breakpoint rejection), incorporate it.',
        'Create branch ' + args.branch + ' off the current branch (if it already exists, reuse it). Stage only the intended files. Make ONE commit. End the commit message body with the Co-Authored-By trailer for Claude Opus 4.8 (1M context).',
        'Reference issues #46 and #96 in the commit message. Do NOT push.',
        'Return ONLY the structured JSON result with the branch name, changed files, and the commit SHA + the full proposed PR title/body and the proposed issue-reconciliation plan (what to do to #46 and #96) for owner review.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'changedFiles', 'commitSha', 'prTitle', 'prBody', 'issuePlan'],
      properties: {
        branch: { type: 'string' },
        changedFiles: { type: 'array', items: { type: 'string' } },
        commitSha: { type: 'string' },
        prTitle: { type: 'string' },
        prBody: { type: 'string' },
        issuePlan: {
          type: 'object',
          required: ['issue46', 'issue96'],
          properties: {
            issue46: { type: 'string' },
            issue96: { type: 'string' },
          },
        },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 5 — publish: push, open PR, reconcile issues #46 / #96.
const publishTask = defineTask('publish', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Push branch, open PR, reconcile issues #46 / #96',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer publishing an approved change to SpyrosPsarras/epaflix',
      task:
        'Execute the OWNER-APPROVED publish plan: push the branch, open a PR (repo policy: merge-commit, PR required, 0 approvals), ' +
        'and reconcile issues #46 and #96 exactly as approved.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Push the branch ' + args.branch + ' to origin.',
        'Open a PR to main with the approved title/body (provided in context as approvedPrTitle / approvedPrBody). Cross-link #46 and #96.',
        'Reconcile issues per the APPROVED plan in context (approvedIssuePlan): for #46 (issue ' + args.issue46 + ') — comment summarizing that option B was delivered (drift resolved, ' + args.chartVersion + ' landed, selfHeal stays manual) and close it as superseded by #96 if the plan says so. For #96 (issue ' + args.issue96 + ') — apply the approved action (edit description to reflect resolved state, OR close, OR delete-as-instructed). Use `gh issue edit/close/comment`.',
        'Do NOT merge the PR unless the approved plan explicitly says to.',
        'Return ONLY the structured JSON result with prUrl, issue46Final, issue96Final.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'issue46Final', 'issue96Final'],
      properties: {
        prUrl: { type: 'string' },
        issue46Final: { type: 'string' },
        issue96Final: { type: 'string' },
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
    appName: 'argocd',
    appPath: '2-k3s/11.argocd',
    appManifest: '2-k3s/11.argocd/apps/app-argocd.yaml',
    kustomization: '2-k3s/11.argocd/kustomization.yaml',
    chartVersion: '9.5.17',
    expectedAppVersion: 'v3.4.x',
    expectedImageHint: 'quay.io/argoproj/argocd:v3.4.x (9.5.17 render)',
    issue46: '46',
    issue96: '96',
    repo: 'SpyrosPsarras/epaflix',
    branch: 'argocd-selfmanage-keep-manual',
    ...inputs,
  };

  ctx.log('info', 'argocd self-management reconcile — option B (drift resolve + 9.5.17 land, selfHeal stays manual)');

  // PHASE 1 — pre-flight diff classification (no mutation)
  const diff = await ctx.task(preflightDiffTask, {
    repoRoot: cfg.repoRoot, appName: cfg.appName, appPath: cfg.appPath,
    chartVersion: cfg.chartVersion, expectedAppVersion: cfg.expectedAppVersion,
  });
  ctx.log('info', `Pre-flight: live=${diff.liveImage}; real changes=${(diff.realChanges || []).length}; noise=${(diff.noiseChanges || []).length}`);

  // GATE 1 (deploy) — mandatory: approve the live control-plane sync.
  const syncGate = await ctx.breakpoint({
    question:
      'Approve the LIVE control-plane sync of the `argocd` app to chart 9.5.17?\n\n' +
      'Live image: ' + diff.liveImage + '\n' +
      'Rendered: ' + (diff.renderedAppVersion || cfg.expectedAppVersion) + '\n' +
      'REAL changes (' + (diff.realChanges || []).length + '): ' + JSON.stringify(diff.realChanges) + '\n' +
      'CRD/RBAC changes: ' + JSON.stringify(diff.crdOrRbacChanges) + '\n' +
      'Rollout impact: ' + JSON.stringify(diff.rolloutImpact) + '\n' +
      'Noise (ignored): ' + (diff.noiseChanges || []).length + ' items\n\n' +
      'Summary: ' + diff.summary + '\n\n' +
      'This restarts ArgoCD control-plane workloads. Sync now? (selfHeal stays OFF regardless.)',
    options: ['Approve sync', 'Abort'],
    expert: 'owner',
    tags: ['deploy', 'control-plane', 'approval-gate'],
  });
  if (!syncGate.approved) {
    ctx.log('warn', 'Live sync not approved — aborting before any mutation.');
    return { success: false, synced: false, reason: 'sync-not-approved', feedback: syncGate.response || syncGate.feedback || '', diff };
  }

  // PHASE 2 — live sync + rollout wait
  const sync = await ctx.task(liveSyncTask, {
    appName: cfg.appName, chartVersion: cfg.chartVersion, expectedAppVersion: cfg.expectedAppVersion,
  });
  ctx.log('info', `Sync result: synced=${sync.synced}; newImage=${sync.newImage}; appHealth=${sync.appHealthStatus}`);

  // PHASE 3 — verify reconciliation, with an owner gate on regression.
  let verify = await ctx.task(verifyTask, {
    preSyncApps: diff.preSyncApps, expectedImageHint: cfg.expectedImageHint,
  });
  if (!sync.synced || !verify.healthy) {
    const recover = await ctx.breakpoint({
      question:
        'Post-sync verification found problems.\n' +
        'Sync ok: ' + sync.synced + ' (' + (sync.detail || '') + ')\n' +
        'Regressions: ' + JSON.stringify(verify.regressions) + '\n' +
        'Control plane ready: ' + verify.controlPlaneReady + '\n' +
        'Summary: ' + verify.summary + '\n\n' +
        'How do you want to proceed?',
      options: ['Re-verify (transient)', 'Continue anyway (accept state)', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const resp = (recover.response || '').toLowerCase();
    if (recover.approved && resp.includes('re-verify')) {
      verify = await ctx.task(verifyTask, { preSyncApps: diff.preSyncApps, expectedImageHint: cfg.expectedImageHint, attempt: 2 });
    } else if (!recover.approved || resp.includes('stop')) {
      return { success: false, synced: sync.synced, allAppsHealthy: false, reason: 'verification-stop', verify, sync };
    }
    // 'Continue anyway' falls through.
  }

  // PHASE 4 — author docs/decision + local commit on a branch (reversible).
  let docs = await ctx.task(prepareDocsTask, {
    repoRoot: cfg.repoRoot, appManifest: cfg.appManifest, kustomization: cfg.kustomization,
    chartVersion: cfg.chartVersion, branch: cfg.branch, issue46: cfg.issue46, issue96: cfg.issue96,
  });

  // GATE 2 (destructive-git / outward-facing) — approve push + PR + issue reconciliation.
  let lastFeedback = null;
  let publishApproved = false;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (lastFeedback) {
      docs = await ctx.task(prepareDocsTask, {
        repoRoot: cfg.repoRoot, appManifest: cfg.appManifest, kustomization: cfg.kustomization,
        chartVersion: cfg.chartVersion, branch: cfg.branch, issue46: cfg.issue46, issue96: cfg.issue96,
        feedback: lastFeedback, attempt: attempt + 1,
      });
    }
    const pubGate = await ctx.breakpoint({
      question:
        'Approve PUSH + PR + issue reconciliation?\n\n' +
        'Branch: ' + docs.branch + ' (commit ' + docs.commitSha + ')\n' +
        'Changed files: ' + JSON.stringify(docs.changedFiles) + '\n\n' +
        'PR title: ' + docs.prTitle + '\n' +
        'PR body:\n' + docs.prBody + '\n\n' +
        'Issue plan:\n  #46: ' + docs.issuePlan.issue46 + '\n  #96: ' + docs.issuePlan.issue96 + '\n\n' +
        'Note: per your instruction we review #96 and either edit its description or delete it. ' +
        'Confirm the #96 action above is what you want. Approve to push + open PR + apply issue actions?',
      options: ['Approve publish', 'Request changes', 'Skip publish (leave local only)'],
      expert: 'owner',
      tags: ['destructive-git', 'outward-facing', 'approval-gate'],
      previousFeedback: lastFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    const r = (pubGate.response || '').toLowerCase();
    if (pubGate.approved && r.includes('approve')) { publishApproved = true; break; }
    if (r.includes('skip')) {
      ctx.log('warn', 'Publish skipped by owner — local branch/commit retained.');
      return { success: true, synced: sync.synced, allAppsHealthy: verify.healthy, published: false, branch: docs.branch, commitSha: docs.commitSha, verify };
    }
    lastFeedback = pubGate.response || pubGate.feedback || 'Changes requested';
  }
  if (!publishApproved) {
    return { success: true, synced: sync.synced, allAppsHealthy: verify.healthy, published: false, reason: 'publish-not-approved-after-retries', branch: docs.branch, verify };
  }

  // PHASE 5 — publish (push + PR + issues)
  const pub = await ctx.task(publishTask, {
    repoRoot: cfg.repoRoot, branch: docs.branch, issue46: cfg.issue46, issue96: cfg.issue96,
    chartVersion: cfg.chartVersion, repo: cfg.repo,
    approvedPrTitle: docs.prTitle, approvedPrBody: docs.prBody, approvedIssuePlan: docs.issuePlan,
  });

  ctx.log('info', `Published: PR=${pub.prUrl}; #46=${pub.issue46Final}; #96=${pub.issue96Final}`);

  return {
    success: true,
    synced: sync.synced,
    allAppsHealthy: verify.healthy,
    published: true,
    prUrl: pub.prUrl,
    issue46State: pub.issue46Final,
    issue96Action: pub.issue96Final,
    newImage: sync.newImage,
  };
}
