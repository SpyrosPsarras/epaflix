/**
 * @process project/rotate-authentik-admin-token
 * @description Issue #175 — Revoke & retire the long-lived Authentik superuser API token
 * (`authentik_admin_api_token` in secrets.yml). The token is already expired (HTTP 403) and is
 * consumed by NO running workload (only doc references), so the approved end-state (Approach A) is:
 * no standing long-lived admin token — delete the dead token object in Authentik, remove the key
 * from the git-ignored secrets.yml, and document an on-demand scoped+expiring minting/rotation
 * runbook in the Authentik README. Docs-only committed change; live revocation is owner-operated
 * behind a mandatory secrets-rotation breakpoint; merge behind a final approval breakpoint.
 *
 * @inputs { issue: number, issueUrl: string, repo: string }
 * @outputs { success: boolean, prUrl: string, issueClosed: boolean, followUps: array }
 *
 * Composition references (process library):
 *  - specializations/devops-sre-platform/secrets-management.js  (assess -> review-gate -> rotate -> verify -> document)
 *  - methodologies/gsd/verify-work  (quality gate = no secret leak, docs accurate, CI/kustomize unaffected)
 *
 * @skill none
 * @agent general-purpose specializations/devops-sre-platform/agents
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

export async function process(inputs, ctx) {
  const {
    issue = 175,
    issueUrl = 'https://github.com/SpyrosPsarras/epaflix/issues/175',
    repo = 'SpyrosPsarras/epaflix',
  } = inputs || {};

  const REPO_ROOT = '/home/spy/Documents/Epaflix/k3s-swarm-proxmox';
  const SECRETS_KEY = 'authentik_admin_api_token';
  const branch = `issue-${issue}-authentik-admin-token-revoke`;

  ctx.log('info', `Issue #${issue}: revoke & retire ${SECRETS_KEY} (Approach A — no standing admin token)`);

  // ──────────────────────────────────────────────────────────────────────────
  // PHASE 1: ASSESS — re-confirm blast radius, expiry, and exact edit plan
  // ──────────────────────────────────────────────────────────────────────────
  const assessment = await ctx.task(assessTask, { repoRoot: REPO_ROOT, secretsKey: SECRETS_KEY, issue, repo });

  // ──────────────────────────────────────────────────────────────────────────
  // PHASE 2: AUTHOR DOCS (committed) — runbook + fix stale notes, on a fresh branch
  //          with a doc-review quality gate + refinement loop
  // ──────────────────────────────────────────────────────────────────────────
  let docs;
  let docFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    docs = await ctx.task(authorDocsTask, {
      repoRoot: REPO_ROOT, branch, secretsKey: SECRETS_KEY, issue, repo,
      editPlan: assessment.editPlan, feedback: docFeedback || undefined, attempt: attempt + 1,
    });
    const review = await ctx.task(docReviewTask, {
      repoRoot: REPO_ROOT, branch, secretsKey: SECRETS_KEY, filesChanged: docs.filesChanged,
    });
    if (review.pass) { docs.review = review; break; }
    docFeedback = review.issues && review.issues.join('; ') || 'Doc review failed; address issues.';
    ctx.log('warn', `Doc review failed (attempt ${attempt + 1}): ${docFeedback}`);
  }

  // ──────────────────────────────────────────────────────────────────────────
  // PHASE 3: SECRETS-ROTATION BREAKPOINT (always-break, owner)
  //          Owner performs the LIVE Authentik token deletion, then approves.
  // ──────────────────────────────────────────────────────────────────────────
  let rotationApproved = false;
  let bpFeedback = null;
  for (let attempt = 0; attempt < 5; attempt++) {
    const bp = await ctx.breakpoint({
      question:
        `SECRETS ROTATION (Approach A — revoke & retire). The committed docs are ready on branch \`${branch}\`. ` +
        `Now perform the LIVE revocation in Authentik, then approve.\n\n` +
        `OWNER STEPS:\n` +
        `1. Authentik (auth.epaflix.com) → Directory → Tokens. Find the token identified as \`authentik-admin-api-token\` ` +
        `(or the superuser/service-account token created for #134) and DELETE it. If it was minted under a dedicated ` +
        `service account that now has no other purpose, delete that service account too.\n` +
        `2. Confirm it is gone: \`GET /api/v3/core/users/me/\` with the old token must return 401/403 (it already returns 403).\n` +
        `3. (I will then remove the \`${SECRETS_KEY}\` line from the git-ignored secrets.yml automatically and verify.)\n\n` +
        `Approve once the live token object is deleted in Authentik.`,
      title: 'Secrets rotation — delete live Authentik admin token',
      context: {
        runId: ctx.runId, issue, issueUrl,
        approach: 'A — revoke & retire (no standing long-lived admin token)',
        blastRadius: assessment.blastRadius,
        liveTokenStatusBefore: assessment.liveTokenStatus,
        branch,
        filesChanged: docs.filesChanged,
        secretsYmlLineToRemove: SECRETS_KEY,
      },
      expert: 'owner',
      tags: ['approval-gate', 'secrets-rotation'],
      previousFeedback: bpFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    if (bp.approved) { rotationApproved = true; break; }
    bpFeedback = bp.response || bp.feedback || 'Not approved';
    ctx.log('warn', `Rotation breakpoint not approved (attempt ${attempt + 1}): ${bpFeedback}`);
  }
  if (!rotationApproved) {
    ctx.log('error', 'Rotation not approved after retries; aborting without changes to live state.');
    return { success: false, prUrl: null, issueClosed: false, followUps: [], aborted: 'rotation-not-approved' };
  }

  // ──────────────────────────────────────────────────────────────────────────
  // PHASE 4: VERIFY RETIRED — remove the dead key from secrets.yml (local) + confirm
  // ──────────────────────────────────────────────────────────────────────────
  const verify = await ctx.task(verifyRetiredTask, { repoRoot: REPO_ROOT, secretsKey: SECRETS_KEY });

  // ──────────────────────────────────────────────────────────────────────────
  // PHASE 5: FINALIZE — push branch, open PR (Epaflix merge policy), follow-ups, update issue
  // ──────────────────────────────────────────────────────────────────────────
  const finalize = await ctx.task(finalizeTask, {
    repoRoot: REPO_ROOT, branch, issue, issueUrl, repo,
    docsSummary: docs.summary, verifySummary: verify.summary,
  });

  // ──────────────────────────────────────────────────────────────────────────
  // PHASE 6: MERGE BREAKPOINT (owner) → merge per policy → close issue
  // ──────────────────────────────────────────────────────────────────────────
  let mergeApproved = false;
  let mergeFeedback = null;
  for (let attempt = 0; attempt < 5; attempt++) {
    const bp = await ctx.breakpoint({
      question:
        `Docs PR is open: ${finalize.prUrl}\n` +
        `It is a docs-only change (no manifest -> no live ArgoCD reconcile). Approve to rebase onto origin/main, ` +
        `wait for the \`validate\` check, and \`gh pr merge --merge\` per the Epaflix semi-linear merge policy, then close #${issue}?`,
      title: 'Approve merge of docs PR',
      context: {
        runId: ctx.runId, prUrl: finalize.prUrl, issue,
        followUps: finalize.followUps,
        docsOnly: true,
      },
      expert: 'owner',
      tags: ['approval-gate', 'deploy', 'destructive-git'],
      previousFeedback: mergeFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    if (bp.approved) { mergeApproved = true; break; }
    mergeFeedback = bp.response || bp.feedback || 'Not approved';
    ctx.log('warn', `Merge breakpoint not approved (attempt ${attempt + 1}): ${mergeFeedback}`);
  }
  if (!mergeApproved) {
    ctx.log('warn', 'Merge not approved; leaving PR open for the owner.');
    return { success: true, prUrl: finalize.prUrl, issueClosed: false, followUps: finalize.followUps, merged: false };
  }

  const merge = await ctx.task(mergeTask, { repoRoot: REPO_ROOT, branch, issue, repo, prUrl: finalize.prUrl });

  ctx.log('info', `Done. PR ${finalize.prUrl} merged=${merge.merged}, issue #${issue} closed=${merge.issueClosed}.`);
  return {
    success: true,
    prUrl: finalize.prUrl,
    merged: merge.merged,
    issueClosed: merge.issueClosed,
    followUps: finalize.followUps,
  };
}

// ════════════════════════════════════════════════════════════════════════════
// TASKS
// ════════════════════════════════════════════════════════════════════════════

export const assessTask = defineTask('assess-authentik-token', (args, taskCtx) => ({
  kind: 'agent',
  title: `Assess ${args.secretsKey} — blast radius, expiry, edit plan`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'DevOps / secrets-management specialist',
      task: `Assess the Authentik admin API token (#${args.issue}) for revoke-and-retire. READ-ONLY — make no edits.`,
      context: { repoRoot: args.repoRoot, secretsKey: args.secretsKey, issue: args.issue, repo: args.repo },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. Confirm blast radius: grep the whole repo (excluding .a5c/ and node_modules/) for "${args.secretsKey}" and for any manifest/initContainer/script that READS it. Confirm it is referenced ONLY in documentation (expected: 0-truenas/custom-apps/odysseus/README.md and docs/superpowers/plans/2026-05-25-sops-secret-automation.md). Report every reference with file:line.`,
        `2. Probe live token status (read-only): extract the token value from .github/instructions/secrets.yml and run: curl -s -o /dev/null -w "%{http_code}" -m 10 -H "Authorization: Bearer <TOKEN>" https://auth.epaflix.com/api/v3/core/users/me/  . Report the HTTP code (expected 403 = already expired). NEVER print the token value itself.`,
        `3. Build the exact edit plan for the COMMITTED docs change (Approach A = revoke & retire, no standing admin token):`,
        `   a. 2-k3s/07.authentik-deployment/README.md — ADD a new section documenting the admin API token policy: the standing superuser token is retired; future SSO automation should mint a SHORT-LIVED, SCOPED service-account token on demand (Directory → Tokens / service account) and DELETE it immediately after use; include the exact create + delete steps and the verify curl. Cross-link #134 (what created it) and #${args.issue} (this retirement). Identify the best heading location (near "Authorization & Application Integration" / "Granting Service Access").`,
        `   b. 0-truenas/custom-apps/odysseus/README.md — the "Step 0 — PREREQUISITE / BLOCKER" note says the token is expired and blocks setup. Odysseus SSO is already done; rewrite that note so it no longer presents a dead standing token as the path — point to the new on-demand minting runbook in the Authentik README and note the standing token was retired by #${args.issue}. Preserve all the real odysseus pk/uuid facts.`,
        `   c. docs/superpowers/plans/2026-05-25-sops-secret-automation.md — only a passing mention; assess whether it needs a one-line note. Recommend keep/skip.`,
        `4. Do NOT edit secrets.yml or any file. Output the plan only.`,
        'Return ONLY the JSON result object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['references', 'blastRadius', 'liveTokenStatus', 'editPlan'],
      properties: {
        references: { type: 'array', items: { type: 'string' } },
        blastRadius: { type: 'string' },
        liveTokenStatus: { type: 'string' },
        editPlan: {
          type: 'object',
          properties: {
            authentikReadme: { type: 'string' },
            odysseusReadme: { type: 'string' },
            sopsPlan: { type: 'string' },
          },
        },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'assessment', 'secrets'],
}));

export const authorDocsTask = defineTask('author-token-docs', (args, taskCtx) => ({
  kind: 'agent',
  title: `Author retirement runbook + fix stale notes (attempt ${args.attempt})`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'DevOps technical writer working in a GitOps IaC repo',
      task: `Make the committed docs-only change for issue #${args.issue} (revoke & retire the Authentik admin API token) on a fresh branch.`,
      context: {
        repoRoot: args.repoRoot, branch: args.branch, secretsKey: args.secretsKey,
        issue: args.issue, repo: args.repo, editPlan: args.editPlan,
        feedback: args.feedback || null,
      },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. Branch: git fetch origin && create/checkout branch "${args.branch}" off origin/main (git checkout -B ${args.branch} origin/main). The current checked-out branch is a different (merged) issue branch — always base off origin/main.`,
        `If "feedback" is provided, you are REFINING an existing branch — incorporate the feedback instead of starting over.`,
        `2. Edit 2-k3s/07.authentik-deployment/README.md per editPlan.authentikReadme: add an "Admin API Token (retired — mint on demand)" runbook. State that the standing superuser token (secrets.yml key ${args.secretsKey}) was retired under #${args.issue} because the #134 automation that needed it is complete; there is now NO standing long-lived admin token. Document how to mint a SHORT-LIVED, SCOPED service-account token on demand for future SSO automation and delete it right after, with exact UI steps and the verify curl (GET /api/v3/core/users/me/). Cross-link #134 and #${args.issue}.`,
        `3. Edit 0-truenas/custom-apps/odysseus/README.md per editPlan.odysseusReadme: rewrite the "Step 0 / BLOCKER" expired-token note to point at the new Authentik runbook and note the standing token was retired by #${args.issue}. Keep all real pk/uuid facts intact.`,
        `4. Apply editPlan.sopsPlan recommendation for docs/superpowers/plans/2026-05-25-sops-secret-automation.md (likely a one-line note or skip).`,
        `5. Do NOT touch secrets.yml. Do NOT print or embed any real token value anywhere. Use only placeholders like <SCOPED_TOKEN>.`,
        `6. Verify no manifest/kustomize change crept in: git diff --name-only origin/main must list ONLY .md files.`,
        `7. Commit (do not push) with subject: "docs(authentik): retire standing admin API token, document on-demand minting (#${args.issue})" and end the commit body with the line: Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`,
        'Return ONLY the JSON result object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'filesChanged', 'commitSha', 'summary'],
      properties: {
        branch: { type: 'string' },
        filesChanged: { type: 'array', items: { type: 'string' } },
        commitSha: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'docs', 'secrets'],
}));

export const docReviewTask = defineTask('review-token-docs', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Quality gate — verify docs accuracy & no secret leak',
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Adversarial reviewer for a GitOps IaC repo',
      task: 'Verify the committed docs-only change is correct, complete, and leaks no secrets.',
      context: { repoRoot: args.repoRoot, branch: args.branch, secretsKey: args.secretsKey, filesChanged: args.filesChanged },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. git diff origin/main...${args.branch} -- and confirm ONLY .md files changed (FAIL if any .yaml/.yml/.sh/manifest changed).`,
        `2. Confirm NO real secret value appears in the diff: grep the diff for anything resembling a live token (long base64/hex strings, "Bearer", actual values of ${args.secretsKey}). Only placeholders allowed. FAIL on any real value.`,
        `3. Confirm the Authentik README runbook clearly states the standing token is RETIRED, documents on-demand scoped+expiring minting + deletion, and cross-links #134 and #175.`,
        `4. Confirm the odysseus README stale "Step 0 blocker" note was fixed (no longer presents the dead standing token as the path) and real pk/uuid facts are preserved.`,
        `5. Confirm the commit message subject/footer are correct.`,
        `Return pass=true only if all checks hold; otherwise pass=false with a concrete issues list.`,
        'Return ONLY the JSON result object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['pass', 'issues'],
      properties: {
        pass: { type: 'boolean' },
        issues: { type: 'array', items: { type: 'string' } },
        notes: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'review', 'quality-gate'],
}));

export const verifyRetiredTask = defineTask('verify-token-retired', (args, taskCtx) => ({
  kind: 'agent',
  title: `Remove ${args.secretsKey} from secrets.yml (local) + verify retirement`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'DevOps operator handling a git-ignored secrets file',
      task: `The owner has deleted the live Authentik admin token. Remove the now-dead ${args.secretsKey} entry from the git-ignored secrets.yml and verify retirement.`,
      context: { repoRoot: args.repoRoot, secretsKey: args.secretsKey },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. Confirm .github/instructions/secrets.yml is git-ignored (git check-ignore .github/instructions/secrets.yml must succeed). NEVER git-add it.`,
        `2. Probe the live token is dead: extract its value, curl GET /api/v3/core/users/me/ — expect 401 or 403. (If it somehow returns 200, STOP and report success=false: the live token was NOT deleted.) Never print the token value.`,
        `3. Remove the single "${args.secretsKey}: ..." line from .github/instructions/secrets.yml. If a short explanatory comment line is adjacent and specific to this key, remove it too; otherwise add a one-line comment noting the key was retired by #175 (on-demand minting now — see Authentik README). Make a backup copy at .github/instructions/secrets.yml.bak-175 first, leave the .bak as-is (also git-ignored).`,
        `4. Verify: grep -c "${args.secretsKey}" .github/instructions/secrets.yml on the active key line returns 0 active occurrences (a retirement comment mentioning #175 is fine).`,
        `5. Confirm git status shows NO tracked change for secrets.yml (it is git-ignored).`,
        'Return ONLY the JSON result object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['success', 'liveTokenStatus', 'secretsYmlUpdated', 'summary'],
      properties: {
        success: { type: 'boolean' },
        liveTokenStatus: { type: 'string' },
        secretsYmlUpdated: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'secrets', 'verify'],
}));

export const finalizeTask = defineTask('finalize-pr-and-followups', (args, taskCtx) => ({
  kind: 'agent',
  title: `Push branch, open PR, follow-ups, update issue #${args.issue}`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Release manager following the Epaflix merge policy',
      task: `Push branch ${args.branch}, open a PR for issue #${args.issue}, open follow-up issues, and update the issue. Do NOT merge.`,
      context: {
        repoRoot: args.repoRoot, branch: args.branch, issue: args.issue,
        issueUrl: args.issueUrl, repo: args.repo,
        docsSummary: args.docsSummary, verifySummary: args.verifySummary,
      },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. git push -u origin ${args.branch} (force-with-lease if it already exists remotely).`,
        `2. gh pr create --repo ${args.repo} --base main --head ${args.branch} with a clear title referencing #${args.issue} and a body that: explains Approach A (revoke & retire, no standing admin token); notes the live token was already expired (403) and consumed by no workload; lists the docs changed; states secrets.yml was cleaned locally (git-ignored); and includes a "## Test plan" / "## Verification" checklist (e.g. "- [ ] kustomize build unaffected (docs-only)", "- [ ] live token returns 401/403", "- [ ] no real token value in diff", "- [ ] secrets.yml has no active ${args.secretsKey} key"). End the PR body with: 🤖 Generated with [Claude Code](https://claude.com/claude-code). Cross-link #134 and #${args.issue}.`,
        `3. Run the docs-only verification now and tick the boxes you can confirm directly by EDITING the PR body (never add a comment): kustomize build for 2-k3s/07.authentik-deployment if a kustomization exists (should be unaffected by .md edits); re-probe the live token (expect 401/403); confirm no real token value in the diff.`,
        `4. Open follow-up gh issues on ${args.repo} for any deferred item using the repo's enhancement shape (## Finding / ## Current state / ## Desired outcome / ## Notes), cross-linking #${args.issue}. Candidate follow-ups (open only those that genuinely apply): (a) the on-demand-minting runbook should eventually be exercised/validated the next time SSO automation is needed; (b) audit secrets.yml for any OTHER standing long-lived tokens that should move to mint-on-demand or gain expiry. If none apply, return an empty followUps array and say so.`,
        `5. Update issue #${args.issue} body or add a brief status — but per repo rules do NOT close it yet (merge step closes it). A short progress comment summarizing the PR is acceptable here.`,
        'Return ONLY the JSON result object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'followUps', 'verification', 'summary'],
      properties: {
        prUrl: { type: 'string' },
        followUps: { type: 'array', items: { type: 'string' } },
        verification: { type: 'object' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'pr', 'github'],
}));

export const mergeTask = defineTask('merge-pr-close-issue', (args, taskCtx) => ({
  kind: 'agent',
  title: `Rebase + merge PR per policy, close issue #${args.issue}`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Release manager following the Epaflix semi-linear merge policy',
      task: `Merge ${args.prUrl} per policy and close issue #${args.issue}.`,
      context: { repoRoot: args.repoRoot, branch: args.branch, issue: args.issue, repo: args.repo, prUrl: args.prUrl },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. Rebase the branch onto latest origin/main: git fetch origin && git checkout ${args.branch} && git rebase origin/main, then git push --force-with-lease. Resolve trivial conflicts if any; docs-only so unlikely.`,
        `2. Wait for the required \`validate\` check to pass: gh pr checks ${args.prUrl} --watch (or poll). Do not merge until it is green and the branch is up to date with main (strict).`,
        `3. Merge: gh pr merge ${args.prUrl} --merge --repo ${args.repo}. (Merge-commit, NOT squash/rebase — Epaflix policy.)`,
        `4. Confirm main advanced with a "Merge pull request #N" marker (git fetch origin && git log origin/main -3 --oneline).`,
        `5. Close issue #${args.issue} if the merge did not auto-close it (the PR body cross-links it). Verify state=closed via gh issue view.`,
        `6. Optionally delete the remote branch.`,
        'Return ONLY the JSON result object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['merged', 'issueClosed', 'summary'],
      properties: {
        merged: { type: 'boolean' },
        issueClosed: { type: 'boolean' },
        mergeCommit: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'merge', 'github'],
}));
