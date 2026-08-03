/**
 * @process issue-188-readme-reconcile
 * @description Triage issue #188 (Odysseus TrueNAS README build runbook drift), get owner approval on
 *   the disposition, then execute it — update the README via the branch+PR+rebase+merge policy, or
 *   close as obsolete — and close the issue.
 * @inputs { issueNumber: number, repo: string, readmePath: string, imageId: string, ownerContext: string }
 * @outputs { closed: boolean, disposition: string, prUrl: (string|null) }
 *
 * GSD verify-work shape: triage/plan -> owner decision gate (retry/refine) -> execute.
 * Low breakpoint tolerance (user profile): single decision gate that authorizes BOTH the chosen change
 * AND the outward-facing PR+merge / issue-close that follows.
 *
 * @skill none
 * @agent general-purpose
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

export async function process(inputs, ctx) {
  const {
    issueNumber = 188,
    repo = 'SpyrosPsarras/epaflix',
    readmePath = '0-truenas/custom-apps/odysseus/README.md',
    imageId = '44a5ac0a2364',
    ownerContext = '',
  } = inputs;

  // ==========================================================================
  // PHASE 1: TRIAGE + DRAFT THE CHANGE
  // ==========================================================================
  let triage = await ctx.task(triageTask, {
    issueNumber,
    repo,
    readmePath,
    imageId,
    ownerContext,
  });

  // ==========================================================================
  // PHASE 2: OWNER DECISION GATE (retry/refine on rejection)
  // ==========================================================================
  let approval = { approved: false };
  let lastFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (lastFeedback) {
      triage = await ctx.task(triageTask, {
        issueNumber,
        repo,
        readmePath,
        imageId,
        ownerContext,
        feedback: lastFeedback,
        attempt: attempt + 1,
      });
    }

    approval = await ctx.breakpoint({
      question:
        `Issue #${issueNumber} disposition: ${triage.disposition}.\n\n` +
        `Why: ${triage.summary}\n\n` +
        `Planned change: ${triage.planSummary}\n\n` +
        `Execution: ${triage.disposition === 'close-obsolete'
          ? 'comment + close the issue (no code change).'
          : 'edit the README on a new branch, then branch+PR+rebase+wait-validate+merge per repo policy, then close #' + issueNumber + '.'}\n\n` +
        `Approve this disposition AND the execution (including any PR + merge)?`,
      title: `Issue #${issueNumber}: approve disposition + execution?`,
      options: ['Approve and execute', 'Request changes'],
      expert: 'owner',
      tags: ['approval-gate', 'doc-change', 'pr-merge'],
      previousFeedback: lastFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
      context: {
        runId: ctx.runId,
        disposition: triage.disposition,
        proposedEdits: triage.proposedEdits,
        closingComment: triage.closingComment,
      },
    });

    if (approval.approved) break;
    lastFeedback = approval.response || approval.feedback || 'Changes requested';
  }

  if (!approval.approved) {
    return {
      closed: false,
      disposition: triage.disposition,
      prUrl: null,
      reason: 'Owner did not approve',
      metadata: { processId: 'issue-188-readme-reconcile', timestamp: ctx.now() },
    };
  }

  // ==========================================================================
  // PHASE 3: EXECUTE
  // ==========================================================================
  const exec = await ctx.task(executeTask, {
    issueNumber,
    repo,
    readmePath,
    imageId,
    disposition: triage.disposition,
    proposedEdits: triage.proposedEdits,
    closingComment: triage.closingComment,
    prTitle: triage.prTitle,
    prBody: triage.prBody,
    ownerDecision: approval.response || 'Approve and execute',
  });

  return {
    closed: exec.closed,
    disposition: triage.disposition,
    prUrl: exec.prUrl || null,
    merged: exec.merged || false,
    metadata: { processId: 'issue-188-readme-reconcile', timestamp: ctx.now() },
  };
}

// ============================================================================
// TASKS
// ============================================================================

export const triageTask = defineTask('triage-188', (args, taskCtx) => ({
  kind: 'agent',
  title: `Triage issue #${args.issueNumber} and draft the change`,
  description: 'Decide disposition and draft exact README edits or closing rationale',

  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'DevOps/SRE engineer triaging a documentation-drift GitHub issue',
      task:
        `Triage GitHub issue #${args.issueNumber} in ${args.repo} and produce a concrete plan. The issue ` +
        `asks to reconcile ${args.readmePath}: the build runbook documents a "docker save | docker load" ` +
        'transfer path, but the image was actually built ON the TrueNAS docker engine, and the pinned ' +
        `image id placeholder was never filled (actual id: ${args.imageId}). IMPORTANT context: the ` +
        'TrueNAS Odysseus Custom App has since been DECOMMISSIONED — Odysseus now runs on k3s and pulls ' +
        'the image from GHCR (ghcr.io/spyrospsarras/odysseus:73673258), per the #184 migration. So this ' +
        'README documents a deployment that no longer runs.',
      context: {
        issueNumber: args.issueNumber,
        repo: args.repo,
        readmePath: args.readmePath,
        imageId: args.imageId,
        repoPath: '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
        ownerContext: args.ownerContext,
        relatedIssues: [183, 184, 187, 212],
        previousFeedback: args.feedback || null,
        attempt: args.attempt || 1,
      },
      instructions: [
        'Run: gh issue view ' + args.issueNumber + ' --repo ' + args.repo + ' to read the full issue.',
        'Read ' + args.readmePath + ' fully. Note line ~67 placeholder "<RECORD_IMAGE_ID_HERE_AFTER_LOAD>" and the section that presents docker save | docker load as the build/transfer path.',
        'Decide a disposition: "doc-fix" (do exactly what the issue asks), "doc-fix-with-superseded-note" (fix it AND add a short banner that this TrueNAS Custom App path is decommissioned/superseded by the k3s+GHCR deployment, cross-ref #184), or "close-obsolete" (only if the doc has truly no remaining value). Recommend the most honest, lowest-cost option. Filling a known image id + correcting a build path is cheap and improves provenance, so prefer fixing over closing unless there is a strong reason.',
        'Draft proposedEdits as an array of exact find/replace pairs (old_string verbatim from the file, new_string) that an executor can apply with an Edit tool. Cover: (1) fill the pinned image id ' + args.imageId + ', (2) reframe so the on-engine build is the ACTUAL path used and docker save | docker load is the ALTERNATIVE for a remote builder, (3) if disposition includes a superseded note, add a brief top banner pointing to the k3s deployment (2-k3s/13.odysseus) and #184. Keep edits minimal and surgical; do not rewrite the whole file.',
        'Make sure each old_string is unique and copied verbatim (including indentation/markdown) so the edit will apply cleanly.',
        'Draft prTitle (conventional commit style, e.g. "docs(odysseus): reconcile TrueNAS build runbook with on-engine build + record image id (#188)") and a short prBody with a ## Summary and a ## Test plan (e.g. "verify image id filled", "no docker save|load presented as the path actually used").',
        'Draft a closingComment to post when #' + args.issueNumber + ' is closed after merge (or, for close-obsolete, the standalone closing rationale).',
        'Refer to any media titles only by id, never by name. Do NOT edit any files or run git in this task — only read and draft.',
        'If previousFeedback is present, revise the plan accordingly.',
        'Return ONLY the JSON result.',
      ],
      outputFormat:
        'JSON with disposition (string), summary (string), planSummary (string), ' +
        'proposedEdits (array of {old_string,new_string}), prTitle (string), prBody (string), ' +
        'closingComment (string)',
    },
    outputSchema: {
      type: 'object',
      required: ['disposition', 'summary', 'planSummary', 'proposedEdits', 'closingComment'],
      properties: {
        disposition: { type: 'string' },
        summary: { type: 'string' },
        planSummary: { type: 'string' },
        proposedEdits: {
          type: 'array',
          items: {
            type: 'object',
            required: ['old_string', 'new_string'],
            properties: {
              old_string: { type: 'string' },
              new_string: { type: 'string' },
            },
          },
        },
        prTitle: { type: ['string', 'null'] },
        prBody: { type: ['string', 'null'] },
        closingComment: { type: 'string' },
      },
    },
  },

  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },

  labels: ['agent', 'devops', 'triage', 'docs'],
}));

export const executeTask = defineTask('execute-188', (args, taskCtx) => ({
  kind: 'agent',
  title: `Execute approved disposition for issue #${args.issueNumber}`,
  description: 'Apply README edits via branch+PR+rebase+merge policy, or close as obsolete',

  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'DevOps engineer executing an approved, owner-authorized change in the epaflix repo',
      task:
        `Execute the approved disposition "${args.disposition}" for issue #${args.issueNumber} in ${args.repo}.`,
      context: {
        issueNumber: args.issueNumber,
        repo: args.repo,
        readmePath: args.readmePath,
        imageId: args.imageId,
        disposition: args.disposition,
        proposedEdits: args.proposedEdits,
        closingComment: args.closingComment,
        prTitle: args.prTitle,
        prBody: args.prBody,
        repoPath: '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
        mergePolicy:
          'MERGE-COMMIT + mandatory rebase (semi-linear). Branch from main, push, open PR (0 approvals ' +
          'required), rebase onto origin/main + git push --force-with-lease, wait for the required ' +
          '"validate" check to pass, then: gh pr merge <n> --merge. Commit footer must end with ' +
          '"Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>". PR body must end with ' +
          'the "🤖 Generated with [Claude Code](https://claude.com/claude-code)" line.',
      },
      instructions: [
        'If disposition is "close-obsolete": skip all git/PR work. Just post closingComment via gh issue comment ' + args.issueNumber + ' and close with gh issue close ' + args.issueNumber + ' --repo ' + args.repo + ' --reason "not planned". Return {closed:true, prUrl:null, merged:false}.',
        'Otherwise (doc-fix or doc-fix-with-superseded-note): work in ' + '/home/spy/Documents/Epaflix/k3s-swarm-proxmox' + '. Confirm git status is clean first; if there are unrelated uncommitted changes, STOP and report them rather than committing them.',
        'Create a branch: git checkout main && git pull --ff-only origin main && git checkout -b issue-188-readme-reconcile (if the branch exists, reuse/reset it to origin/main).',
        'Apply EACH item in proposedEdits to ' + args.readmePath + ' using exact string replacement. If any old_string does not match verbatim, re-read the file, adapt the match, and apply the intended change; ensure the pinned image id ' + args.imageId + ' ends up in the file and the on-engine build is presented as the actual path. Verify with grep that "' + args.imageId + '" is present and the "<RECORD_IMAGE_ID_HERE_AFTER_LOAD>" placeholder is gone.',
        'Commit only ' + args.readmePath + ' (git add the single file) with a conventional message matching prTitle and the required Co-Authored-By footer.',
        'Push: git push -u origin issue-188-readme-reconcile.',
        'Open the PR: gh pr create --repo ' + args.repo + ' --base main --head issue-188-readme-reconcile --title "<prTitle>" --body "<prBody ending with the Claude Code generated line>".',
        'Rebase onto latest main and re-push: git fetch origin && git rebase origin/main && git push --force-with-lease. Resolve trivial conflicts; if a non-trivial conflict arises, STOP and report.',
        'Wait for the required "validate" check: poll gh pr checks <n> --repo ' + args.repo + ' (or gh pr view <n> --json statusCheckRollup) until validate is SUCCESS (give it several minutes). Do NOT merge until validate passes.',
        'Merge: gh pr merge <n> --repo ' + args.repo + ' --merge. Confirm merged.',
        'Post closingComment on the issue and close it: gh issue comment ' + args.issueNumber + ' then gh issue close ' + args.issueNumber + ' --repo ' + args.repo + ' (closing as completed). If the merge auto-closed it via a "Closes #188" in the PR body, just verify it is closed and still post the closingComment.',
        'Verify final state: gh issue view ' + args.issueNumber + ' --repo ' + args.repo + ' --json state,closed and gh pr view <n> --json state,merged.',
        'Refer to any media titles only by id, never by name. Do NOT touch secrets or any .enc.yaml files.',
        'Return ONLY the JSON result with the actual PR url, merge state, and close state.',
      ],
      outputFormat: 'JSON with closed (boolean), prUrl (string|null), merged (boolean), prNumber (number|null)',
    },
    outputSchema: {
      type: 'object',
      required: ['closed'],
      properties: {
        closed: { type: 'boolean' },
        prUrl: { type: ['string', 'null'] },
        merged: { type: 'boolean' },
        prNumber: { type: ['number', 'null'] },
      },
    },
  },

  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },

  labels: ['agent', 'devops', 'docs', 'git', 'pr-merge'],
}));
