/**
 * @process issue-187-vram-obsolescence
 * @description Verify issue #187 (Odysseus in-process VRAM/GPU coordination) is obsolete after the
 *   k3s migration, get owner approval, then close it with a documented rationale (+ optional follow-up).
 * @inputs { issueNumber: number, repo: string, ownerContext: string }
 * @outputs { closed: boolean, recommendation: string, followUpIssue: (string|null) }
 *
 * GSD verify-work pattern: verify deliverable claim -> owner decision gate -> execute closure.
 * Low breakpoint tolerance (user profile): single decision-gate breakpoint before the outward-facing
 * GitHub issue closure (which is also the only state-changing step).
 *
 * @skill none
 * @agent general-purpose
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

export async function process(inputs, ctx) {
  const {
    issueNumber = 187,
    repo = 'SpyrosPsarras/epaflix',
    ownerContext = '',
  } = inputs;

  // ==========================================================================
  // PHASE 1: VERIFY OBSOLESCENCE AGAINST LIVE STATE
  // ==========================================================================
  let verification = await ctx.task(verifyObsolescenceTask, {
    issueNumber,
    repo,
    ownerContext,
  });

  // ==========================================================================
  // PHASE 2: OWNER DECISION GATE (retry/refine on rejection)
  // ==========================================================================
  let approval = { approved: false };
  let lastFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (lastFeedback) {
      verification = await ctx.task(verifyObsolescenceTask, {
        issueNumber,
        repo,
        ownerContext,
        feedback: lastFeedback,
        attempt: attempt + 1,
      });
    }

    approval = await ctx.breakpoint({
      question:
        `Issue #${issueNumber} appears OBSOLETE. Recommendation: ${verification.recommendation}.\n\n` +
        `Reason: ${verification.summary}\n\n` +
        `Proposed action: ${verification.proposedAction}\n` +
        (verification.followUpProposal
          ? `Proposed follow-up: ${verification.followUpProposal}\n`
          : 'No follow-up issue proposed.\n') +
        `\nApprove closing #${issueNumber} as described?`,
      title: `Close issue #${issueNumber} as obsolete?`,
      options: ['Approve and close', 'Request changes'],
      expert: 'owner',
      tags: ['approval-gate', 'issue-closure'],
      previousFeedback: lastFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
      context: {
        runId: ctx.runId,
        closingComment: verification.closingComment,
        followUpProposal: verification.followUpProposal || null,
      },
    });

    if (approval.approved) break;
    lastFeedback = approval.response || approval.feedback || 'Changes requested';
  }

  if (!approval.approved) {
    return {
      closed: false,
      recommendation: verification.recommendation,
      followUpIssue: null,
      reason: 'Owner did not approve closure',
      metadata: { processId: 'issue-187-vram-obsolescence', timestamp: ctx.now() },
    };
  }

  // ==========================================================================
  // PHASE 3: EXECUTE CLOSURE (comment + close + optional follow-up)
  // ==========================================================================
  const closure = await ctx.task(closeIssueTask, {
    issueNumber,
    repo,
    closingComment: verification.closingComment,
    followUpProposal: approval.feedback || approval.response
      ? verification.followUpProposal // owner may have amended; agent re-reads breakpoint feedback
      : verification.followUpProposal,
    ownerDecision: approval.response || 'Approve and close',
  });

  return {
    closed: closure.closed,
    recommendation: verification.recommendation,
    followUpIssue: closure.followUpIssue || null,
    closingCommentUrl: closure.closingCommentUrl || null,
    metadata: { processId: 'issue-187-vram-obsolescence', timestamp: ctx.now() },
  };
}

// ============================================================================
// TASKS
// ============================================================================

export const verifyObsolescenceTask = defineTask('verify-obsolescence', (args, taskCtx) => ({
  kind: 'agent',
  title: `Verify issue #${args.issueNumber} obsolescence against live state`,
  description: 'Confirm the VRAM/GPU-coordination premise no longer applies after the k3s migration',

  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'DevOps/SRE engineer auditing an infrastructure GitHub issue for obsolescence',
      task:
        `Determine whether GitHub issue #${args.issueNumber} in ${args.repo} is obsolete, and if so ` +
        'draft an accurate closing comment. The issue asks for a VRAM budget / GPU coordination scheme ' +
        'so that Odysseus in-process (Cookbook) model serving and Ollama cannot collectively exceed the ' +
        'single RTX 2070 SUPER 8GB GPU. The owner reports Odysseus has since moved into k3s (no GPU on ' +
        'k3s workers) and now uses the remote Ollama only for all LLM serving.',
      context: {
        issueNumber: args.issueNumber,
        repo: args.repo,
        ownerContext: args.ownerContext,
        repoPath: '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
        keyFiles: [
          '2-k3s/13.odysseus/configmap.yaml',
          '2-k3s/13.odysseus/odysseus.yaml',
          '2-k3s/13.odysseus/README.md',
        ],
        relatedIssues: [184, 183],
        previousFeedback: args.feedback || null,
        attempt: args.attempt || 1,
      },
      instructions: [
        'Run: gh issue view ' + args.issueNumber + ' --repo ' + args.repo + ' to read the full issue.',
        'Read 2-k3s/13.odysseus/configmap.yaml and 2-k3s/13.odysseus/odysseus.yaml in the repo.',
        'Confirm in git/manifests: (a) the k3s Odysseus Deployment requests NO GPU (no nvidia.com/gpu, no NVIDIA_* env), (b) LLM/embedding serving points only at remote Ollama (OLLAMA_BASE_URL/LLM_HOST -> http://192.168.10.200:30068), (c) there is no in-process Cookbook model-serving config.',
        'Optionally verify live: kubectl -n odysseus get deploy odysseus -o yaml | grep -iE "nvidia|gpu" (expect none). Do NOT change any live state.',
        'Decide a recommendation: one of "close-as-obsolete", "repurpose", or "keep-open". The premise (two models racing on one shared 8GB GPU) is impossible if Odysseus has no GPU and only calls remote Ollama.',
        'Assess whether any RESIDUAL concern survives that is NOT covered by other issues — e.g. does the remote Ollama itself still need a documented single-model / keep-alive cap independent of Odysseus? If a genuine, non-duplicate residual concern exists, propose a concise follow-up issue (title + one-line body in the ## Finding / ## Current state / ## Desired outcome / ## Notes shape). If not, set followUpProposal to null. Do NOT invent busywork.',
        'Draft a closing comment (closingComment) that: states the issue is obsolete, cites the concrete evidence (no GPU reservation on k3s, NVIDIA keys removed, OLLAMA_BASE_URL points at remote Ollama, fastembed on CPU), references the k3s migration (issue #184 resolution) and the #183 GPU-proof, and notes any follow-up if proposed. Keep it factual and brief. Refer to any media titles only by id, never by name.',
        'If previousFeedback is present, incorporate it into the recommendation and closingComment.',
        'Return ONLY the JSON result. Do not close the issue yourself in this task.',
      ],
      outputFormat:
        'JSON with recommendation (string), summary (string), proposedAction (string), ' +
        'closingComment (string), followUpProposal (string|null), evidence (array of strings)',
    },
    outputSchema: {
      type: 'object',
      required: ['recommendation', 'summary', 'proposedAction', 'closingComment'],
      properties: {
        recommendation: { type: 'string' },
        summary: { type: 'string' },
        proposedAction: { type: 'string' },
        closingComment: { type: 'string' },
        followUpProposal: { type: ['string', 'null'] },
        evidence: { type: 'array', items: { type: 'string' } },
      },
    },
  },

  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },

  labels: ['agent', 'devops', 'verification', 'issue-triage'],
}));

export const closeIssueTask = defineTask('close-issue', (args, taskCtx) => ({
  kind: 'agent',
  title: `Close issue #${args.issueNumber} with documented rationale`,
  description: 'Post the approved closing comment, close the issue, and open any approved follow-up',

  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'DevOps engineer executing an approved GitHub issue closure',
      task:
        `Owner approved closing issue #${args.issueNumber} in ${args.repo} as obsolete. ` +
        'Post the closing comment, close the issue, and open the follow-up issue if one was proposed.',
      context: {
        issueNumber: args.issueNumber,
        repo: args.repo,
        closingComment: args.closingComment,
        followUpProposal: args.followUpProposal || null,
        ownerDecision: args.ownerDecision,
      },
      instructions: [
        'Write the approved closingComment to a temp file and post it: gh issue comment ' + args.issueNumber + ' --repo ' + args.repo + ' --body-file <tmp>.',
        'Then close the issue: gh issue close ' + args.issueNumber + ' --repo ' + args.repo + ' --reason "not planned".',
        'If followUpProposal is a non-null string, create it: gh issue create --repo ' + args.repo + ' --title "<title>" --body "<body>" and cross-link it back in a comment on #' + args.issueNumber + '. If followUpProposal is null, skip follow-up creation.',
        'Capture the closing comment URL and (if created) the follow-up issue URL/number from the gh output.',
        'Verify with: gh issue view ' + args.issueNumber + ' --repo ' + args.repo + ' --json state,closed (expect closed).',
        'Do NOT commit anything to git; this task only touches GitHub issues.',
        'Return ONLY the JSON result.',
      ],
      outputFormat: 'JSON with closed (boolean), closingCommentUrl (string), followUpIssue (string|null)',
    },
    outputSchema: {
      type: 'object',
      required: ['closed'],
      properties: {
        closed: { type: 'boolean' },
        closingCommentUrl: { type: ['string', 'null'] },
        followUpIssue: { type: ['string', 'null'] },
      },
    },
  },

  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },

  labels: ['agent', 'devops', 'issue-closure', 'github'],
}));
