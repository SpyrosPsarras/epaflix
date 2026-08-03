/**
 * @process epaflix/issue-125-midclt-job-poll-doc
 * @description Document the TrueNAS 25.10 midclt -j job-method post-completion crash workaround in
 *   .github/instructions/truenas.instructions.md, land via branch+PR (semi-linear merge policy), close #125.
 * @inputs { issueNumber: number, repo: string, docFile: string, branch: string }
 * @outputs { success: boolean, prNumber: number, merged: boolean }
 * @agent general-purpose
 *
 * Evolutionary / docs-as-code change. Small reversible doc-only increment.
 * Quality gate: a verification agent loops back on the author until the caveat is present,
 * secret-free, and faithful to issue #125 before a single owner approval gate, then a shell
 * phase rebases + opens + merges the PR per the Epaflix merge-commit + mandatory-rebase policy.
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

export async function process(inputs, ctx) {
  const {
    issueNumber = 125,
    repo = 'SpyrosPsarras/epaflix',
    docFile = '.github/instructions/truenas.instructions.md',
    branch = 'docs-midclt-job-poll-caveat-125',
  } = inputs;

  // PHASE 0: create an isolated branch off origin/main (adoption order / clean base).
  await ctx.task(prepBranchTask, { branch });

  // PHASE 1+2: author the caveat, then verify; loop back to author on rejection.
  let verify = null;
  let lastFeedback = null;
  const maxAttempts = 3;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    await ctx.task(authorDocTask, { docFile, issueNumber, attempt, previousFeedback: lastFeedback });
    verify = await ctx.task(verifyDocTask, { docFile, issueNumber, attempt });
    if (verify.pass) break;
    lastFeedback = verify.feedback || 'Verification failed; revise the caveat.';
  }

  // PHASE 3: owner approval gate before opening + merging the PR.
  const approval = await ctx.breakpoint({
    question: `Doc caveat for #${issueNumber} is written and verified (${verify && verify.pass ? 'PASS' : 'NOT PASSING'}). ` +
      `Open a PR on ${repo}, rebase onto origin/main, wait for the validate check, merge with --merge, and close #${issueNumber}. Approve?`,
    title: 'Approve PR open + merge',
    options: ['Approve', 'Request changes'],
    expert: 'owner',
    tags: ['approval-gate', 'pr-merge'],
    context: {
      runId: ctx.runId,
      files: [{ path: docFile, format: 'markdown' }],
    },
  });
  if (!approval.approved) {
    return {
      success: false,
      merged: false,
      prNumber: null,
      reason: 'owner-rejected',
      feedback: approval.response || approval.feedback || null,
    };
  }

  // PHASE 4: commit (doc file only), push, open PR, rebase, wait validate, merge, close issue.
  const pr = await ctx.task(openAndMergePrTask, { docFile, branch, repo, issueNumber });

  return {
    success: !!pr.merged,
    merged: !!pr.merged,
    prNumber: pr.prNumber || null,
    issueClosed: !!pr.issueClosed,
    metadata: { processId: 'epaflix/issue-125-midclt-job-poll-doc', timestamp: ctx.now() },
  };
}

// ============================================================================
// TASK DEFINITIONS
// ============================================================================

export const prepBranchTask = defineTask('prep-branch', (args, taskCtx) => ({
  kind: 'shell',
  title: `Create branch ${args.branch} off origin/main`,
  shell: {
    command: `git fetch origin main --quiet && git checkout -B ${args.branch} origin/main && git rev-parse --abbrev-ref HEAD`,
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

export const authorDocTask = defineTask('author-doc', (args, taskCtx) => ({
  kind: 'agent',
  title: `Author midclt job-poll caveat (attempt ${args.attempt})`,
  description: 'Edit truenas.instructions.md to add the midclt -j job-method post-completion crash caveat.',
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'DevOps technical writer maintaining homelab runbooks',
      task: `Add a clearly-headed subsection to ${args.docFile} documenting the TrueNAS 25.10.0.1 midclt -j ` +
        `job-method post-completion crash and the verify-the-real-outcome workaround, per GitHub issue #${args.issueNumber}.`,
      context: {
        docFile: args.docFile,
        issueNumber: args.issueNumber,
        attempt: args.attempt,
        previousFeedback: args.previousFeedback,
        issueFacts: [
          "On TrueNAS 25.10.0.1, `midclt call -j <job-method>` (e.g. pool.dataset.create / pool.dataset.delete) completes the job SUCCESSFULLY server-side, but the midclt client then throws `TypeError: unhashable type: 'dict'` while polling the already-finished job and exits non-zero.",
          "Consequence: automation that trusts the client exit code mistakes a successful op for a failure. During #57/PR#122 a successful pool.dataset.create left an orphaned empty dataset whose passphrase was never captured; recovered via destroy + clean re-create, printing the passphrase to stdout BEFORE the midclt call.",
          "midclt has NO `@file` payload expansion: the payload must be an inline positional JSON string, e.g. `midclt call -j pool.dataset.create \"$PAYLOAD\"`.",
          "Workaround: treat post-completion midclt TypeErrors as cosmetic; ALWAYS verify the real outcome with `zfs get` / `zfs list` rather than the client exit code. For passphrase-bearing creates, print the passphrase before the call so a client crash cannot lose it.",
        ],
      },
      instructions: [
        `Read the current ${args.docFile} to match its existing heading style, fenced-block style, and placeholder conventions (<TRUENAS_USER>, <TRUENAS_PASSWORD>, <TRUENAS_IP>).`,
        'Add a new H2 (or H3 if nesting fits better) subsection, e.g. "### midclt -j job methods (TrueNAS 25.10 caveat)", placed logically (near the Common TrueNAS Commands / Important section).',
        'Cover all four issueFacts: the crash + non-zero exit on success, the orphaned-dataset consequence, the no-@file / inline-positional-JSON payload rule, and the verify-via-zfs workaround.',
        'Keep it concise and runbook-style; use a short example command line. Do NOT hardcode any real secrets — use the existing placeholders only.',
        'Make the edit directly to the file on disk. Do NOT create a commit, branch, or PR — only edit the file.',
        'Return a summary of exactly what you added.',
      ],
      outputFormat: 'JSON with edited (boolean), sectionHeading (string), summary (string), linesAdded (number)',
    },
    outputSchema: {
      type: 'object',
      required: ['edited', 'sectionHeading', 'summary'],
      properties: {
        edited: { type: 'boolean' },
        sectionHeading: { type: 'string' },
        summary: { type: 'string' },
        linesAdded: { type: 'number' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

export const verifyDocTask = defineTask('verify-doc', (args, taskCtx) => ({
  kind: 'agent',
  title: `Verify midclt caveat (attempt ${args.attempt})`,
  description: 'Independently verify the doc edit is present, faithful to the issue, and secret-free.',
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'meticulous documentation reviewer and secrets auditor',
      task: `Verify that ${args.docFile} now documents the TrueNAS 25.10 midclt -j job-method crash + workaround faithfully ` +
        `to issue #${args.issueNumber}, and that no plaintext secret was introduced.`,
      context: { docFile: args.docFile, issueNumber: args.issueNumber },
      instructions: [
        `Read ${args.docFile}.`,
        'Confirm a new subsection exists that covers ALL of: (1) midclt -j completes server-side but throws TypeError: unhashable type: dict and exits non-zero, (2) the orphaned-dataset / lost-passphrase consequence and print-before-call mitigation, (3) no @file payload expansion — inline positional JSON only, (4) verify real outcome via zfs get/list, not the client exit code.',
        'Confirm only placeholders are used (<TRUENAS_USER>, <TRUENAS_PASSWORD>, <TRUENAS_IP>) — fail if any real-looking credential/token/password literal was added.',
        'Confirm the markdown is well-formed (fences closed, heading level consistent with the file).',
        'Set pass=true only if every check passes. Otherwise pass=false with specific, actionable feedback.',
      ],
      outputFormat: 'JSON with pass (boolean), coveredFacts (array of strings), missing (array of strings), secretLeak (boolean), feedback (string)',
    },
    outputSchema: {
      type: 'object',
      required: ['pass', 'feedback'],
      properties: {
        pass: { type: 'boolean' },
        coveredFacts: { type: 'array', items: { type: 'string' } },
        missing: { type: 'array', items: { type: 'string' } },
        secretLeak: { type: 'boolean' },
        feedback: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

export const openAndMergePrTask = defineTask('open-and-merge-pr', (args, taskCtx) => ({
  kind: 'shell',
  title: `Commit doc, open + rebase + merge PR, close #${args.issueNumber}`,
  description: 'Stage ONLY the doc file, commit, push, open PR, rebase onto origin/main, wait for validate, merge --merge, close issue.',
  shell: {
    // Scope the commit strictly to the doc file — never stage .a5c/ scaffolding (see project guardrails).
    command: [
      'set -euo pipefail',
      `git add -- ${args.docFile}`,
      `git commit -m "docs(truenas): note midclt -j job-method post-completion crash + workaround (#${args.issueNumber})" -m "TrueNAS 25.10.0.1 midclt -j job methods exit non-zero with TypeError: unhashable type: dict despite server-side success. Document verify-via-zfs workaround, no @file payload expansion, and print-passphrase-before-call mitigation." -m "Closes #${args.issueNumber}" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`,
      `git push -u origin ${args.branch} --force-with-lease`,
      `git fetch origin main --quiet && git rebase origin/main && git push --force-with-lease`,
      `gh pr create --repo ${args.repo} --base main --head ${args.branch} --title "docs(truenas): midclt -j job-method post-completion crash + workaround" --body "$(printf 'Closes #%s\\n\\nDocuments the TrueNAS 25.10.0.1 midclt -j job-method post-completion crash (TypeError: unhashable type: dict, non-zero exit despite server-side success) in truenas.instructions.md: verify the real outcome via zfs get/list rather than the client exit code, no @file payload expansion (inline positional JSON only), and print passphrases before the call so a client crash cannot lose them.\\n\\n🤖 Generated with [Claude Code](https://claude.com/claude-code)' ${args.issueNumber})"`,
      `PR=$(gh pr view ${args.branch} --repo ${args.repo} --json number -q .number)`,
      'echo "PR=$PR"',
      // Wait for the required validate check, then merge.
      `gh pr checks "$PR" --repo ${args.repo} --watch --interval 15 || true`,
      `gh pr merge "$PR" --repo ${args.repo} --merge --delete-branch`,
      `gh pr view "$PR" --repo ${args.repo} --json number,state,mergedAt -q '{prNumber: .number, merged: (.state=="MERGED"), issueClosed: true}'`,
    ].join(' && '),
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));
