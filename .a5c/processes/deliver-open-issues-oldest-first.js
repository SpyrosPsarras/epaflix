/**
 * @process project/deliver-open-issues-oldest-first
 * @description Continuous delivery loop over the OPEN GitHub issues on SpyrosPsarras/epaflix,
 * processed oldest-first (smallest open issue number first). For each issue it runs a generic,
 * repo-aware delivery pipeline that adapts to the issue's nature:
 *
 *   select-oldest-open  ->  triage/design (read-only, classify deliveryMode)
 *     -> OWNER TRIAGE GATE (proceed / skip / stop / change-approach refine loop)
 *     -> if deliveryMode == 'code-change-pr':
 *          implement+validate refine loop -> adversarial review loop -> finalize (push+PR, Closes #N, test plan, follow-ups)
 *          -> OWNER DEPLOY/MERGE GATE -> rebase+merge (Epaflix semi-linear) -> ArgoCD verify -> close issue
 *     -> else (cluster-op / owner-decision / record-only / wont-fix / already-done):
 *          -> OWNER EXECUTE GATE (deploy + destructive-git) -> execute live op OR record decision/relabel/close + follow-ups + verify
 *   -> record issue as processed -> loop to next oldest.
 *
 * Skips the Renovate "Dependency Dashboard" tracker issue (it is auto-managed and re-opens).
 *
 * Calibrated to the user profile: breakpointTolerance=low, alwaysBreakOn=[destructive-git, deploy].
 * Exactly two human gates per delivered issue (a triage/approach gate so the owner sees every issue
 * one-by-one, and a deploy/merge/execute gate before anything irreversible touches main or the cluster).
 * All other work (analysis, edits on a branch, validation, adversarial review, PR authoring) runs
 * autonomously between the gates.
 *
 * @inputs { repo: string, repoRoot: string, dashboardIssue: number, ruleset: string, maxIssues: number, startFrom: number|null }
 * @outputs { success: boolean, delivered: array, skipped: array, stopped: boolean, results: array }
 *
 * Composition references (process library):
 *  - specializations/devops-sre-platform/iac-implementation.js   (plan -> implement -> validate(kustomize build/helm template/yaml) -> refine loop)
 *  - methodologies/gsd/verify-work                               (quality gate = renders + scope correct + post-merge ArgoCD Synced/Healthy)
 *  - methodologies/gsd/iterative-convergence                     (adopt -> soak; defer destructive follow-ups to gh issues)
 *  - project/deliver-issue-192-image-updater-writeback.js        (same-repo conventions: branch off origin/main, Epaflix semi-linear merge policy, owner gates, PR test-plan ticking, follow-up-issue rule)
 *
 * @skill none
 * @agent general-purpose specializations/devops-sre-platform/agents
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

export async function process(inputs, ctx) {
  const {
    repo = 'SpyrosPsarras/epaflix',
    repoRoot = '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
    dashboardIssue = 31,
    ruleset = '16805247',
    maxIssues = 40,
    startFrom = null,
  } = inputs || {};

  const processed = [];   // issue numbers fully handled (delivered/held/skipped/recorded)
  const delivered = [];   // numbers whose work was actually merged/executed/closed
  const skipped = [];     // numbers the owner chose to skip
  const results = [];

  // Interpret an owner breakpoint reply into a control decision.
  const classify = (bp) => {
    const txt = `${bp && bp.approved ? 'approve ' : ''}${(bp && (bp.response || bp.feedback)) || ''}`.toLowerCase();
    if (bp && bp.approved) return 'proceed';
    if (/\bstop\b|\babort\b|\bend run\b|\bhalt\b/.test(txt)) return 'stop';
    if (/\bskip\b|\bnext\b|\bpass\b|\bleave it\b/.test(txt)) return 'skip';
    return 'change'; // a plain rejection with feedback -> refine
  };

  for (let i = 0; i < maxIssues; i++) {
    // ── SELECT next oldest open issue not yet processed ─────────────────────
    const sel = await ctx.task(selectIssueTask, {
      repo, repoRoot, dashboardIssue, processed, startFrom, iteration: i + 1,
    });
    if (sel.done || !sel.issue) {
      ctx.log('info', `No more deliverable open issues. Processed ${processed.length}.`);
      break;
    }
    const issue = sel.issue;
    ctx.log('info', `[#${issue.number}] ${issue.title} — triaging (oldest open, iteration ${i + 1}).`);

    // ── TRIAGE / DESIGN (read-only) with an owner gate + change-refine loop ──
    let triage = await ctx.task(triageTask, { repo, repoRoot, issue, dashboardIssue, ruleset });
    let triageFeedback = null;
    let ownerDirection = null;
    let decision = null;
    for (let attempt = 0; attempt < 6; attempt++) {
      if (triageFeedback) {
        triage = await ctx.task(triageTask, {
          repo, repoRoot, issue, dashboardIssue, ruleset,
          feedback: triageFeedback, attempt: attempt + 1,
        });
      }
      const bp = await ctx.breakpoint({
        question:
          `TRIAGE — issue #${issue.number}: ${issue.title}\n\n` +
          `Recommended delivery mode: ${triage.deliveryMode}\n` +
          `Risk class: ${triage.riskClass}\n` +
          `Branch (if code change): ${triage.branch || '(n/a)'}\n` +
          `Files to change: ${(triage.filesToChange || []).join(', ') || '(none / not a code change)'}\n` +
          `Live/cluster actions: ${(triage.clusterActions || []).join('; ') || '(none)'}\n` +
          `Follow-up candidates: ${(triage.followUpCandidates || []).join(' | ') || '(none)'}\n\n` +
          `Plan summary:\n${triage.summary}\n\n` +
          (triage.openQuestions && triage.openQuestions.length
            ? `Open questions for you:\n- ${triage.openQuestions.join('\n- ')}\n\n` : '') +
          `Reply to PROCEED (approve), or say "skip" to move to the next issue, "stop" to end the run, ` +
          `or give feedback / your decision to change the approach (I'll re-triage). ` +
          `For owner-decision issues, your answer here is the decision I will act on.`,
        title: `Triage gate — #${issue.number}`,
        options: ['Proceed', 'Skip this issue', 'Stop the run', 'Change approach / give decision'],
        context: {
          runId: ctx.runId, issue: issue.number, title: issue.title,
          deliveryMode: triage.deliveryMode, riskClass: triage.riskClass,
          plan: triage.plan, filesToChange: triage.filesToChange,
          clusterActions: triage.clusterActions, followUpCandidates: triage.followUpCandidates,
          openQuestions: triage.openQuestions,
          artifact: { path: `artifacts/triage-issue-${issue.number}.md`, format: 'markdown' },
        },
        expert: 'owner',
        tags: ['approval-gate', 'triage'],
        previousFeedback: triageFeedback || undefined,
        attempt: attempt > 0 ? attempt + 1 : undefined,
      });
      decision = classify(bp);
      ownerDirection = bp.response || bp.feedback || null;
      if (decision === 'proceed' || decision === 'skip' || decision === 'stop') break;
      triageFeedback = bp.response || bp.feedback || 'Change the approach.';
      ctx.log('warn', `[#${issue.number}] triage change requested: ${triageFeedback}`);
    }

    if (decision === 'stop') {
      ctx.log('info', `Owner stopped the run at #${issue.number}.`);
      results.push({ issue: issue.number, outcome: 'stopped-before-work' });
      return { success: true, delivered, skipped, stopped: true, processedCount: processed.length, results };
    }
    if (decision === 'skip' || decision === 'change') {
      // 'change' fell out of the loop only if retries exhausted -> treat as skip to avoid looping forever.
      processed.push(issue.number);
      skipped.push(issue.number);
      results.push({ issue: issue.number, outcome: 'skipped', reason: ownerDirection || 'owner skipped / retries exhausted' });
      ctx.log('info', `[#${issue.number}] skipped.`);
      continue;
    }

    // ── DELIVER ─────────────────────────────────────────────────────────────
    if (triage.deliveryMode === 'code-change-pr') {
      // IMPLEMENT + VALIDATE refine loop
      let impl, validation = { pass: false }, implFeedback = null;
      for (let attempt = 0; attempt < 4; attempt++) {
        impl = await ctx.task(implementTask, {
          repo, repoRoot, issue, branch: triage.branch, plan: triage.plan,
          filesToChange: triage.filesToChange, ownerDirection,
          feedback: implFeedback || undefined, attempt: attempt + 1,
        });
        validation = await ctx.task(validateTask, {
          repo, repoRoot, issue, branch: triage.branch, filesChanged: impl.filesChanged,
        });
        if (validation.pass) break;
        implFeedback = (validation.issues || []).join('; ') || 'Validation failed.';
        ctx.log('warn', `[#${issue.number}] validation failed (attempt ${attempt + 1}): ${implFeedback}`);
      }
      if (!validation.pass) {
        processed.push(issue.number);
        results.push({ issue: issue.number, outcome: 'validation-failed', branch: triage.branch });
        ctx.log('error', `[#${issue.number}] left branch ${triage.branch} for inspection (validation failed).`);
        continue;
      }

      // ADVERSARIAL REVIEW refine loop
      let review = { pass: false }, reviewFeedback = null;
      for (let attempt = 0; attempt < 3; attempt++) {
        if (reviewFeedback) {
          impl = await ctx.task(implementTask, {
            repo, repoRoot, issue, branch: triage.branch, plan: triage.plan,
            filesToChange: triage.filesToChange, ownerDirection,
            feedback: reviewFeedback, attempt: attempt + 1,
          });
          const reval = await ctx.task(validateTask, {
            repo, repoRoot, issue, branch: triage.branch, filesChanged: impl.filesChanged,
          });
          if (!reval.pass) { reviewFeedback = (reval.issues || []).join('; ') || 'Re-validation failed'; continue; }
        }
        review = await ctx.task(reviewTask, {
          repo, repoRoot, issue, branch: triage.branch, filesChanged: impl.filesChanged, plan: triage.plan,
        });
        if (review.pass) break;
        reviewFeedback = (review.issues || []).join('; ') || 'Review failed.';
        ctx.log('warn', `[#${issue.number}] review failed (attempt ${attempt + 1}): ${reviewFeedback}`);
      }
      if (!review.pass) {
        processed.push(issue.number);
        results.push({ issue: issue.number, outcome: 'review-failed', branch: triage.branch });
        continue;
      }

      // FINALIZE — push + PR (no merge)
      const finalize = await ctx.task(finalizeTask, {
        repo, repoRoot, issue, branch: triage.branch, dashboardIssue,
        implSummary: impl.summary, reviewSummary: review.summary,
        followUpCandidates: triage.followUpCandidates,
      });

      // DEPLOY / MERGE GATE (owner; alwaysBreakOn deploy + destructive-git)
      const dbp = await ctx.breakpoint({
        question:
          `DEPLOY + MERGE — issue #${issue.number}\nPR: ${finalize.prUrl}\n\n` +
          `On approval I will rebase onto origin/main, wait for the required \`validate\` check, ` +
          `\`gh pr merge --merge\` (Epaflix semi-linear policy), then confirm ArgoCD apps stay Synced/Healthy ` +
          `and the issue closes.\n\n` +
          `Merges to main (GitOps deploy). Reply to approve, "hold" to leave the PR open and move on, or "stop".`,
        title: `Deploy/merge gate — #${issue.number}`,
        options: ['Merge now', 'Hold (leave PR open)', 'Stop the run'],
        context: {
          runId: ctx.runId, issue: issue.number, prUrl: finalize.prUrl,
          followUps: finalize.followUps, testPlan: finalize.verification,
          liveEffect: 'merge to main; ArgoCD reconciles the change',
        },
        expert: 'owner',
        tags: ['approval-gate', 'deploy', 'destructive-git'],
      });
      const ddec = classify(dbp);
      if (ddec === 'stop') {
        results.push({ issue: issue.number, outcome: 'pr-open-stopped', prUrl: finalize.prUrl, followUps: finalize.followUps });
        return { success: true, delivered, skipped, stopped: true, processedCount: processed.length, results };
      }
      if (ddec !== 'proceed') {
        processed.push(issue.number);
        results.push({ issue: issue.number, outcome: 'pr-held-open', prUrl: finalize.prUrl, followUps: finalize.followUps });
        ctx.log('info', `[#${issue.number}] PR left open per owner.`);
        continue;
      }

      const deploy = await ctx.task(deployVerifyTask, {
        repo, repoRoot, issue, branch: triage.branch, prUrl: finalize.prUrl,
      });
      processed.push(issue.number);
      if (deploy.merged) delivered.push(issue.number);
      results.push({
        issue: issue.number, outcome: deploy.merged ? 'merged' : 'merge-incomplete',
        prUrl: finalize.prUrl, merged: deploy.merged, verified: deploy.verified,
        issuesClosed: deploy.issuesClosed, followUps: finalize.followUps,
      });
      ctx.log('info', `[#${issue.number}] merged=${deploy.merged} verified=${deploy.verified}.`);
      continue;
    }

    // ── NON-CODE delivery: cluster-op / owner-decision / record / wont-fix / already-done ──
    const needsLiveGate = triage.deliveryMode === 'cluster-op'
      || /destructive|deploy|live/.test(String(triage.riskClass || '').toLowerCase());
    let execApproved = true;
    if (needsLiveGate) {
      const ebp = await ctx.breakpoint({
        question:
          `EXECUTE — issue #${issue.number} (${triage.deliveryMode})\n\n` +
          `Live/cluster actions I will run on approval:\n- ${(triage.clusterActions || ['(see plan)']).join('\n- ')}\n\n` +
          `Plan:\n${triage.summary}\n\n` +
          `These touch the live cluster/infra. Reply to approve, "skip" to abandon, or "stop".`,
        title: `Execute gate — #${issue.number}`,
        options: ['Execute now', 'Skip this issue', 'Stop the run'],
        context: {
          runId: ctx.runId, issue: issue.number, deliveryMode: triage.deliveryMode,
          clusterActions: triage.clusterActions, riskClass: triage.riskClass,
          liveEffect: 'live cluster / infrastructure operation',
        },
        expert: 'owner',
        tags: ['approval-gate', 'deploy', 'destructive-git'],
      });
      const edec = classify(ebp);
      if (edec === 'stop') {
        return { success: true, delivered, skipped, stopped: true, processedCount: processed.length, results };
      }
      if (edec !== 'proceed') {
        processed.push(issue.number); skipped.push(issue.number);
        results.push({ issue: issue.number, outcome: 'skipped-at-execute-gate' });
        continue;
      }
      ownerDirection = ebp.response || ownerDirection;
    }

    const exec = await ctx.task(executeNonCodeTask, {
      repo, repoRoot, issue, deliveryMode: triage.deliveryMode,
      plan: triage.plan, clusterActions: triage.clusterActions,
      followUpCandidates: triage.followUpCandidates, ownerDirection, dashboardIssue,
    });
    processed.push(issue.number);
    if (exec.closed) delivered.push(issue.number);
    results.push({
      issue: issue.number, outcome: exec.outcome || 'executed',
      closed: exec.closed, followUps: exec.followUps, summary: exec.summary,
    });
    ctx.log('info', `[#${issue.number}] non-code delivery outcome=${exec.outcome} closed=${exec.closed}.`);
  }

  return {
    success: true,
    delivered,
    skipped,
    stopped: false,
    processedCount: processed.length,
    results,
    metadata: { processId: 'project/deliver-open-issues-oldest-first', timestamp: ctx.now() },
  };
}

// ════════════════════════════════════════════════════════════════════════════
// TASKS
// ════════════════════════════════════════════════════════════════════════════

export const selectIssueTask = defineTask('select-oldest-open-issue', (args, taskCtx) => ({
  kind: 'agent',
  title: `Select oldest open issue (iteration ${args.iteration})`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Release coordinator triaging a GitHub backlog',
      task: 'Pick the OLDEST open issue (smallest number) that has NOT yet been processed, and return its full content. READ-ONLY.',
      context: {
        repo: args.repo, repoRoot: args.repoRoot, dashboardIssue: args.dashboardIssue,
        processed: args.processed, startFrom: args.startFrom,
      },
      instructions: [
        `cd ${args.repoRoot}.`,
        `List open issues oldest-first: gh issue list --repo ${args.repo} --state open --limit 100 --json number,title,body,labels --jq 'sort_by(.number)'.`,
        `EXCLUDE the Renovate "Dependency Dashboard" tracker issue #${args.dashboardIssue} (auto-managed, re-opens — never deliver it).`,
        `EXCLUDE any number already in this processed list: ${JSON.stringify(args.processed)}.`,
        args.startFrom ? `Only consider issues with number >= ${args.startFrom}.` : `No lower bound on issue number.`,
        `Choose the smallest remaining open issue number. Return its number, title, full body, and label names.`,
        `If there are NO remaining deliverable open issues, return { "done": true, "issue": null }.`,
        'Return ONLY the JSON result object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['done'],
      properties: {
        done: { type: 'boolean' },
        issue: {
          type: ['object', 'null'],
          properties: {
            number: { type: 'number' },
            title: { type: 'string' },
            body: { type: 'string' },
            labels: { type: 'array', items: { type: 'string' } },
          },
        },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'select', 'github'],
}));

export const triageTask = defineTask('triage-issue', (args, taskCtx) => ({
  kind: 'agent',
  title: `Triage + design #${args.issue.number}${args.attempt ? ` (refine ${args.attempt})` : ''}`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Senior DevOps/GitOps engineer in a Kustomize+Helm+ArgoCD IaC repo (K3s + Docker Swarm on Proxmox)',
      task: `Triage and design the delivery for issue #${args.issue.number}. READ-ONLY — make NO edits, NO commits, NO cluster changes.`,
      context: {
        repoRoot: args.repoRoot, repo: args.repo, dashboardIssue: args.dashboardIssue,
        ruleset: args.ruleset, issue: args.issue, feedback: args.feedback || null,
      },
      instructions: [
        `cd ${args.repoRoot}.`,
        `Read CLAUDE.md first (cluster inventory, DNS/SSH/storage, Critical Rules: never commit secrets, *.enc.yaml SOPS, open a gh issue for every follow-up, Epaflix merge policy, execute PR test plans).`,
        `Read the issue fully: gh issue view ${args.issue.number} --repo ${args.repo} --comments. Re-read the title/body provided in context.`,
        `Investigate the repo to ground the fix: find the exact files, manifests, scripts, ArgoCD Applications, kustomizations, docs, and any .a5c memory notes relevant to this issue. Use rg/grep/ls. Determine the current live-vs-git state if the issue implies drift.`,
        `Classify deliveryMode as exactly ONE of:`,
        `  - "code-change-pr": the fix is edits to files in this repo (manifests/scripts/docs/CI) delivered via a branch + PR + merge (the normal GitOps path).`,
        `  - "cluster-op": the fix requires a live action on the cluster/infra (ArgoCD selfHeal/prune flip via kubectl/argocd, a TrueNAS/Proxmox/SSH operation, an app API change, deleting a dataset, etc.) with little or no repo edit. May also include a small companion doc/manifest PR.`,
        `  - "owner-decision": the issue asks the owner to DECIDE something (e.g. re-add vs remove an orphan, pick a CPU type, choose an approach) before any work can happen. The owner's reply at the triage gate IS the decision.`,
        `  - "record-only": no change needed in repo or cluster; the right outcome is to comment + close/relabel (e.g. duplicate, superseded, captured elsewhere).`,
        `  - "already-done": verify against live/git that the work is already complete; outcome is to comment with evidence + close.`,
        `  - "wont-fix": recommend closing as not-planned with a rationale (e.g. accepted risk).`,
        `Assign riskClass: one of "safe-code-change", "live-cluster-op", "destructive", "decision-only". Anything that merges to main is at least a GitOps deploy; destructive = deletes data / irreversible.`,
        `If deliveryMode is code-change-pr: propose a branch name "issue-${args.issue.number}-<short-slug>" and a concrete filesToChange list with the exact edit per file. Honor: never hardcode secrets (use placeholders + secrets.yml), encrypted Secrets are *.enc.yaml via SOPS+age, do not edit unrelated files.`,
        `If deliveryMode is cluster-op: list the exact clusterActions (commands) you will run, in order, including how you will VERIFY success and roll back. Note any SSH host (CLAUDE.md inventory) and whether ArgoCD selfHeal will fight the change (must be codified in git too).`,
        `Identify follow-up candidates (the repo rule: open a gh issue for every deferred/next-step item — soak flips, decommissions, scope cuts) as followUpCandidates, cross-linking #${args.issue.number}.`,
        `List any openQuestions the owner must answer before you can deliver (especially for owner-decision issues). Keep them crisp.`,
        `If "feedback" is present, it is the owner's direction/decision from the triage gate — revise the plan to honor it exactly.`,
        `Write the triage to artifacts/triage-issue-${args.issue.number}.md and produce a concise human summary + a machine-usable plan object.`,
        'Return ONLY the JSON result object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['deliveryMode', 'riskClass', 'summary', 'plan', 'filesToChange', 'clusterActions', 'followUpCandidates', 'openQuestions'],
      properties: {
        deliveryMode: { type: 'string', enum: ['code-change-pr', 'cluster-op', 'owner-decision', 'record-only', 'already-done', 'wont-fix'] },
        riskClass: { type: 'string' },
        branch: { type: ['string', 'null'] },
        summary: { type: 'string' },
        plan: { type: 'object' },
        filesToChange: { type: 'array', items: { type: 'string' } },
        clusterActions: { type: 'array', items: { type: 'string' } },
        followUpCandidates: { type: 'array', items: { type: 'string' } },
        openQuestions: { type: 'array', items: { type: 'string' } },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'triage', 'design', 'gitops'],
}));

export const implementTask = defineTask('implement-issue', (args, taskCtx) => ({
  kind: 'agent',
  title: `Implement #${args.issue.number} on branch (attempt ${args.attempt})`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'DevOps engineer editing a Kustomize+Helm+ArgoCD GitOps repo',
      task: `Implement the approved plan for issue #${args.issue.number} on a fresh branch. Do NOT push.`,
      context: {
        repoRoot: args.repoRoot, repo: args.repo, issue: args.issue, branch: args.branch,
        plan: args.plan, filesToChange: args.filesToChange, ownerDirection: args.ownerDirection || null,
        feedback: args.feedback || null,
      },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. Branch: git fetch origin && git checkout -B ${args.branch} origin/main. If "feedback" is provided you are REFINING the existing ${args.branch} — incorporate the feedback; do not start over from scratch unnecessarily.`,
        `2. Make ONLY the edits in the approved plan / filesToChange (${JSON.stringify(args.filesToChange)}). Follow repo conventions in CLAUDE.md and surrounding files (style, naming, comment density).`,
        `3. NEVER hardcode secrets — use placeholders and reference secrets.yml; any kind: Secret must be a SOPS-encrypted *.enc.yaml (the pre-commit hook refuses plaintext Secrets). Run ./.github/hooks/install-hooks.sh is already done for this clone.`,
        `4. Guard scope: git diff --name-only origin/main must list ONLY the intended files. Remove any stray changes.`,
        `5. Commit (do NOT push). Subject: a clear conventional-commit line referencing (#${args.issue.number}). Body: what + why. End the commit body with: Co-Authored-By: Claude Fable 5 (1M context) <noreply@anthropic.com>`,
        `If "ownerDirection" is present, honor it as the owner's explicit instruction/decision.`,
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
  labels: ['agent', 'implement', 'gitops'],
}));

export const validateTask = defineTask('validate-issue', (args, taskCtx) => ({
  kind: 'agent',
  title: `Quality gate — render/validate #${args.issue.number}`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'CI/quality-gate engineer mirroring the repo\'s `validate` gate',
      task: `Validate branch ${args.branch} for issue #${args.issue.number} with REAL checks. Make NO edits.`,
      context: { repoRoot: args.repoRoot, repo: args.repo, issue: args.issue, branch: args.branch, filesChanged: args.filesChanged },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. Scope: git diff --name-only origin/main must list ONLY the intended files; FAIL on stray changes.`,
        `2. Mirror the CI \`validate\` gate (.github/workflows/ci.yml): for changed Kustomize dirs run \`kustomize build <dir> --enable-helm\` (use the pinned kustomize/helm if present); for changed Helm values run \`helm template\`; for changed plain YAML parse with \`python3 -c "import yaml,sys;list(yaml.safe_load_all(open(f)))"\`; for changed shell scripts run \`bash -n\`; for renovate.json run the renovate-config-validator if reachable else strict JSON parse. FAIL on any render/parse error.`,
        `3. If a kind: Secret was added, confirm it is a SOPS-encrypted *.enc.yaml (no plaintext) — the pre-commit hook would refuse plaintext.`,
        `4. Confirm the commit subject references the issue and the Co-Authored-By footer is present.`,
        `Return pass=true only if every check holds; else pass=false with a concrete issues list.`,
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
  labels: ['agent', 'validate', 'quality-gate'],
}));

export const reviewTask = defineTask('review-issue', (args, taskCtx) => ({
  kind: 'agent',
  title: `Adversarial review #${args.issue.number}`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Adversarial reviewer for a GitOps change',
      task: `Verify the change on ${args.branch} fully and correctly delivers issue #${args.issue.number} with no regressions or scope creep.`,
      context: { repoRoot: args.repoRoot, repo: args.repo, issue: args.issue, branch: args.branch, filesChanged: args.filesChanged, plan: args.plan },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. git diff origin/main...${args.branch}: confirm the diff fully implements the issue's intent and nothing unrelated changed.`,
        `2. Correctness: trace the change against the issue's acceptance criteria. Check ArgoCD implications (will selfHeal accept it; any ignoreDifferences needed; SSA list-merge gotchas).`,
        `3. Safety: no secret leaked; no plaintext Secret; no destructive side effect smuggled in; placeholders used where required.`,
        `4. Conventions: commit subject + Co-Authored-By footer; matches surrounding file style.`,
        `Return pass=true only if all hold; else pass=false with a concrete, actionable issues list.`,
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
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'review', 'quality-gate'],
}));

export const finalizeTask = defineTask('finalize-issue', (args, taskCtx) => ({
  kind: 'agent',
  title: `Push + open PR (closes #${args.issue.number}) + follow-ups`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Release manager following the Epaflix merge policy',
      task: `Push ${args.branch}, open the PR for issue #${args.issue.number}, open follow-ups, post a progress note. Do NOT merge.`,
      context: {
        repoRoot: args.repoRoot, repo: args.repo, issue: args.issue, branch: args.branch,
        dashboardIssue: args.dashboardIssue, implSummary: args.implSummary,
        reviewSummary: args.reviewSummary, followUpCandidates: args.followUpCandidates,
      },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. git push -u origin ${args.branch} (force-with-lease if it already exists remotely).`,
        `2. gh pr create --repo ${args.repo} --base main --head ${args.branch}. Title: a clear conventional-commit line ending with (#${args.issue.number}). Body: problem, chosen approach, changes, and "Closes #${args.issue.number}". Include a "## Test plan" checklist with the static (pre-merge) checks AND the (post-merge) ArgoCD Synced/Healthy check AND any (soak) item. End the body with: 🤖 Generated with [Claude Code](https://claude.com/claude-code)`,
        `3. Run the PRE-merge test-plan checks NOW and tick those boxes by EDITING the PR body (never add a comment). Leave (post-merge)/(soak) boxes unchecked.`,
        `4. Open follow-up gh issues (enhancement shape: ## Finding / ## Current state / ## Desired outcome / ## Notes), cross-linking #${args.issue.number}, only for items that genuinely apply from followUpCandidates: ${JSON.stringify(args.followUpCandidates || [])}.`,
        `5. Post a brief progress note on #${args.issue.number} linking the PR (do NOT close — the merge closes it via "Closes").`,
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

export const deployVerifyTask = defineTask('deploy-verify-issue', (args, taskCtx) => ({
  kind: 'agent',
  title: `Rebase+merge per policy, verify ArgoCD, close #${args.issue.number}`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Release manager + cluster operator following the Epaflix semi-linear merge policy',
      task: `Merge ${args.prUrl}, confirm ArgoCD reconciles cleanly, close #${args.issue.number}.`,
      context: { repoRoot: args.repoRoot, repo: args.repo, issue: args.issue, branch: args.branch, prUrl: args.prUrl },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. Rebase onto origin/main: git fetch origin && git checkout ${args.branch} && git rebase origin/main ; resolve conflicts ; git push --force-with-lease.`,
        `2. Wait for the required \`validate\` check green AND branch up-to-date (strict): gh pr checks ${args.prUrl} --watch.`,
        `3. Merge: gh pr merge ${args.prUrl} --merge --repo ${args.repo} (merge-commit per Epaflix policy). Confirm a "Merge pull request #N" marker on main.`,
        `4. ArgoCD: confirm affected Applications reconcile the new commit and stay Synced + Healthy (kubectl -n argocd get applications, or argocd CLI). Report any drift/degradation. If the cluster is unreachable from here, say so and mark verified based on merge + manifest correctness, noting the live check is pending.`,
        `5. Tick the (post-merge) test-plan boxes on the PR by EDITING the PR body (never a comment) with what you observed.`,
        `6. Confirm #${args.issue.number} is closed (the "Closes" line should auto-close on merge); if not, close it with a brief evidence note.`,
        `7. Optionally delete the remote branch.`,
        'Return ONLY the JSON result object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['merged', 'verified', 'issuesClosed', 'summary'],
      properties: {
        merged: { type: 'boolean' },
        verified: { type: 'boolean' },
        issuesClosed: { type: 'array', items: { type: 'number' } },
        mergeCommit: { type: 'string' },
        argocdStatus: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'merge', 'deploy', 'verify', 'github'],
}));

export const executeNonCodeTask = defineTask('execute-noncode-issue', (args, taskCtx) => ({
  kind: 'agent',
  title: `Execute non-code delivery #${args.issue.number} (${args.deliveryMode})`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'DevOps/cluster operator + release manager for the Epaflix infra repo',
      task: `Deliver issue #${args.issue.number} via deliveryMode="${args.deliveryMode}". Execute fully, verify, and close/relabel the issue. Open follow-ups per the repo rule.`,
      context: {
        repoRoot: args.repoRoot, repo: args.repo, issue: args.issue, deliveryMode: args.deliveryMode,
        plan: args.plan, clusterActions: args.clusterActions, ownerDirection: args.ownerDirection || null,
        followUpCandidates: args.followUpCandidates, dashboardIssue: args.dashboardIssue,
      },
      instructions: [
        `cd ${args.repoRoot}. Read CLAUDE.md for inventory/SSH/conventions.`,
        `Honor the owner's direction/decision verbatim: ${JSON.stringify(args.ownerDirection || '(none given)')}.`,
        `If deliveryMode == "cluster-op": run the approved clusterActions (${JSON.stringify(args.clusterActions || [])}) in order via SSH/kubectl/argocd/midclt as appropriate. VERIFY each step succeeded and the desired end-state holds. If the change could be reverted by ArgoCD selfHeal, ALSO codify it in git (small PR per the normal path) so it is durable — note that in the result. If the cluster/host is unreachable, do NOT fake success: report what is blocked and leave the issue open with a status comment.`,
        `If deliveryMode == "owner-decision": apply the owner's decision (which may itself trigger a cluster-op or a code change — do it), then record it on the issue.`,
        `If deliveryMode == "record-only" or "already-done": post a clear comment with the evidence (links, command output) and close the issue.`,
        `If deliveryMode == "wont-fix": post the rationale and close as not-planned (gh issue close --reason "not planned").`,
        `Open follow-up gh issues (## Finding / ## Current state / ## Desired outcome / ## Notes) cross-linking #${args.issue.number} for any deferred item in ${JSON.stringify(args.followUpCandidates || [])} that genuinely applies.`,
        `Close #${args.issue.number} only when the work is genuinely complete (or it is record-only/already-done/wont-fix). Post a concise closing comment summarizing what was done and how it was verified.`,
        `If you also updated docs or .a5c memory, mention it. Never commit gitignored files (e.g. .history/*.md) with -f.`,
        'Return ONLY the JSON result object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['outcome', 'closed', 'summary'],
      properties: {
        outcome: { type: 'string' },
        closed: { type: 'boolean' },
        followUps: { type: 'array', items: { type: 'string' } },
        prUrl: { type: ['string', 'null'] },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'execute', 'cluster-op', 'github'],
}));
