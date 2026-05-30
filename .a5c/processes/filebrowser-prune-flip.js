/**
 * @process specializations/devops-sre-platform/filebrowser-prune-flip
 * @description Deliver Epaflix issue #52: flip the `filebrowser` ArgoCD Application from
 *   prune:false to prune:true after its (overdue) 48h clean soak. Pre-flip safety verify is
 *   centred on the OIDC Secret: at adoption time `filebrowser-oidc` was imperative + excluded
 *   (so prune would have deleted it — see feedback_argocd_adoption_order). Since 2026-05-25
 *   (Issue #29) the Secret is reconciled FROM git via the kustomization's ksops generator, so
 *   it is now TRACKED + in the desired set — prune is safe ONLY if the verify proves the
 *   Secret renders from git (tracked + Synced) and no tracked resource is missing from git.
 *   Then deploy breakpoint, branch+commit, push+PR+merge (app-of-apps #48 is CLOSED so the
 *   merged Application spec reconciles via app-of-apps selfHeal — no manual kubectl apply),
 *   then post-merge verify (prune live, OIDC Secret survives, app Synced+Healthy) + closeout.
 * @inputs { repoRoot, appName, appManifest, appPath, ns, oidcSecret, masterSsh, issue, repo, branch }
 * @outputs { success, safeToFlip, merged, prUrl, livePrune, issue52State, followUpIssue }
 *
 * OIDC-Secret risk: the flip enables prune on a selfHeal app. If `filebrowser-oidc` were
 * tracked but absent from the rendered git set, prune would DELETE it (exactly the 2026-05-24
 * adoption regression). Gated by a mandatory deploy breakpoint after a pre-flip verify that
 * proves the Secret is sourced from git and zero tracked resources would be pruned.
 *
 * Local render caveat: no age key / ksops exec plugin on the orchestrator host, so a local
 * `kustomize build` of 2-k3s/09.filebrowser CANNOT decrypt the ksops generator. The verify
 * therefore relies on ArgoCD's own live diff/sync status (Synced ⇒ desired==live for every
 * tracked resource ⇒ nothing to prune) rather than a local render of the Secret.
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
  title: 'Verify prune:true is safe for the filebrowser app (OIDC Secret git-sourced, no tracked orphans)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Kubernetes/ArgoCD SRE working on the Epaflix k3s cluster',
      task:
        'Prove that flipping the `' + args.appName + '` ArgoCD Application to prune:true is SAFE — i.e. ' +
        'no resource that ArgoCD TRACKS for this app is missing from git (which prune would delete), ' +
        'with SPECIAL attention to the OIDC Secret `' + args.oidcSecret + '` which a prior adoption ' +
        'regression once deleted. DO NOT change anything.',
      context: { ...args },
      instructions: [
        'kubectl access is over SSH: prefix every cluster command with `' + args.masterSsh + ' \'<kubectl ...>\'`. Run git commands locally from repoRoot=' + args.repoRoot + '.',
        'Confirm current app state: `' + args.masterSsh + " 'kubectl -n argocd get application " + args.appName + " -o json'` — record sync.status (expect Synced), health.status (expect Healthy), and spec.syncPolicy.automated (expect selfHeal:true, prune:false).",
        'Build the TRACKED resource list from `.status.resources[]` of that Application JSON (kind/name/namespace/status). Note each entry\'s sync status; every entry should be status=Synced.',
        'IMPORTANT — local render is NOT possible: there is no age key / ksops exec plugin on this host, so `kustomize build ' + args.appPath + '` will FAIL on the ksops generator. Do NOT rely on a local render for the desired set. Instead use ArgoCD\'s own diff: a fully-Synced app means desired(git)==live for every tracked resource, so there is nothing for prune to delete.',
        'PRUNE-SAFETY CHECK: identify any tracked resource whose ArgoCD sync state indicates it exists live but is NO LONGER in the desired git manifest (these are the "would-be-pruned" items). For a Synced app this list must be empty. If the app is NOT fully Synced, enumerate exactly which resources are OutOfSync and why before declaring safe/unsafe. (Expected result: NONE would be pruned.)',
        'OIDC-SECRET CHECK (the critical one): confirm `Secret/' + args.oidcSecret + '` in namespace ' + args.ns + ' is (a) present in `.status.resources[]` i.e. TRACKED by this app, and (b) Synced — meaning ArgoCD renders it from git via the kustomization ksops generator (see filebrowser-oidc.enc.yaml + ksops-generator.yaml in ' + args.appPath + '). Cross-check git: confirm ' + args.appPath + '/ksops-generator.yaml and ' + args.appPath + '/filebrowser-oidc.enc.yaml exist and that kustomization.yaml lists the generator. If the Secret is tracked AND git-sourced AND Synced, prune is SAFE for it; if it were tracked but absent from git, prune would DELETE it — that would make safeToFlip=false.',
        'ORPHAN CHECK: enumerate other resources in namespace ' + args.ns + ' (kubectl get all,secret,configmap,pvc,ingress -o json over the SSH prefix) that are NOT ArgoCD-tracked by this app (no app.kubernetes.io/instance or argocd tracking-id naming ' + args.appName + '). Untracked resources are IGNORED by prune (safe) but report them.',
        'Set safeToFlip=true ONLY if: app is Synced+Healthy AND the would-be-pruned list is empty AND `' + args.oidcSecret + '` is tracked+git-sourced+Synced. Untracked orphans do NOT block safety (prune ignores them) but MUST be reported.',
        'Return ONLY the structured JSON result, not a plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['safeToFlip', 'appSync', 'appHealth', 'currentPrune', 'trackedCount', 'oidcSecretTracked', 'oidcSecretGitSourced', 'wouldBePruned', 'untrackedOrphans', 'summary'],
      properties: {
        safeToFlip: { type: 'boolean' },
        appSync: { type: 'string' },
        appHealth: { type: 'string' },
        currentPrune: { type: 'boolean' },
        trackedCount: { type: 'number' },
        oidcSecretTracked: { type: 'boolean' },
        oidcSecretGitSourced: { type: 'boolean' },
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
  title: 'Flip prune:true in app-filebrowser.yaml + local commit on branch',
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
        'Edit ' + args.appManifest + ': change `prune: false` to `prune: true` under spec.syncPolicy.automated. Update the `# Sync:` header comment line that says "Prune stays off." — state prune is now enabled after the 48h soak (issue #' + args.issue + '), and keep the selfHeal / OIDC-Secret-via-ksops notes intact.',
        'Do NOT change any other field (selfHeal stays true, ignoreDifferences untouched, source/destination untouched).',
        'If feedback is present in context (a prior breakpoint rejection), incorporate it.',
        'Create branch ' + args.branch + ' off the current branch (reuse if it exists). Stage ONLY ' + args.appManifest + '. Make ONE commit referencing #' + args.issue + '. End the commit message body with the Co-Authored-By trailer for Claude Opus 4.8 (1M context).',
        'Return ONLY the structured JSON result: branch, changedFiles, commitSha, the exact prune diff (before/after lines), plus the proposed PR title and body. The PR body MUST include a Test Plan section with checkbox items for the post-merge verification (app-of-apps reconciles the spec, filebrowser shows prune:true live, app stays Synced+Healthy, no unexpected pruning, `' + args.oidcSecret + '` OIDC Secret still present).',
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
        'reconciles the new filebrowser Application spec.',
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

// Phase 4 — post-merge verification (app-of-apps reconciled, prune live, OIDC Secret survives).
const postMergeVerifyTask = defineTask('post-merge-verify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify app-of-apps reconciled prune:true live + OIDC Secret survives + no unexpected pruning',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'ArgoCD SRE verifying a post-merge GitOps reconcile',
      task:
        'Confirm the merged prune:true reached the live `' + args.appName + '` Application via app-of-apps, ' +
        'the app is still Synced+Healthy, the OIDC Secret survived, and nothing was unexpectedly pruned.',
      context: { ...args },
      instructions: [
        'kubectl access is over SSH: prefix cluster commands with `' + args.masterSsh + ' \'<kubectl ...>\'`.',
        'Allow app-of-apps to reconcile; inspect: `' + args.masterSsh + " 'kubectl -n argocd get application app-of-apps " + args.appName + " -o json'`. You may wait/poll a short while (app-of-apps selfHeal is automated). Optionally trigger a refresh by annotating, but do NOT force-sync with prune flags manually.",
        'Confirm live spec: `' + args.masterSsh + " 'kubectl -n argocd get application " + args.appName + " -o jsonpath=\"{.spec.syncPolicy.automated.prune}\"'` returns true.",
        'Confirm app health: sync.status == Synced and health.status == Healthy. Confirm the filebrowser Deployment is still Available and pods Ready (`' + args.masterSsh + " 'kubectl -n " + args.ns + " get deploy,pods'`).",
        'OIDC-SECRET SURVIVAL (critical): confirm `Secret/' + args.oidcSecret + '` in ' + args.ns + ' STILL EXISTS after the prune-enabled sync (`' + args.masterSsh + " 'kubectl -n " + args.ns + " get secret " + args.oidcSecret + "'`). This is the resource the adoption regression once deleted — it MUST survive.",
        'COLLATERAL CHECK: confirm the tracked resource count is unchanged vs the pre-flip trackedCount in context (no tracked resource got pruned).',
        'Set verified=true ONLY if: livePrune==true AND app Synced+Healthy AND the OIDC Secret survives AND no tracked resource was pruned. List any anomaly.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['verified', 'livePrune', 'appSync', 'appHealth', 'oidcSecretSurvives', 'anomalies', 'summary'],
      properties: {
        verified: { type: 'boolean' },
        livePrune: { type: 'boolean' },
        appSync: { type: 'string' },
        appHealth: { type: 'string' },
        oidcSecretSurvives: { type: 'boolean' },
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

// Phase 5 — closeout: close #52 w/ outcome, tick PR test plan, open any surfaced follow-up.
const closeoutTask = defineTask('closeout', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Close #52 with outcome, update PR test plan, open any drift follow-up',
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
        'Comment on issue #' + args.issue + ' summarizing the verified result (prune:true live via app-of-apps, app Synced+Healthy, zero tracked resources pruned, `' + args.oidcSecret + '` OIDC Secret survived) and CLOSE it.',
        'Edit the PR body (gh pr edit --body) to check off the Test Plan items that passed, recording the observed evidence inline. Do NOT add a separate comment for the test plan.',
        'Follow-up policy (CLAUDE.md): if the pre-flip untrackedOrphans list in context is non-empty (any untracked resource living in ' + args.ns + ' that should be codified into git or removed), open ONE new gh issue using the repo enhancement-issue shape (## Finding / ## Current state / ## Desired outcome / ## Notes), cross-linking #' + args.issue + '. If there are NO orphans, do NOT open a noise issue — return followUpIssueUrl as empty string.',
        'Return ONLY the structured JSON result with issue52State, prUpdated, followUpIssueUrl.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['issue52State', 'prUpdated', 'followUpIssueUrl'],
      properties: {
        issue52State: { type: 'string' },
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
    appName: 'filebrowser',
    appManifest: '2-k3s/11.argocd/apps/app-filebrowser.yaml',
    appPath: '2-k3s/09.filebrowser',
    ns: 'filebrowser',
    oidcSecret: 'filebrowser-oidc',
    masterSsh: 'ssh ubuntu@192.168.10.51',
    issue: '52',
    repo: 'SpyrosPsarras/epaflix',
    branch: 'filebrowser-prune-flip',
    ...inputs,
  };

  ctx.log('info', 'filebrowser prune flip (#52) — verify-safe (OIDC Secret git-sourced) → deploy gate → merge → verify → closeout');

  // PHASE 1 — pre-flip safety verify (no mutation).
  const verify = await ctx.task(preflightVerifyTask, {
    repoRoot: cfg.repoRoot, appName: cfg.appName, appPath: cfg.appPath, ns: cfg.ns, oidcSecret: cfg.oidcSecret, masterSsh: cfg.masterSsh,
  });
  ctx.log('info', `Pre-flip: safe=${verify.safeToFlip}; sync=${verify.appSync}/${verify.appHealth}; oidcTracked=${verify.oidcSecretTracked}/gitSourced=${verify.oidcSecretGitSourced}; wouldBePruned=${(verify.wouldBePruned || []).length}; orphans=${(verify.untrackedOrphans || []).length}`);

  // GATE 1 (deploy) — mandatory: approve the flip + merge. Merging deploys via app-of-apps.
  const gate = await ctx.breakpoint({
    question:
      'Approve flipping `filebrowser` ArgoCD Application to prune:true and MERGING it (#' + cfg.issue + ')?\n\n' +
      'Pre-flip safety: ' + (verify.safeToFlip ? 'SAFE ✅' : 'NOT SAFE ⚠️') + '\n' +
      'App state: ' + verify.appSync + ' / ' + verify.appHealth + ' (prune currently ' + verify.currentPrune + ')\n' +
      'Tracked resources: ' + verify.trackedCount + '\n' +
      'OIDC Secret `' + cfg.oidcSecret + '`: tracked=' + verify.oidcSecretTracked + ', git-sourced=' + verify.oidcSecretGitSourced + ' (the resource the 2026-05-24 adoption once deleted)\n' +
      'Would-be-pruned (tracked-but-not-in-git): ' + JSON.stringify(verify.wouldBePruned) + '\n' +
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
    repoRoot: cfg.repoRoot, appManifest: cfg.appManifest, branch: cfg.branch, issue: cfg.issue, oidcSecret: cfg.oidcSecret,
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
    appName: cfg.appName, ns: cfg.ns, oidcSecret: cfg.oidcSecret, masterSsh: cfg.masterSsh, trackedCount: verify.trackedCount,
  });
  if (!pub.merged || !post.verified) {
    const recover = await ctx.breakpoint({
      question:
        'Post-merge verification found problems.\n' +
        'Merged: ' + pub.merged + '\n' +
        'Live prune: ' + post.livePrune + '; app: ' + post.appSync + '/' + post.appHealth + '\n' +
        'OIDC Secret survives: ' + post.oidcSecretSurvives + '\n' +
        'Anomalies: ' + JSON.stringify(post.anomalies) + '\n' +
        'Summary: ' + post.summary + '\n\nHow to proceed?',
      options: ['Re-verify (transient)', 'Continue to closeout (accept state)', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (recover.response || '').toLowerCase();
    if (recover.approved && r.includes('re-verify')) {
      post = await ctx.task(postMergeVerifyTask, {
        appName: cfg.appName, ns: cfg.ns, oidcSecret: cfg.oidcSecret, masterSsh: cfg.masterSsh, trackedCount: verify.trackedCount, attempt: 2,
      });
    } else if (!recover.approved || r.includes('stop')) {
      return { success: false, merged: pub.merged, prUrl: pub.prUrl, reason: 'verification-stop', post };
    }
  }

  // PHASE 5 — closeout: close #52, tick PR test plan, open any drift follow-up.
  const close = await ctx.task(closeoutTask, {
    repoRoot: cfg.repoRoot, issue: cfg.issue, repo: cfg.repo, ns: cfg.ns, oidcSecret: cfg.oidcSecret, prUrl: pub.prUrl,
    untrackedOrphans: verify.untrackedOrphans,
  });

  ctx.log('info', `Closeout: #52=${close.issue52State}; follow-up=${close.followUpIssueUrl}`);

  return {
    success: true,
    safeToFlip: verify.safeToFlip,
    merged: pub.merged,
    prUrl: pub.prUrl,
    livePrune: post.livePrune,
    issue52State: close.issue52State,
    followUpIssue: close.followUpIssueUrl,
  };
}
