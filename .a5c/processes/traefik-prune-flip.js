/**
 * @process specializations/devops-sre-platform/traefik-prune-flip
 * @description Deliver Epaflix issue #51: flip the `traefik` ArgoCD Application from
 *   prune:false to prune:true after its (overdue) 48h clean soak. Pre-flip safety verify
 *   (nothing tracked-but-not-in-git would be wrongly pruned; the known untracked
 *   `https-redirect-epaflix` orphan is confirmed not ArgoCD-owned so prune won't touch it),
 *   deploy breakpoint, branch+commit, push+PR+merge (app-of-apps #48 is CLOSED so the
 *   merged manifest reconciles via app-of-apps selfHeal — no manual kubectl apply), then
 *   post-merge verify + closeout (close #51, open a drift follow-up for the orphan).
 * @inputs { repoRoot, appName, appManifest, appPath, ns, masterSsh, issue, repo, branch }
 * @outputs { success, safeToFlip, merged, prUrl, livePrune, issue51State, followUpIssue }
 *
 * Ingress-path risk: traefik is the edge. The flip enables prune on a selfHeal app, so any
 * tracked resource absent from git would be DELETED on next sync. Gated by a mandatory
 * deploy breakpoint before push/merge, after a pre-flip verify proves zero tracked orphans.
 *
 * @agent general-purpose (kubectl-over-ssh / git / gh executor + classification/verification)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';

// ---------------------------------------------------------------------------
// Task definitions
// ---------------------------------------------------------------------------

// Phase 1 — pre-flip safety verification (NO mutation).
const preflightVerifyTask = defineTask('preflight-verify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify prune:true is safe for the traefik app (no tracked orphans)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Kubernetes/ArgoCD SRE working on the Epaflix k3s cluster',
      task:
        'Prove that flipping the `' + args.appName + '` ArgoCD Application to prune:true is SAFE — i.e. ' +
        'no resource that ArgoCD TRACKS for this app is missing from git (which prune would delete). DO NOT change anything.',
      context: { ...args },
      instructions: [
        'kubectl access is over SSH: prefix every cluster command with `' + args.masterSsh + ' \'<kubectl ...>\'`. Run git/render commands locally from repoRoot=' + args.repoRoot + '.',
        'Confirm current app state: `' + args.masterSsh + " 'kubectl -n argocd get application " + args.appName + " -o json'` — record sync.status (expect Synced), health.status (expect Healthy), and spec.syncPolicy.automated (expect selfHeal:true, prune:false).",
        'Build the TRACKED resource list from `.status.resources[]` of that Application JSON (kind/name/namespace/status). Every entry should be status=Synced.',
        'Render git source to get the DESIRED set: `kustomize build --enable-helm ' + args.appPath + '` (helmCharts inflation). List every rendered object (kind/name/namespace).',
        'PRUNE-SAFETY CHECK: for each TRACKED resource, confirm it also exists in the rendered git set. Any tracked resource NOT present in git is a "would-be-pruned" item — list it. (Expected result: NONE.)',
        'ORPHAN CHECK: list all traefik CRs cluster-wide in the app namespace ' + args.ns + ': `' + args.masterSsh + " 'kubectl get ingressroute,ingressroutetcp,middleware,middlewaretcp,serverstransport,tlsoption -n " + args.ns + " -o json'`. For each, read metadata.labels[\"app.kubernetes.io/instance\"] and metadata.annotations[\"argocd.argoproj.io/tracking-id\"]. A resource is ArgoCD-OWNED-by-traefik only if one of those references the traefik app; otherwise it is an untracked ORPHAN that prune will NOT delete.",
        'Known orphan to confirm: `IngressRoute/https-redirect-epaflix` in ' + args.ns + ' — verify it is NOT in git and has NO argocd tracking label/annotation (so prune is harmless to it). Flag any OTHER untracked resource you find in ' + args.ns + '.',
        'Set safeToFlip=true ONLY if: app is Synced+Healthy AND the would-be-pruned list is empty. Untracked orphans do NOT block safety (prune ignores them) but MUST be reported.',
        'Return ONLY the structured JSON result, not a plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['safeToFlip', 'appSync', 'appHealth', 'currentPrune', 'trackedCount', 'wouldBePruned', 'untrackedOrphans', 'summary'],
      properties: {
        safeToFlip: { type: 'boolean' },
        appSync: { type: 'string' },
        appHealth: { type: 'string' },
        currentPrune: { type: 'boolean' },
        trackedCount: { type: 'number' },
        wouldBePruned: { type: 'array', items: { type: 'string' } },
        untrackedOrphans: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 2 — author the flip on a branch + local commit (reversible, no push).
const prepareChangeTask = defineTask('prepare-change', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Flip prune:true in app-traefik.yaml + local commit on branch',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer editing ArgoCD Application manifests in the Epaflix repo',
      task:
        'Flip `syncPolicy.automated.prune` from false to true in ' + args.appManifest + ', update the ' +
        'header comment to reflect the settled decision, then create a branch and ONE local commit. ' +
        'Do NOT push, do NOT open a PR, do NOT touch issues.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Respect CLAUDE.md: never commit secrets; this is a one-line policy change.',
        'Edit ' + args.appManifest + ': change `prune: false` to `prune: true` under spec.syncPolicy.automated. Update the `# Sync:` header comment line that says prune is disabled — state prune is now enabled after the 48h soak (issue #' + args.issue + '), keep the selfHeal/client-side-apply notes intact.',
        'Do NOT change any other field (selfHeal stays true, ignoreDifferences untouched, source/destination untouched).',
        'If feedback is present in context (a prior breakpoint rejection), incorporate it.',
        'Create branch ' + args.branch + ' off the current branch (reuse if it exists). Stage ONLY ' + args.appManifest + '. Make ONE commit referencing #' + args.issue + '. End the commit message body with the Co-Authored-By trailer for Claude Opus 4.8 (1M context).',
        'Return ONLY the structured JSON result: branch, changedFiles, commitSha, the exact prune diff (before/after lines), plus the proposed PR title and body. The PR body MUST include a Test Plan section with checkbox items for the post-merge verification (app-of-apps reconciles the spec, traefik shows prune:true live, app stays Synced+Healthy, no unexpected pruning, https-redirect-epaflix orphan still present).',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'changedFiles', 'commitSha', 'diff', 'prTitle', 'prBody'],
      properties: {
        branch: { type: 'string' },
        changedFiles: { type: 'array', items: { type: 'string' } },
        commitSha: { type: 'string' },
        diff: { type: 'string' },
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

// Phase 3 — push + PR + merge (the deploy: app-of-apps reconciles the merged spec).
const publishMergeTask = defineTask('publish-merge', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Push branch, open PR, merge per policy (triggers app-of-apps reconcile)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer publishing an OWNER-APPROVED change to SpyrosPsarras/epaflix',
      task:
        'Push the branch, open a PR, and merge it per the Epaflix policy (merge-commit only, PR required, ' +
        '0 approvals, admin bypass authorized). Merging is the deploy step — app-of-apps selfHeal then ' +
        'reconciles the new traefik Application spec.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Push branch ' + args.branch + ' to origin.',
        'Open a PR to main with the approved title/body (in context as approvedPrTitle / approvedPrBody). Cross-link issue #' + args.issue + '.',
        'Merge using the authorized flow: `gh pr merge --admin --merge` (merge commit). Do NOT squash/rebase.',
        'Capture the PR URL and the merge commit SHA. Confirm the PR is MERGED before returning.',
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

// Phase 4 — post-merge verification (app-of-apps reconciled, prune live, no collateral).
const postMergeVerifyTask = defineTask('post-merge-verify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify app-of-apps reconciled prune:true live + no unexpected pruning',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'ArgoCD SRE verifying a post-merge GitOps reconcile on the ingress path',
      task:
        'Confirm the merged prune:true reached the live `' + args.appName + '` Application via app-of-apps, ' +
        'the app is still Synced+Healthy, and nothing was unexpectedly pruned.',
      context: { ...args },
      instructions: [
        'kubectl access is over SSH: prefix cluster commands with `' + args.masterSsh + ' \'<kubectl ...>\'`.',
        'Allow app-of-apps to reconcile; if needed, nudge: `' + args.masterSsh + " 'kubectl -n argocd get application app-of-apps " + args.appName + " -o json'`. You may wait/poll a short while (app-of-apps selfHeal is automated). Optionally trigger a refresh by annotating, but do NOT force-sync with prune flags manually.",
        'Confirm live spec: `' + args.masterSsh + " 'kubectl -n argocd get application " + args.appName + " -o jsonpath=\"{.spec.syncPolicy.automated.prune}\"'` returns true.",
        'Confirm app health: sync.status == Synced and health.status == Healthy. Confirm the traefik Deployment is still Available and pods Ready (`' + args.masterSsh + " 'kubectl -n " + args.ns + " get deploy,pods'`).",
        'COLLATERAL CHECK: confirm the tracked resource count is unchanged vs the pre-flip trackedCount in context (no tracked resource got pruned). Confirm `IngressRoute/https-redirect-epaflix` in ' + args.ns + ' STILL EXISTS (untracked orphan must survive prune).',
        'Set verified=true ONLY if: livePrune==true AND app Synced+Healthy AND no tracked resource was pruned AND the orphan survives. List any anomaly.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['verified', 'livePrune', 'appSync', 'appHealth', 'orphanSurvives', 'anomalies', 'summary'],
      properties: {
        verified: { type: 'boolean' },
        livePrune: { type: 'boolean' },
        appSync: { type: 'string' },
        appHealth: { type: 'string' },
        orphanSurvives: { type: 'boolean' },
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

// Phase 5 — closeout: close #51 w/ outcome, tick PR test plan, open orphan drift follow-up.
const closeoutTask = defineTask('closeout', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Close #51 with outcome, update PR test plan, open orphan drift follow-up',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer reconciling issues/PR after a verified GitOps change in SpyrosPsarras/epaflix',
      task:
        'Record the verified outcome: close issue #' + args.issue + ', tick the PR test-plan checkboxes by ' +
        'EDITING the PR body (never a new comment), and open a follow-up issue for the untracked ' +
        '`https-redirect-epaflix` drift in ' + args.ns + '.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Repo is ' + args.repo + '.',
        'Comment on issue #' + args.issue + ' summarizing the verified result (prune:true live via app-of-apps, app Synced+Healthy, zero tracked resources pruned, orphan survived) and CLOSE it.',
        'Edit the PR body (gh pr edit --body) to check off the Test Plan items that passed, recording the observed evidence inline. Do NOT add a separate comment for the test plan.',
        'Open a NEW follow-up gh issue for the drift: `IngressRoute/https-redirect-epaflix` exists live in ' + args.ns + ' but is NOT in git and is NOT ArgoCD-tracked. Use the repo enhancement-issue shape (## Finding / ## Current state / ## Desired outcome / ## Notes). Desired outcome = either codify it into 2-k3s/05.traefik-deployment git source so it becomes tracked, or delete it if redundant with redirect-https middleware. Cross-link #' + args.issue + '.',
        'Return ONLY the structured JSON result with issue51State, prUpdated, followUpIssueUrl.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['issue51State', 'prUpdated', 'followUpIssueUrl'],
      properties: {
        issue51State: { type: 'string' },
        prUpdated: { type: 'boolean' },
        followUpIssueUrl: { type: 'string' },
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
    appName: 'traefik',
    appManifest: '2-k3s/11.argocd/apps/app-traefik.yaml',
    appPath: '2-k3s/05.traefik-deployment',
    ns: 'traefik-system',
    masterSsh: 'ssh ubuntu@192.168.10.51',
    issue: '51',
    repo: 'SpyrosPsarras/epaflix',
    branch: 'traefik-prune-flip',
    ...inputs,
  };

  ctx.log('info', 'traefik prune flip (#51) — verify-safe → deploy gate → merge → verify → closeout');

  // PHASE 1 — pre-flip safety verify (no mutation).
  const verify = await ctx.task(preflightVerifyTask, {
    repoRoot: cfg.repoRoot, appName: cfg.appName, appPath: cfg.appPath, ns: cfg.ns, masterSsh: cfg.masterSsh,
  });
  ctx.log('info', `Pre-flip: safe=${verify.safeToFlip}; sync=${verify.appSync}/${verify.appHealth}; wouldBePruned=${(verify.wouldBePruned || []).length}; orphans=${(verify.untrackedOrphans || []).length}`);

  // GATE 1 (deploy) — mandatory: approve the flip + merge. Merging deploys via app-of-apps.
  const gate = await ctx.breakpoint({
    question:
      'Approve flipping `traefik` ArgoCD Application to prune:true and MERGING it (#' + cfg.issue + ')?\n\n' +
      'Pre-flip safety: ' + (verify.safeToFlip ? 'SAFE ✅' : 'NOT SAFE ⚠️') + '\n' +
      'App state: ' + verify.appSync + ' / ' + verify.appHealth + ' (prune currently ' + verify.currentPrune + ')\n' +
      'Tracked resources: ' + verify.trackedCount + '\n' +
      'Would-be-pruned (tracked-but-not-in-git): ' + JSON.stringify(verify.wouldBePruned) + '\n' +
      'Untracked orphans (prune ignores these): ' + JSON.stringify(verify.untrackedOrphans) + '\n\n' +
      'Summary: ' + verify.summary + '\n\n' +
      'traefik is the ingress edge. Merging triggers app-of-apps selfHeal to apply prune:true live. Proceed?',
    options: ['Approve flip + merge', 'Abort'],
    expert: 'owner',
    tags: ['deploy', 'ingress', 'approval-gate'],
  });
  if (!gate.approved) {
    ctx.log('warn', 'Flip not approved — aborting before any mutation.');
    return { success: false, safeToFlip: verify.safeToFlip, merged: false, reason: 'not-approved', feedback: gate.response || gate.feedback || '', verify };
  }

  // PHASE 2 — author the flip on a branch + local commit (reversible).
  const change = await ctx.task(prepareChangeTask, {
    repoRoot: cfg.repoRoot, appManifest: cfg.appManifest, branch: cfg.branch, issue: cfg.issue,
  });
  ctx.log('info', `Prepared: branch=${change.branch} commit=${change.commitSha}`);

  // PHASE 3 — push + PR + merge (the deploy).
  const pub = await ctx.task(publishMergeTask, {
    repoRoot: cfg.repoRoot, branch: change.branch, issue: cfg.issue, repo: cfg.repo,
    approvedPrTitle: change.prTitle, approvedPrBody: change.prBody,
  });
  ctx.log('info', `Merged: ${pub.merged}; PR=${pub.prUrl}; sha=${pub.mergeSha}`);

  // PHASE 4 — post-merge verify, with an owner gate on anomaly.
  let post = await ctx.task(postMergeVerifyTask, {
    appName: cfg.appName, ns: cfg.ns, masterSsh: cfg.masterSsh, trackedCount: verify.trackedCount,
  });
  if (!pub.merged || !post.verified) {
    const recover = await ctx.breakpoint({
      question:
        'Post-merge verification found problems.\n' +
        'Merged: ' + pub.merged + '\n' +
        'Live prune: ' + post.livePrune + '; app: ' + post.appSync + '/' + post.appHealth + '\n' +
        'Orphan survives: ' + post.orphanSurvives + '\n' +
        'Anomalies: ' + JSON.stringify(post.anomalies) + '\n' +
        'Summary: ' + post.summary + '\n\nHow to proceed?',
      options: ['Re-verify (transient)', 'Continue to closeout (accept state)', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (recover.response || '').toLowerCase();
    if (recover.approved && r.includes('re-verify')) {
      post = await ctx.task(postMergeVerifyTask, {
        appName: cfg.appName, ns: cfg.ns, masterSsh: cfg.masterSsh, trackedCount: verify.trackedCount, attempt: 2,
      });
    } else if (!recover.approved || r.includes('stop')) {
      return { success: false, merged: pub.merged, prUrl: pub.prUrl, reason: 'verification-stop', post };
    }
  }

  // PHASE 5 — closeout: close #51, tick PR test plan, open orphan drift follow-up.
  const close = await ctx.task(closeoutTask, {
    repoRoot: cfg.repoRoot, issue: cfg.issue, repo: cfg.repo, ns: cfg.ns, prUrl: pub.prUrl,
  });

  ctx.log('info', `Closeout: #51=${close.issue51State}; follow-up=${close.followUpIssueUrl}`);

  return {
    success: true,
    safeToFlip: verify.safeToFlip,
    merged: pub.merged,
    prUrl: pub.prUrl,
    livePrune: post.livePrune,
    issue51State: close.issue51State,
    followUpIssue: close.followUpIssueUrl,
  };
}
