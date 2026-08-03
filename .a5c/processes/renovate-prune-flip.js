/**
 * @process specializations/devops-sre-platform/renovate-prune-flip
 * @description Deliver Epaflix issue #54 (last of the 5 split prune-flip issues, after
 *   #49/#50/#51/#52/#53): flip the `renovate` ArgoCD Application from prune:false to
 *   prune:true after its (long-overdue, soak target 2026-05-27) 48h clean soak. The
 *   renovate stack is tiny and tracks ONLY {Namespace renovate, ServiceAccount renovate,
 *   CronJob renovate} (kustomization lists namespace.yaml + cronjob.yaml). Two
 *   renovate-specific prune-safety concerns drive the pre-flip verify:
 *     1. `renovate-secrets` (Opaque, holds the fine-grained GitHub PAT) is applied
 *        IMPERATIVELY from secrets.yml and is DELIBERATELY kept OUT of git / out of the
 *        kustomization (secret-app.yaml is not a kustomization resource). It must therefore
 *        be an UNTRACKED orphan that prune IGNORES — verify proves it is untracked and
 *        will survive the flip. (Mirror image of filebrowser-oidc #52: there the Secret was
 *        moved INTO git to make prune safe; here it must stay out and stay untracked.)
 *     2. CronJob transient state — the renovate CronJob spawns Jobs which spawn Pods. These
 *        are owner-referenced (not in git, not ArgoCD-tracked), so prune does not touch them.
 *        Verify confirms no transient Job/Pod is independently tracked.
 *   Then deploy breakpoint, branch+commit, push+PR+merge (app-of-apps #48 is CLOSED so the
 *   merged Application spec reconciles via app-of-apps selfHeal — no manual kubectl apply),
 *   then post-merge verify (prune live, renovate-secrets survives, CronJob intact, app
 *   Synced+Healthy) + closeout (close #54, tick PR test plan, open any drift follow-up).
 * @inputs { repoRoot, appName, appManifest, appPath, ns, patSecret, masterSsh, issue, repo, branch }
 * @outputs { success, safeToFlip, merged, prUrl, livePrune, issue54State, followUpIssue }
 *
 * PAT-Secret risk: the flip enables prune on a selfHeal app. The danger scenario is a
 * resource that ArgoCD TRACKS (in .status.resources[]) but is absent from the rendered git
 * set — prune would delete it. For renovate the only out-of-git resource is
 * `renovate-secrets`; if it were somehow tracked it would be at risk. Gated by a mandatory
 * deploy breakpoint after a pre-flip verify that proves renovate-secrets is UNTRACKED
 * (prune-ignored) and zero tracked resources would be pruned.
 *
 * Local render caveat: the orchestrator host has no cluster context; kubectl access is over
 * SSH to a master. The verify relies on ArgoCD's own live diff/sync status (Synced ⇒
 * desired==live for every tracked resource ⇒ nothing to prune) rather than a local render.
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
  title: 'Verify prune:true is safe for the renovate app (renovate-secrets untracked, no tracked orphans, no transient Job/Pod tracked)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Kubernetes/ArgoCD SRE working on the Epaflix k3s cluster',
      task:
        'Prove that flipping the `' + args.appName + '` ArgoCD Application to prune:true is SAFE — i.e. ' +
        'no resource that ArgoCD TRACKS for this app is missing from git (which prune would delete), ' +
        'with SPECIAL attention to the imperatively-applied PAT Secret `' + args.patSecret + '` which is ' +
        'deliberately kept OUT of git and MUST be an untracked orphan that prune ignores, and to any ' +
        'transient CronJob Job/Pod state. DO NOT change anything.',
      context: { ...args },
      instructions: [
        'kubectl access is over SSH: prefix every cluster command with `' + args.masterSsh + ' \'<kubectl ...>\'`. Run git commands locally from repoRoot=' + args.repoRoot + '.',
        'Confirm current app state: `' + args.masterSsh + " 'kubectl -n argocd get application " + args.appName + " -o json'` — record sync.status (expect Synced), health.status (expect Healthy), and spec.syncPolicy.automated (expect selfHeal:true, prune:false).",
        'Build the TRACKED resource list from `.status.resources[]` of that Application JSON (kind/name/namespace/status). The renovate kustomization (' + args.appPath + '/kustomization.yaml) renders ONLY namespace.yaml + cronjob.yaml, so the expected tracked set is exactly {Namespace renovate, ServiceAccount renovate, CronJob renovate}. Note each entry\'s sync status; every entry should be status=Synced.',
        'PRUNE-SAFETY CHECK: identify any tracked resource whose ArgoCD sync state indicates it exists live but is NO LONGER in the desired git manifest (the "would-be-pruned" items). For a Synced app this list must be empty. If the app is NOT fully Synced, enumerate exactly which resources are OutOfSync and why before declaring safe/unsafe. (Expected result: NONE would be pruned.)',
        'PAT-SECRET CHECK (the critical one): `Secret/' + args.patSecret + '` in namespace ' + args.ns + ' holds the fine-grained GitHub PAT and is applied imperatively from secrets.yml (see ' + args.appPath + '/secret-app.yaml — NOT a kustomization resource). Confirm it (a) EXISTS live (`' + args.masterSsh + " 'kubectl -n " + args.ns + ' get secret ' + args.patSecret + "'`) and (b) is NOT in `.status.resources[]` i.e. NOT ArgoCD-tracked, and (c) carries no app.kubernetes.io/instance or argocd tracking-id label pointing at the renovate app (`" + args.masterSsh + " 'kubectl -n " + args.ns + ' get secret ' + args.patSecret + " -o jsonpath=\"{.metadata.labels}{\\\"\\\\n\\\"}{.metadata.annotations}\"'`). If it is UNTRACKED, prune IGNORES it and it is SAFE. If it were tracked-but-not-in-git, prune would DELETE it — that would make safeToFlip=false.",
        'TRANSIENT-STATE CHECK: list renovate Jobs/Pods (`' + args.masterSsh + " 'kubectl -n " + args.ns + " get jobs,pods -o json'`). Confirm each is owner-referenced (ownerReferences to the CronJob/Job) and NOT independently present in `.status.resources[]`. Owner-referenced Jobs/Pods are not ArgoCD-tracked and prune does not delete them — confirm none would be pruned.",
        'ORPHAN CHECK: enumerate other resources in namespace ' + args.ns + ' (`' + args.masterSsh + " 'kubectl -n " + args.ns + " get all,secret,configmap,pvc,ingress -o json'`) that are NOT ArgoCD-tracked by this app. Untracked resources are IGNORED by prune (safe) but report them (renovate-secrets is the expected one).",
        'Set safeToFlip=true ONLY if: app is Synced+Healthy AND the would-be-pruned list is empty AND `' + args.patSecret + '` exists and is UNTRACKED AND no transient Job/Pod is independently tracked. Untracked orphans do NOT block safety (prune ignores them) but MUST be reported.',
        'Return ONLY the structured JSON result, not a plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['safeToFlip', 'appSync', 'appHealth', 'currentPrune', 'trackedCount', 'trackedResources', 'patSecretExists', 'patSecretTracked', 'wouldBePruned', 'transientTracked', 'untrackedOrphans', 'summary'],
      properties: {
        safeToFlip: { type: 'boolean' },
        appSync: { type: 'string' },
        appHealth: { type: 'string' },
        currentPrune: { type: 'boolean' },
        trackedCount: { type: 'number' },
        trackedResources: { type: 'array', items: { type: 'string' } },
        patSecretExists: { type: 'boolean' },
        patSecretTracked: { type: 'boolean' },
        wouldBePruned: { type: 'array', items: { type: 'string' } },
        transientTracked: { type: 'array', items: { type: 'string' } },
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
  title: 'Flip prune:true in app-renovate.yaml + local commit on branch',
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
        'Edit ' + args.appManifest + ': change `prune: false` to `prune: true` under spec.syncPolicy.automated. Update the header `# Sync:` comment that currently says prune stays OFF — state prune is now enabled after the 48h soak (issue #' + args.issue + '), keeping the selfHeal note and the "no argocd-image-updater here" rationale intact.',
        'Do NOT change any other field (selfHeal stays true, source/destination/syncOptions untouched). The renovate app has no ignoreDifferences block — do not add one.',
        'If feedback is present in context (a prior breakpoint rejection), incorporate it.',
        'Create branch ' + args.branch + ' off origin/main (fetch first; reuse if it already exists). Stage ONLY ' + args.appManifest + '. Make ONE commit referencing #' + args.issue + ' (suggested subject: `chore(argocd): flip renovate Application prune to true (#' + args.issue + ')`). End the commit message body with the Co-Authored-By trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.',
        'Return ONLY the structured JSON result: branch, changedFiles, commitSha, the exact prune diff (before/after lines), plus the proposed PR title and body. The PR body MUST include a Test Plan section with checkbox items for the post-merge verification (app-of-apps reconciles the spec, renovate shows prune:true live, app stays Synced+Healthy, no unexpected pruning, CronJob/renovate intact, `' + args.patSecret + '` PAT Secret still present).',
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
  title: 'Push branch, open PR, rebase + merge per Epaflix policy (triggers app-of-apps reconcile)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer publishing an OWNER-APPROVED change to SpyrosPsarras/epaflix',
      task:
        'Push the branch, open a PR, and merge it per the Epaflix policy (merge-commit + mandatory ' +
        'rebase / semi-linear, PR required, 0 approvals). Merging is the deploy step — app-of-apps ' +
        'selfHeal then reconciles the new renovate Application spec.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Rebase branch ' + args.branch + ' onto origin/main and `git push --force-with-lease` (the strict up-to-date + required `validate` check block stale branches — see feedback_epaflix_merge_policy).',
        'Open a PR to main with the approved title/body (in context as approvedPrTitle / approvedPrBody). Cross-link issue #' + args.issue + '.',
        'Wait for the required `validate` check to pass, then merge with `gh pr merge --merge` (merge commit — never squash/rebase-merge).',
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

// Phase 4 — post-merge verification (app-of-apps reconciled, prune live, PAT Secret survives).
const postMergeVerifyTask = defineTask('post-merge-verify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify app-of-apps reconciled prune:true live + renovate-secrets survives + CronJob intact + no unexpected pruning',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'ArgoCD SRE verifying a post-merge GitOps reconcile',
      task:
        'Confirm the merged prune:true reached the live `' + args.appName + '` Application via app-of-apps, ' +
        'the app is still Synced+Healthy, the imperative PAT Secret survived, the CronJob is intact, and ' +
        'nothing was unexpectedly pruned.',
      context: { ...args },
      instructions: [
        'kubectl access is over SSH: prefix cluster commands with `' + args.masterSsh + ' \'<kubectl ...>\'`.',
        'Allow app-of-apps to reconcile; you may wait/poll a short while (app-of-apps selfHeal is automated). Optionally trigger a refresh by annotating the Application, but do NOT force-sync with prune flags manually.',
        'Confirm live spec: `' + args.masterSsh + " 'kubectl -n argocd get application " + args.appName + " -o jsonpath=\"{.spec.syncPolicy.automated.prune}\"'` returns true.",
        'Confirm app health: sync.status == Synced and health.status == Healthy.',
        'CRONJOB SURVIVAL: confirm `CronJob/renovate` and `ServiceAccount/renovate` in ' + args.ns + ' still exist (`' + args.masterSsh + " 'kubectl -n " + args.ns + " get cronjob,sa'`).",
        'PAT-SECRET SURVIVAL (critical): confirm `Secret/' + args.patSecret + '` in ' + args.ns + ' STILL EXISTS after the prune-enabled sync (`' + args.masterSsh + " 'kubectl -n " + args.ns + ' get secret ' + args.patSecret + "'`). Being untracked, prune must leave it alone — it MUST survive.",
        'COLLATERAL CHECK: confirm the tracked resource count is unchanged vs the pre-flip trackedCount (' + args.trackedCount + ') in context (no tracked resource got pruned).',
        'Set verified=true ONLY if: livePrune==true AND app Synced+Healthy AND CronJob+SA survive AND the PAT Secret survives AND no tracked resource was pruned. List any anomaly.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['verified', 'livePrune', 'appSync', 'appHealth', 'cronjobSurvives', 'patSecretSurvives', 'anomalies', 'summary'],
      properties: {
        verified: { type: 'boolean' },
        livePrune: { type: 'boolean' },
        appSync: { type: 'string' },
        appHealth: { type: 'string' },
        cronjobSurvives: { type: 'boolean' },
        patSecretSurvives: { type: 'boolean' },
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

// Phase 5 — closeout: close #54 w/ outcome, tick PR test plan, open any surfaced follow-up.
const closeoutTask = defineTask('closeout', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Close #54 with outcome, update PR test plan, open any drift follow-up',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer reconciling issues/PR after a verified GitOps change in SpyrosPsarras/epaflix',
      task:
        'Record the verified outcome: close issue #' + args.issue + ', tick the PR test-plan checkboxes by ' +
        'EDITING the PR body (never a new comment), and open a follow-up issue ONLY if the verify surfaced ' +
        'an untracked orphan or other drift worth tracking.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Repo is ' + args.repo + '.',
        'Comment on issue #' + args.issue + ' summarizing the verified result (prune:true live via app-of-apps, app Synced+Healthy, zero tracked resources pruned, CronJob intact, `' + args.patSecret + '` PAT Secret survived) and CLOSE it. Note this completes the 5-issue prune-flip split from #21 (#49/#50/#51/#52/#53/#54).',
        'Edit the PR body (gh pr edit --body) to check off the Test Plan items that passed, recording the observed evidence inline. Do NOT add a separate comment for the test plan (see feedback_pr_test_plans).',
        'Follow-up policy (CLAUDE.md): the `' + args.patSecret + '` imperative Secret is EXPECTED and already documented — do NOT open a noise issue for it. Open ONE new gh issue ONLY if the verify surfaced a DIFFERENT untracked orphan (a resource in ' + args.ns + ' that should be codified into git or removed), using the repo enhancement-issue shape (## Finding / ## Current state / ## Desired outcome / ## Notes), cross-linking #' + args.issue + '. Otherwise return followUpIssueUrl as empty string.',
        'Return ONLY the structured JSON result with issueState, prUpdated, followUpIssueUrl.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['issueState', 'prUpdated', 'followUpIssueUrl'],
      properties: {
        issueState: { type: 'string' },
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
    appName: 'renovate',
    appManifest: '2-k3s/11.argocd/apps/app-renovate.yaml',
    appPath: '2-k3s/12.renovate',
    ns: 'renovate',
    patSecret: 'renovate-secrets',
    masterSsh: 'ssh ubuntu@192.168.10.51',
    issue: '54',
    repo: 'SpyrosPsarras/epaflix',
    branch: 'renovate-prune-flip',
    ...inputs,
  };

  ctx.log('info', 'renovate prune flip (#54, last of the 5 split) — verify-safe (renovate-secrets untracked, CronJob transient) → deploy gate → rebase+merge → verify → closeout');

  // PHASE 1 — pre-flip safety verify (no mutation).
  const verify = await ctx.task(preflightVerifyTask, {
    repoRoot: cfg.repoRoot, appName: cfg.appName, appPath: cfg.appPath, ns: cfg.ns, patSecret: cfg.patSecret, masterSsh: cfg.masterSsh,
  });
  ctx.log('info', `Pre-flip: safe=${verify.safeToFlip}; sync=${verify.appSync}/${verify.appHealth}; patExists=${verify.patSecretExists}/tracked=${verify.patSecretTracked}; wouldBePruned=${(verify.wouldBePruned || []).length}; transientTracked=${(verify.transientTracked || []).length}; orphans=${(verify.untrackedOrphans || []).length}`);

  // GATE 1 (deploy) — mandatory: approve the flip + merge. Merging deploys via app-of-apps.
  const gate = await ctx.breakpoint({
    question:
      'Approve flipping `renovate` ArgoCD Application to prune:true and MERGING it (#' + cfg.issue + ', last of the 5 split prune-flips)?\n\n' +
      'Pre-flip safety: ' + (verify.safeToFlip ? 'SAFE ✅' : 'NOT SAFE ⚠️') + '\n' +
      'App state: ' + verify.appSync + ' / ' + verify.appHealth + ' (prune currently ' + verify.currentPrune + ')\n' +
      'Tracked resources (' + verify.trackedCount + '): ' + JSON.stringify(verify.trackedResources) + '\n' +
      'PAT Secret `' + cfg.patSecret + '`: exists=' + verify.patSecretExists + ', argocd-tracked=' + verify.patSecretTracked + ' (imperative PAT, must be UNTRACKED so prune ignores it)\n' +
      'Would-be-pruned (tracked-but-not-in-git): ' + JSON.stringify(verify.wouldBePruned) + '\n' +
      'Transient Job/Pod tracked (should be none): ' + JSON.stringify(verify.transientTracked) + '\n' +
      'Untracked orphans (prune ignores these): ' + JSON.stringify(verify.untrackedOrphans) + '\n\n' +
      'Summary: ' + verify.summary + '\n\n' +
      'Merging triggers app-of-apps selfHeal to apply prune:true live. Proceed?',
    options: ['Approve flip + merge', 'Abort'],
    expert: 'owner',
    tags: ['deploy', 'approval-gate'],
  });
  if (!gate.approved) {
    ctx.log('warn', 'Flip not approved — aborting before any mutation.');
    return { success: false, safeToFlip: verify.safeToFlip, merged: false, reason: 'not-approved', feedback: gate.response || gate.feedback || '', verify };
  }

  // PHASE 2 — author the flip on a branch + local commit (reversible).
  const change = await ctx.task(prepareChangeTask, {
    repoRoot: cfg.repoRoot, appManifest: cfg.appManifest, branch: cfg.branch, issue: cfg.issue, patSecret: cfg.patSecret,
  });
  ctx.log('info', `Prepared: branch=${change.branch} commit=${change.commitSha}`);

  // PHASE 3 — rebase + push + PR + merge (the deploy).
  const pub = await ctx.task(publishMergeTask, {
    repoRoot: cfg.repoRoot, branch: change.branch, issue: cfg.issue, repo: cfg.repo,
    approvedPrTitle: change.prTitle, approvedPrBody: change.prBody,
  });
  ctx.log('info', `Merged: ${pub.merged}; PR=${pub.prUrl}; sha=${pub.mergeSha}`);

  // PHASE 4 — post-merge verify, with an owner gate on anomaly.
  let post = await ctx.task(postMergeVerifyTask, {
    appName: cfg.appName, ns: cfg.ns, patSecret: cfg.patSecret, masterSsh: cfg.masterSsh, trackedCount: verify.trackedCount,
  });
  if (!pub.merged || !post.verified) {
    const recover = await ctx.breakpoint({
      question:
        'Post-merge verification found problems.\n' +
        'Merged: ' + pub.merged + '\n' +
        'Live prune: ' + post.livePrune + '; app: ' + post.appSync + '/' + post.appHealth + '\n' +
        'CronJob survives: ' + post.cronjobSurvives + '; PAT Secret survives: ' + post.patSecretSurvives + '\n' +
        'Anomalies: ' + JSON.stringify(post.anomalies) + '\n' +
        'Summary: ' + post.summary + '\n\nHow to proceed?',
      options: ['Re-verify (transient)', 'Continue to closeout (accept state)', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (recover.response || '').toLowerCase();
    if (recover.approved && r.includes('re-verify')) {
      post = await ctx.task(postMergeVerifyTask, {
        appName: cfg.appName, ns: cfg.ns, patSecret: cfg.patSecret, masterSsh: cfg.masterSsh, trackedCount: verify.trackedCount, attempt: 2,
      });
    } else if (!recover.approved || r.includes('stop')) {
      return { success: false, merged: pub.merged, prUrl: pub.prUrl, reason: 'verification-stop', post };
    }
  }

  // PHASE 5 — closeout: close #54, tick PR test plan, open any drift follow-up.
  const close = await ctx.task(closeoutTask, {
    repoRoot: cfg.repoRoot, issue: cfg.issue, repo: cfg.repo, ns: cfg.ns, patSecret: cfg.patSecret, prUrl: pub.prUrl,
    untrackedOrphans: verify.untrackedOrphans,
  });

  ctx.log('info', `Closeout: #54=${close.issueState}; follow-up=${close.followUpIssueUrl}`);

  return {
    success: true,
    safeToFlip: verify.safeToFlip,
    merged: pub.merged,
    prUrl: pub.prUrl,
    livePrune: post.livePrune,
    issue54State: close.issueState,
    followUpIssue: close.followUpIssueUrl,
  };
}
