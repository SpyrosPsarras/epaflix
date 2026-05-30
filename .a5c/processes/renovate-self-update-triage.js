/**
 * @process specializations/devops-sre-platform/renovate-self-update-triage
 * @description Triage the Renovate self-update branch `renovate/ghcr.io-renovatebot-renovate-43.x`
 *   (bumps the self-hosted Renovate CronJob image from the deployed 43.33.2 to 43.205.1 — an
 *   in-major MINOR bump that the repo's automerge rules do NOT auto-merge). Assess breaking-change
 *   risk over the 43.33→43.205 window + confirm live state (read-only), then an owner DECISION gate
 *   chooses: merge now / leave for Renovate's nightly PR / close-ignore the branch. Execute the
 *   chosen action, verify the renovate app (selfHeal:true) reconciles if merged, then close out.
 * @inputs { repoRoot, appName, ns, masterSsh, branch, fromTag, toTag, kustomization, repo }
 * @outputs { success, decision, prUrl, livePrune, liveImage, deployed, summary }
 *
 * Low risk: the change only swaps the bot's own image tag (kustomize images: newTag). The bot owns
 * nothing in-cluster beyond its own job pods. The only real consideration is config-migration
 * breaking changes between 43.33.2 and 43.205.1. A decision breakpoint gates any merge.
 *
 * @agent general-purpose (changelog research + kubectl-over-ssh / git / gh executor)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';

// Phase 1 — assess the self-update branch + breaking-change risk (NO mutation).
const assessTask = defineTask('assess', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Assess renovate self-update branch + 43.33→43.205 breaking-change risk',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Platform/SRE engineer triaging a self-hosted Renovate bot self-upgrade on the Epaflix k3s cluster',
      task:
        'Explain WHY the branch ' + args.branch + ' exists and assess whether merging the ' +
        args.fromTag + ' → ' + args.toTag + ' Renovate self-image bump is safe. DO NOT change anything.',
      context: { ...args },
      instructions: [
        'kubectl access is over SSH: prefix cluster commands with `' + args.masterSsh + ' \'<kubectl ...>\'`. Run git/gh locally from repoRoot=' + args.repoRoot + '.',
        'Confirm live state: `' + args.masterSsh + " 'kubectl -n argocd get application " + args.appName + " -o jsonpath=\"{.status.sync.status},{.status.health.status},{.spec.syncPolicy.automated}\"'` and the deployed image `" + args.masterSsh + " 'kubectl -n " + args.ns + " get cronjob -o jsonpath=\"{.items[*].spec.jobTemplate.spec.template.spec.containers[*].image}\"'` (expect " + args.fromTag + ").",
        'Confirm git state: the branch diff is only ' + args.kustomization + ' images[].newTag ' + args.fromTag + '→' + args.toTag + ' (`git --no-pager diff origin/main...origin/' + args.branch + '`). Confirm no open PR currently (`gh pr list --head ' + args.branch + ' --state open`).',
        'Classify the bump: 43.33.2 → 43.205.1 is same-major (43), MINOR. Per .github/renovate.json the patch-automerge rule does NOT cover minor, and no rule auto-merges renovate-self minors, so this needs manual review/merge or it will sit as a nightly PR.',
        'BREAKING-CHANGE SCAN: research Renovate release notes / changelog between v43.33.2 and v43.205.1 (use web search — e.g. github.com/renovatebot/renovate releases, docs.renovatebot.com). Focus on: config-option removals/renames, default-behavior changes, anything that could break THIS repo\'s usage (self-hosted Docker CronJob, config:recommended + :dependencyDashboard + :semanticCommits, kustomize manager, platformAutomerge). Summarize any item that would require a config change in .github/renovate.json or the cronjob.',
        'Give a recommendation: merge-now / leave-for-nightly / close-ignore, with one-line rationale. Note this is the bot updating ITSELF (it owns nothing beyond its own job pods), so blast radius is limited to future Renovate runs.',
        'Return ONLY the structured JSON result, not a plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['whyCreated', 'liveImage', 'appSync', 'appHealth', 'diffIsTagOnly', 'openPrExists', 'bumpType', 'breakingChanges', 'recommendation', 'rationale', 'summary'],
      properties: {
        whyCreated: { type: 'string' },
        liveImage: { type: 'string' },
        appSync: { type: 'string' },
        appHealth: { type: 'string' },
        diffIsTagOnly: { type: 'boolean' },
        openPrExists: { type: 'boolean' },
        bumpType: { type: 'string' },
        breakingChanges: { type: 'array', items: { type: 'string' } },
        recommendation: { type: 'string' },
        rationale: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 2a — merge the bump (open PR from branch + merge per policy).
const mergeTask = defineTask('merge-bump', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Open PR from renovate branch + merge per policy',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer publishing an OWNER-APPROVED Renovate self-bump to SpyrosPsarras/epaflix',
      task:
        'The owner approved merging ' + args.fromTag + '→' + args.toTag + '. Open a PR from the existing ' +
        'branch ' + args.branch + ' (if none is open) and merge it per the Epaflix policy (merge-commit only, ' +
        'PR required, 0 approvals, admin bypass authorized). Merge propagates via app-of-apps to the renovate app (selfHeal:true).',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + ', repo=' + args.repo + '.',
        'If an open PR already exists for ' + args.branch + ', use it; else open one: `gh pr create --base main --head ' + args.branch + ' --title "[renovate] Update ghcr.io/renovatebot/renovate Docker tag to v43.205.1" --body "<short body noting owner-approved self-bump ' + args.fromTag + '→' + args.toTag + ', deployed via app-of-apps to renovate app (selfHeal)>"`. Add label renovate if it exists.',
        'Merge: `gh pr merge ' + args.branch + ' --admin --merge` (merge commit, NOT squash/rebase).',
        'Capture PR URL + merge SHA; confirm MERGED state before returning.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'merged', 'mergeSha'],
      properties: { prUrl: { type: 'string' }, merged: { type: 'boolean' }, mergeSha: { type: 'string' } },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 2b — verify the renovate app reconciled the new image (post-merge).
const verifyTask = defineTask('verify-deploy', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify renovate app reconciled the new image via app-of-apps',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'ArgoCD SRE verifying a post-merge reconcile of the self-hosted Renovate CronJob',
      task: 'Confirm the merged ' + args.toTag + ' image reached the live renovate CronJob via app-of-apps and the app is Synced+Healthy.',
      context: { ...args },
      instructions: [
        'kubectl over SSH: prefix with `' + args.masterSsh + ' \'<kubectl ...>\'`.',
        'Allow app-of-apps to reconcile (selfHeal automated); if stale, you may annotate app-of-apps with argocd.argoproj.io/refresh=normal and poll a few rounds (do NOT manual-sync with prune).',
        'Confirm live cronjob image == ' + args.toTag + ': `' + args.masterSsh + " 'kubectl -n " + args.ns + " get cronjob -o jsonpath=\"{.items[*].spec.jobTemplate.spec.template.spec.containers[*].image}\"'`.",
        'Confirm renovate app sync=Synced, health=Healthy. (CronJob health may show Progressing/Suspended-style states; note if so — that is not a failure for a scheduled job.)',
        'Set deployed=true only if the live image == ' + args.toTag + ' and the app is not Degraded.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['deployed', 'liveImage', 'appSync', 'appHealth', 'summary'],
      properties: {
        deployed: { type: 'boolean' },
        liveImage: { type: 'string' },
        appSync: { type: 'string' },
        appHealth: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 2c — close/ignore the branch (owner chose not to take the bump).
const closeBranchTask = defineTask('close-branch', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Close/ignore the renovate self-update branch per owner choice',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer actioning an owner decision to NOT take a Renovate self-bump',
      task: 'The owner chose to NOT take ' + args.fromTag + '→' + args.toTag + ' now. Apply the owner-specified disposition.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + ', repo=' + args.repo + '.',
        'Per the owner choice in context (disposition): if "close-pr-only", close any open PR for ' + args.branch + ' with a comment (Renovate will recreate per schedule). If "ignore-permanently", advise adding an ignore/pin rule to .github/renovate.json instead (do NOT edit config unless the owner explicitly asked) — for this run just close the open PR (if any) and report that a permanent ignore needs a renovate.json rule.',
        'Do NOT delete the branch (Renovate manages it). Do NOT merge.',
        'Return ONLY the structured JSON result describing what was done.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['action', 'detail'],
      properties: { action: { type: 'string' }, detail: { type: 'string' } },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

export async function process(inputs, ctx) {
  const cfg = {
    repoRoot: '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
    appName: 'renovate',
    ns: 'renovate',
    masterSsh: 'ssh ubuntu@192.168.10.51',
    branch: 'renovate/ghcr.io-renovatebot-renovate-43.x',
    fromTag: '43.33.2',
    toTag: '43.205.1',
    kustomization: '2-k3s/12.renovate/kustomization.yaml',
    repo: 'SpyrosPsarras/epaflix',
    ...inputs,
  };

  ctx.log('info', 'renovate self-update triage — assess → decide → (merge|leave|close) → verify');

  // PHASE 1 — assess (read-only).
  const a = await ctx.task(assessTask, {
    repoRoot: cfg.repoRoot, appName: cfg.appName, ns: cfg.ns, masterSsh: cfg.masterSsh,
    branch: cfg.branch, fromTag: cfg.fromTag, toTag: cfg.toTag, kustomization: cfg.kustomization, repo: cfg.repo,
  });
  ctx.log('info', `Assess: live=${a.liveImage}; bump=${a.bumpType}; breaking=${(a.breakingChanges || []).length}; rec=${a.recommendation}`);

  // GATE — owner decision on disposition.
  const gate = await ctx.breakpoint({
    question:
      'Renovate self-update branch triage — what should I do?\n\n' +
      'Why it exists: ' + a.whyCreated + '\n' +
      'Live: ' + a.liveImage + ' (' + a.appSync + '/' + a.appHealth + ')\n' +
      'Proposed: ' + cfg.fromTag + ' → ' + cfg.toTag + ' (' + a.bumpType + '; tag-only diff: ' + a.diffIsTagOnly + '; open PR: ' + a.openPrExists + ')\n' +
      'Breaking changes found (' + (a.breakingChanges || []).length + '): ' + JSON.stringify(a.breakingChanges) + '\n\n' +
      'Recommendation: ' + a.recommendation + ' — ' + a.rationale + '\n\n' +
      'Summary: ' + a.summary + '\n\n' +
      'Choose:',
    options: ['Merge now', 'Leave for Renovate nightly PR', 'Close/ignore the branch'],
    expert: 'owner',
    tags: ['deploy', 'decision-gate'],
  });

  const resp = (gate.response || '').toLowerCase();

  // Owner declined / dismissed → safe default: leave it (no mutation).
  if (!gate.approved && !resp) {
    ctx.log('warn', 'No decision provided — leaving branch untouched (no mutation).');
    return { success: true, decision: 'leave', deployed: false, liveImage: a.liveImage, summary: 'No decision; branch left for Renovate nightly PR. ' + a.summary };
  }

  // LEAVE — no action.
  if (resp.includes('leave')) {
    ctx.log('info', 'Decision: leave for Renovate nightly PR — no action taken.');
    return { success: true, decision: 'leave', deployed: false, liveImage: a.liveImage, summary: 'Left for Renovate nightly PR. ' + a.summary };
  }

  // CLOSE/IGNORE.
  if (resp.includes('close') || resp.includes('ignore')) {
    const c = await ctx.task(closeBranchTask, {
      repoRoot: cfg.repoRoot, repo: cfg.repo, branch: cfg.branch, fromTag: cfg.fromTag, toTag: cfg.toTag,
      disposition: gate.feedback || gate.response || 'close-pr-only',
    });
    ctx.log('info', `Decision: close/ignore — ${c.action}`);
    return { success: true, decision: 'close', deployed: false, liveImage: a.liveImage, action: c.action, detail: c.detail };
  }

  // MERGE NOW (default for "merge").
  const m = await ctx.task(mergeTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo, branch: cfg.branch, fromTag: cfg.fromTag, toTag: cfg.toTag,
  });
  ctx.log('info', `Merged: ${m.merged}; PR=${m.prUrl}`);

  let v = await ctx.task(verifyTask, {
    appName: cfg.appName, ns: cfg.ns, masterSsh: cfg.masterSsh, toTag: cfg.toTag,
  });
  if (!m.merged || !v.deployed) {
    const recover = await ctx.breakpoint({
      question:
        'Post-merge verification incomplete.\nMerged: ' + m.merged + '\nLive image: ' + v.liveImage +
        '\nApp: ' + v.appSync + '/' + v.appHealth + '\nSummary: ' + v.summary + '\n\nHow to proceed?',
      options: ['Re-verify (transient)', 'Accept state', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (recover.response || '').toLowerCase();
    if (recover.approved && r.includes('re-verify')) {
      v = await ctx.task(verifyTask, { appName: cfg.appName, ns: cfg.ns, masterSsh: cfg.masterSsh, toTag: cfg.toTag, attempt: 2 });
    } else if (!recover.approved || r.includes('stop')) {
      return { success: false, decision: 'merge', merged: m.merged, prUrl: m.prUrl, reason: 'verification-stop', verify: v };
    }
  }

  return {
    success: true,
    decision: 'merge',
    prUrl: m.prUrl,
    deployed: v.deployed,
    liveImage: v.liveImage,
    appSync: v.appSync,
    appHealth: v.appHealth,
    summary: v.summary,
  };
}
