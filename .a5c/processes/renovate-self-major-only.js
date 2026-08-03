/**
 * @process specializations/devops-sre-platform/renovate-self-major-only
 * @description Reduce Renovate noise for the self-hosted Renovate image
 *   (ghcr.io/renovatebot/renovate): add a scoped packageRule to .github/renovate.json so ONLY
 *   MAJOR updates create a branch + PR (manual review), while MINOR and PATCH updates are
 *   suppressed (enabled:false → no branch, no PR). This makes "a branch on this image" always
 *   mean a major (the branch name carries the new major). Author on a branch + commit
 *   (reversible), validate the config, gate on deploy, then push + PR + merge per policy.
 * @inputs { repoRoot, configPath, imageName, manager, branch, repo, currentBranch }
 * @outputs { success, merged, prUrl, configValid, summary }
 *
 * @agent general-purpose (renovate-config authoring + validation + git/gh executor)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';

// Phase 1 — author the renovate.json rule on a branch + validate + local commit (no push).
const authorTask = defineTask('author-rule', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Add major-only packageRule for the renovate self-image + validate + commit',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Renovate configuration engineer working in the Epaflix repo',
      task:
        'Edit ' + args.configPath + ' to add a scoped packageRule so the self-hosted Renovate image ' +
        args.imageName + ' produces a branch+PR ONLY for MAJOR updates; MINOR and PATCH are suppressed ' +
        '(enabled:false). Then validate the config, create a branch, and make ONE local commit. ' +
        'Do NOT push, do NOT open a PR.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Read ' + args.configPath + ' first to match its existing style/ordering of packageRules.',
        'Append (so it overrides the earlier global patch-automerge rule) a packageRule scoped to this image. Use matchManagers:["' + args.manager + '"] and matchPackageNames:["' + args.imageName + '"]. Suppress noise: matchUpdateTypes ["minor","patch"] → enabled:false. The intent: only MAJOR remains, which by Renovate default opens a PR on a new branch (renovate/<image>-<newMajor>.x) — keep that PR NON-automerged.',
        'Recommended shape (adapt to file style): one rule { description, matchManagers, matchPackageNames, matchUpdateTypes:["minor","patch"], enabled:false } AND one rule { description, matchManagers, matchPackageNames, matchUpdateTypes:["major"], automerge:false } to make the major-review intent explicit and self-documenting. Place BOTH after the existing global patch-automerge rule so they win for this image.',
        'Add a clear `description` on each rule explaining: cut noise — only majors get a branch/PR for this self-hosted bot image; minor/patch suppressed; a branch on this image therefore always signals a major.',
        'Do NOT change any OTHER packageRule (authentik grouping, global patch automerge for the rest of the repo, etc.). Only ADD the two scoped rules.',
        'VALIDATE the result: try `npx --yes --package renovate renovate-config-validator ' + args.configPath + '` (network may be needed). If that is unavailable, at minimum confirm the file is valid JSON (e.g. `jq . ' + args.configPath + '`) and that the schema-required structure is intact. Record which validation ran and its result.',
        'If feedback is present in context (a prior breakpoint rejection), incorporate it.',
        'Create branch ' + args.branch + ' off ' + args.currentBranch + ' (reuse if exists). Stage ONLY ' + args.configPath + '. Make ONE commit. End the commit message body with the trailer: Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>.',
        'Return ONLY the structured JSON result: branch, commitSha, the exact added-rules JSON, the full git diff of ' + args.configPath + ', the validation method+result, and proposed PR title/body. The PR body MUST note: only majors create a branch/PR for ' + args.imageName + '; minor+patch suppressed; the standing renovate/<image>-43.x minor branch will be abandoned by Renovate on its next run; this is a config-only change (no cluster deploy).',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'commitSha', 'addedRules', 'diff', 'validation', 'configValid', 'prTitle', 'prBody'],
      properties: {
        branch: { type: 'string' },
        commitSha: { type: 'string' },
        addedRules: { type: 'string' },
        diff: { type: 'string' },
        validation: { type: 'string' },
        configValid: { type: 'boolean' },
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

// Phase 2 — push + PR + merge per policy.
const publishTask = defineTask('publish-merge', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Push branch, open PR, merge per policy',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer publishing an OWNER-APPROVED renovate.json change to SpyrosPsarras/epaflix',
      task:
        'Push the branch, open a PR, and merge per the Epaflix policy (merge-commit only, PR required, ' +
        '0 approvals, admin bypass authorized).',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + ', repo=' + args.repo + '. Push branch ' + args.branch + ' to origin.',
        'Open a PR to main with the approved title/body (context approvedPrTitle / approvedPrBody).',
        'Merge: `gh pr merge ' + args.branch + ' --admin --merge` (merge commit, NOT squash/rebase).',
        'Capture PR URL + merge SHA; confirm MERGED before returning.',
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

export async function process(inputs, ctx) {
  const cfg = {
    repoRoot: '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
    configPath: '.github/renovate.json',
    imageName: 'ghcr.io/renovatebot/renovate',
    manager: 'kustomize',
    branch: 'renovate-self-major-only',
    currentBranch: 'main',
    repo: 'SpyrosPsarras/epaflix',
    ...inputs,
  };

  ctx.log('info', 'renovate self-image: major-only branch/PR, suppress minor+patch');

  // PHASE 1 — author + validate + local commit (reversible).
  let authored = await ctx.task(authorTask, {
    repoRoot: cfg.repoRoot, configPath: cfg.configPath, imageName: cfg.imageName,
    manager: cfg.manager, branch: cfg.branch, currentBranch: cfg.currentBranch,
  });
  ctx.log('info', `Authored: branch=${authored.branch} valid=${authored.configValid} (${authored.validation})`);

  // GATE (deploy/outward-facing) — approve push + PR + merge, with retry/refine on changes.
  let lastFeedback = null;
  let approved = false;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (lastFeedback) {
      authored = await ctx.task(authorTask, {
        repoRoot: cfg.repoRoot, configPath: cfg.configPath, imageName: cfg.imageName,
        manager: cfg.manager, branch: cfg.branch, currentBranch: cfg.currentBranch,
        feedback: lastFeedback, attempt: attempt + 1,
      });
    }
    const gate = await ctx.breakpoint({
      question:
        'Approve this renovate.json change (push + PR + merge)?\n\n' +
        'Goal: ONLY major updates of ' + cfg.imageName + ' create a branch+PR; minor+patch suppressed (no branch). ' +
        'A branch on this image will then always mean a major.\n\n' +
        'Config valid: ' + authored.configValid + ' (' + authored.validation + ')\n' +
        'Added rules:\n' + authored.addedRules + '\n\n' +
        'Diff:\n' + authored.diff + '\n\n' +
        'Note: this SUPPRESSES patch too (so patches no longer auto-merge for this image). ' +
        'If you want patch kept (auto-merge) and only minor suppressed, choose "Request changes" and say so.\n' +
        'The standing renovate/<image>-43.x minor branch will be abandoned by Renovate on its next run.\n\n' +
        'PR title: ' + authored.prTitle,
      options: ['Approve push + merge', 'Request changes', 'Abort'],
      expert: 'owner',
      tags: ['deploy', 'outward-facing', 'approval-gate'],
      previousFeedback: lastFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    const r = (gate.response || '').toLowerCase();
    if (gate.approved && r.includes('approve')) { approved = true; break; }
    if (r.includes('abort') || (!gate.approved && !r)) {
      ctx.log('warn', 'Aborted — local branch/commit retained, nothing pushed.');
      return { success: false, merged: false, reason: 'aborted', branch: authored.branch, configValid: authored.configValid };
    }
    lastFeedback = gate.response || gate.feedback || 'Changes requested';
  }
  if (!approved) {
    return { success: false, merged: false, reason: 'not-approved-after-retries', branch: authored.branch };
  }

  // PHASE 2 — push + PR + merge.
  const pub = await ctx.task(publishTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo, branch: authored.branch,
    approvedPrTitle: authored.prTitle, approvedPrBody: authored.prBody,
  });
  ctx.log('info', `Merged: ${pub.merged}; PR=${pub.prUrl}`);

  return {
    success: pub.merged === true,
    merged: pub.merged,
    prUrl: pub.prUrl,
    mergeSha: pub.mergeSha,
    configValid: authored.configValid,
    summary: 'renovate.json now restricts ' + cfg.imageName + ' to major-only branch/PR; minor+patch suppressed. Standing 43.x minor branch will be abandoned on the next Renovate run.',
  };
}
