/**
 * @process issue-130-k3s-upgrade-guardrails
 * @description Deliver GitHub issue #130 — guard against unsupervised K3s minor
 *   jumps from the automated channel:stable system-upgrade Plans. Three concrete
 *   deliverables: (1) a future-upgrade SOP doc, (2) a version-vs-channel policy
 *   DECISION + guardrail, and (3) a PrometheusRule alert that fires on
 *   unsupervised minor jumps (new upgrade.cattle.io Jobs + node kubeletVersion
 *   change), routed to the existing Alertmanager email path.
 *
 *   Composition (process-library references):
 *     - methodologies/evolutionary  — small reversible increments, adopt→soak
 *     - specializations/devops-sre-platform/iac-implementation — analyze→
 *       implement→validate→deploy gating for Kustomize/ArgoCD GitOps
 *     - specializations/devops-sre-platform/monitoring-setup — alert authoring
 *     - methodologies/gsd/verify-work — verify against spec, zero scope creep
 *
 * @inputs { issueNumber: number, issueUrl: string, repo: string, repoRoot: string }
 * @outputs { success: boolean, filesChanged: array, prUrl: string }
 *
 * @skill code-review specializations/devops-sre-platform/skills
 * @agent general-purpose
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

// ---------------------------------------------------------------------------
// Task definitions
// ---------------------------------------------------------------------------

export const analyzePlanTask = defineTask('analyze-and-plan', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Analyze current state and produce delivery plan + version-vs-channel recommendation',
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Senior Kubernetes/GitOps platform engineer (homelab K3s + ArgoCD)',
      task:
        'Read the current state of the K3s system-upgrade stack and the observability/Alertmanager stack, then produce a concise delivery plan for GitHub issue #130 plus a clear, opinionated recommendation for the version-vs-channel policy decision.',
      context: args,
      instructions: [
        'Repo root: ' + args.repoRoot + '. Work read-only in this task — do NOT edit files yet.',
        'Read these files fully:',
        '  - 2-k3s/maintenance/system-upgrade/README.md',
        '  - 2-k3s/maintenance/system-upgrade/plans/system-upgrade-plans.yaml',
        '  - 2-k3s/maintenance/system-upgrade/plans/kustomization.yaml',
        '  - 2-k3s/11.argocd/apps/app-system-upgrade-plans.yaml',
        '  - 2-k3s/11.argocd/apps/app-system-upgrade-controller.yaml',
        '  - 2-k3s/10.observability/alertmanager-config/custom-alerts.yaml',
        '  - 2-k3s/10.observability/kustomization.yaml',
        'Restate issue #130 as exactly three deliverables: (1) future-upgrade SOP doc, (2) version-vs-channel DECISION + guardrail, (3) unsupervised-minor-jump ALERT.',
        'For deliverable (1): identify which SOP pieces the README already covers and which are MISSING per the issue (where channel/version lives, reading `kubectl -n system-upgrade get plans,jobs -o wide`, pre-upgrade etcd-snapshot step, CNPG switchover expectation, rollback notes). Plan to EXTEND the existing README rather than create a parallel doc.',
        'For deliverable (2): present the trade-offs of pinning `version:` (explicit reviewable git bump, blocks unsupervised jumps, but requires manual bumps) vs keeping `channel: stable` + a guardrail (e.g. a non-prod canary, a paused/version-gated Plan, or relying purely on the alert). Make a single clear RECOMMENDATION. The repo goal is GitOps hygiene + reviewable changes and the maintainer wants minor jumps gated, so weigh toward pinning `version:` unless there is a strong reason not to. State exactly what files/fields change under the recommended option.',
        'For deliverable (3): design a PrometheusRule alert group to add to 2-k3s/10.observability/alertmanager-config/custom-alerts.yaml. It must catch unsupervised minor jumps via TWO independent signals: (a) a new/active upgrade.cattle.io Job in the system-upgrade namespace using kube-state-metrics (e.g. kube_job_status_active{namespace="system-upgrade"} or a kube_job_created-based detection), and (b) node Kubernetes-version skew / change via kube_node_info{kubelet_version=...} (e.g. count of distinct kubelet_version > 1, signalling a roll in progress). Route via existing labels (severity/component) so it reaches the email receiver. Verify the metric names against what kube-prometheus-stack/kube-state-metrics actually exposes; note any uncertainty.',
        'Confirm the canonical alert metric names you will use are produced by the cluster\'s kube-state-metrics (kube_job_status_active, kube_job_status_start_time, kube_node_info with kubelet_version label). If unsure, state the safest expression.',
        'Identify validation commands available offline (kustomize build of the sops-free plans dir; the observability dir needs the age key, note if it must be skipped).',
        'Keep scope TIGHT — only the three deliverables, no extra refactors.',
      ],
      outputFormat:
        'JSON: { success, sopGaps:[...], decision:{ recommendation:"pin-version"|"keep-channel-plus-guardrail", rationale, filesAndFields:[...] }, alertDesign:{ groupName, alerts:[{name, expr, for, severity, component, summary}], metricsVerified:bool, notes }, validationPlan:[...], scopeNotes }',
    },
    outputSchema: {
      type: 'object',
      required: ['success', 'decision', 'alertDesign'],
      properties: {
        success: { type: 'boolean' },
        sopGaps: { type: 'array', items: { type: 'string' } },
        decision: { type: 'object' },
        alertDesign: { type: 'object' },
        validationPlan: { type: 'array', items: { type: 'string' } },
        scopeNotes: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['issue-130', 'analyze', 'phase-1'],
}));

export const implementTask = defineTask('implement', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Implement the three deliverables (SOP doc, version-vs-channel change, alert)',
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Senior Kubernetes/GitOps platform engineer implementing reviewed changes',
      task:
        'Apply the approved delivery plan for issue #130 to the working tree. Make the edits idempotently and report exactly what changed.',
      context: args,
      instructions: [
        'Repo root: ' + args.repoRoot + '. Apply the APPROVED decision: ' + JSON.stringify(args.decision || {}) + '.',
        'If feedback from a prior review/breakpoint is present, address it first: ' + (args.feedback || '(none)') + '.',
        'Deliverable (1) SOP: EXTEND 2-k3s/maintenance/system-upgrade/README.md to cover the missing pieces from the analysis (sopGaps): where the channel/version lives in git, how to read `kubectl -n system-upgrade get plans,jobs -o wide`, a pre-upgrade etcd-snapshot step (`k3s etcd-snapshot save` on a master / verify `/healthz/etcd`), the CNPG switchover expectation when a primary node drains (do not force-manage postgres pods; expect automatic failover), and rollback notes. Match the README\'s existing tone/structure. Do not duplicate content already present — add a clearly-headed "Future-Upgrade SOP" section.',
        'Deliverable (2) decision: implement the approved option in 2-k3s/maintenance/system-upgrade/plans/system-upgrade-plans.yaml (and any guardrail file). If pinning version: replace the `channel:` line with a `version:` field on BOTH k3s-server and k3s-agent Plans pinned to the cluster\'s CURRENT stable K3s version (read it from the repo/README or note that the exact tag must be confirmed), and update surrounding comments + the README "Manual Operations" section to reflect that bumps are now an explicit git change. Update the ArgoCD app comment in 2-k3s/11.argocd/apps/app-system-upgrade-plans.yaml that currently says future bumps "auto-roll unsupervised" so it is no longer misleading.',
        'Deliverable (3) alert: add the approved PrometheusRule alert group to 2-k3s/10.observability/alertmanager-config/custom-alerts.yaml, matching the existing file\'s YAML style (groups[].rules[] with alert/expr/for/labels{severity,component}/annotations{summary,description}). Use the existing email routing (severity/component labels) — no Alertmanager config change needed.',
        'Do NOT touch secrets, *.enc.yaml plaintext, or anything outside these deliverables. Keep edits minimal and reviewable.',
        'Return the list of files changed with a one-line summary each, and the literal git diff stat.',
      ],
      outputFormat: 'JSON: { success, filesChanged:[{path, summary}], notes }',
    },
    outputSchema: {
      type: 'object',
      required: ['success', 'filesChanged'],
      properties: {
        success: { type: 'boolean' },
        filesChanged: { type: 'array' },
        notes: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['issue-130', 'implement', 'phase-2'],
}));

export const validateTask = defineTask('validate', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Offline validation: kustomize build (plans) + YAML lint of changed manifests',
  description: 'Render the sops-free plans overlay and syntactically validate the changed YAML.',
  shell: {
    command:
      'cd ' + args.repoRoot + ' && ' +
      'echo "== kustomize build plans ==" && kustomize build 2-k3s/maintenance/system-upgrade/plans >/dev/null && echo "plans-OK" ; ' +
      'echo "== yaml parse custom-alerts ==" && python3 -c "import yaml,sys; list(yaml.safe_load_all(open(\'2-k3s/10.observability/alertmanager-config/custom-alerts.yaml\'))); print(\'alerts-yaml-OK\')" ; ' +
      'echo "== yaml parse plans ==" && python3 -c "import yaml,sys; list(yaml.safe_load_all(open(\'2-k3s/maintenance/system-upgrade/plans/system-upgrade-plans.yaml\'))); print(\'plans-yaml-OK\')" ; ' +
      'echo "== git diff --stat ==" && git --no-pager diff --stat',
  },
  io: {
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['issue-130', 'validate', 'phase-3'],
}));

export const verifyTask = defineTask('verify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify changes against issue #130 (all 3 deliverables, no scope creep, alert PromQL sane)',
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Adversarial GitOps reviewer verifying delivery against the spec',
      task:
        'Review the working-tree diff and confirm it fully and accurately delivers issue #130 — all three outcomes — with no extra scope and a sound alert.',
      context: args,
      instructions: [
        'Repo root: ' + args.repoRoot + '. Run `git --no-pager diff` and read every changed file.',
        'Check deliverable (1): the README now documents the future-upgrade SOP including where channel/version lives, reading plans,jobs, pre-upgrade etcd-snapshot, CNPG switchover expectation, and rollback notes.',
        'Check deliverable (2): the approved version-vs-channel decision is actually implemented in the Plans, comments are consistent (no leftover "auto-roll unsupervised" claims if version is now pinned), and both k3s-server and k3s-agent are handled.',
        'Check deliverable (3): the PrometheusRule alert group exists, parses, uses metric names kube-state-metrics actually exposes (kube_job_status_active / kube_job_status_start_time / kube_node_info{kubelet_version}), has sane `for:` windows and severity/component labels that route to email, and genuinely catches a minor jump (job-appears AND/OR version-skew). Flag any PromQL that would never fire or always fire.',
        'Confirm NO out-of-scope edits, NO secrets, NO plaintext kind: Secret.',
        'Score 0-100. Pass requires >=90 AND all three deliverables present AND no scope creep. List concrete required fixes if failing.',
      ],
      outputFormat: 'JSON: { score, pass, deliverablesPresent:{sop, decision, alert}, scopeCreep:bool, requiredFixes:[...], summary }',
    },
    outputSchema: {
      type: 'object',
      required: ['score', 'pass'],
      properties: {
        score: { type: 'number' },
        pass: { type: 'boolean' },
        deliverablesPresent: { type: 'object' },
        scopeCreep: { type: 'boolean' },
        requiredFixes: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['issue-130', 'verify', 'phase-4'],
}));

export const integrateTask = defineTask('integrate', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Branch + commit (path-scoped) + push + open PR per Epaflix merge policy',
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Release engineer following the Epaflix merge-commit + mandatory-rebase PR policy',
      task:
        'Create a feature branch from origin/main, commit ONLY the issue-130 deliverable files, push, and open a PR on SpyrosPsarras/epaflix that closes #130.',
      context: args,
      instructions: [
        'Repo root: ' + args.repoRoot + '. The maintainer has APPROVED integration at a breakpoint.',
        'Fetch origin. Create branch `issue-130-k3s-upgrade-guardrails` based on origin/main (do NOT branch off the current cnpg-operator-bump-129 branch). Use `git stash`/worktree care: only the issue-130 files should move — do not clobber unrelated working-tree changes (e.g. .gitignore). If the deliverable edits are on the current branch, cherry-pick/checkout just those paths onto the new branch.',
        'Stage ONLY these paths (path-scoped, never `git add -A`): 2-k3s/maintenance/system-upgrade/README.md, 2-k3s/maintenance/system-upgrade/plans/system-upgrade-plans.yaml, 2-k3s/11.argocd/apps/app-system-upgrade-plans.yaml, 2-k3s/10.observability/alertmanager-config/custom-alerts.yaml, and any new guardrail file created for the decision. Never add .a5c/ scaffolding or .history/.',
        'Commit message: a conventional-commit summary like `feat(system-upgrade): guard against unsupervised K3s minor jumps (#130)` with a body summarising the three deliverables. End the commit message with the required trailer: Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>.',
        'Rebase the branch onto origin/main and push with --force-with-lease (the strict `validate` check blocks stale branches).',
        'Open the PR with `gh pr create --repo SpyrosPsarras/epaflix --base main`, body referencing "Closes #130", summarising the decision taken, and including a Test Plan checklist (kustomize build plans OK, custom-alerts YAML parses, alert PromQL reviewed). End the PR body with the required generated-with line.',
        'Return the PR URL and the branch name. Do NOT merge yet.',
      ],
      outputFormat: 'JSON: { success, branch, prUrl, commitSha }',
    },
    outputSchema: {
      type: 'object',
      required: ['success', 'prUrl'],
      properties: {
        success: { type: 'boolean' },
        branch: { type: 'string' },
        prUrl: { type: 'string' },
        commitSha: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['issue-130', 'integrate', 'phase-5'],
}));

export const followupsTask = defineTask('followups', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Open gh follow-up issues for any deferred items + execute PR test plan',
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Maintainer closing out the delivery loop',
      task:
        'Run the PR test-plan boxes, record outcomes by EDITING the PR body (never a new comment), and open gh follow-up issues for anything deferred.',
      context: args,
      instructions: [
        'Repo root: ' + args.repoRoot + '.',
        'Execute every unchecked Test Plan box in PR ' + (args.prUrl || '(the issue-130 PR)') + ' (e.g. `kustomize build` the plans dir, parse custom-alerts.yaml). Tick the boxes by editing the PR body via `gh pr edit`, never by adding a comment.',
        'If the alert could not be rendered/validated against live kube-state-metrics offline, OR if the version pin needs the exact current stable tag confirmed post-merge, OR any "soak then flip" follow-up is implied, open a gh issue on SpyrosPsarras/epaflix using the repo\'s enhancement shape (## Finding / ## Current state / ## Desired outcome / ## Notes) and cross-link #130.',
        'If there are genuinely no deferred items, say so explicitly — do not invent issues.',
        'Return the list of follow-up issue URLs (possibly empty) and the test-plan outcome.',
      ],
      outputFormat: 'JSON: { success, testPlanOutcome, followupIssues:[...] }',
    },
    outputSchema: {
      type: 'object',
      required: ['success'],
      properties: {
        success: { type: 'boolean' },
        testPlanOutcome: { type: 'string' },
        followupIssues: { type: 'array', items: { type: 'string' } },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['issue-130', 'followups', 'phase-6'],
}));

// ---------------------------------------------------------------------------
// Orchestration
// ---------------------------------------------------------------------------

export async function process(inputs, ctx) {
  const repoRoot = inputs.repoRoot || '/home/spy/Documents/Epaflix/k3s-swarm-proxmox';
  const issueNumber = inputs.issueNumber || 130;
  const repo = inputs.repo || 'SpyrosPsarras/epaflix';

  ctx.log('info', `Delivering issue #${issueNumber} — K3s unsupervised-upgrade guardrails`);

  // Phase 1: Analyze + plan + version-vs-channel recommendation
  const analysis = await ctx.task(analyzePlanTask, { issueNumber, repo, repoRoot });

  // Decision gate — the version-vs-channel policy choice is the critical
  // architecture decision in this issue; low breakpoint tolerance still breaks
  // here. Retry/refine loop on rejection.
  let decision = analysis.decision;
  let decisionFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    const approval = await ctx.breakpoint({
      question:
        `Issue #130 plan ready. Recommended policy: ${decision && decision.recommendation}. ` +
        `Alert: ${analysis.alertDesign && analysis.alertDesign.groupName}. ` +
        `Approve this plan + decision, or request changes?`,
      title: 'Issue #130 — delivery plan + version-vs-channel decision',
      context: {
        runId: ctx.runId,
        sopGaps: analysis.sopGaps,
        decision,
        alertDesign: analysis.alertDesign,
        scopeNotes: analysis.scopeNotes,
      },
      options: ['Approve', 'Request changes'],
      expert: 'owner',
      tags: ['approval-gate', 'architecture-decision'],
      previousFeedback: decisionFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    if (approval.approved) break;
    decisionFeedback = approval.response || approval.feedback || 'Changes requested';
    const replan = await ctx.task(analyzePlanTask, {
      issueNumber, repo, repoRoot, feedback: decisionFeedback,
    });
    decision = replan.decision;
  }

  // Phase 2-4: Implement → validate → verify, with a refinement loop.
  let verifyResult = null;
  let implFeedback = decisionFeedback;
  for (let attempt = 0; attempt < 3; attempt++) {
    const impl = await ctx.task(implementTask, {
      repoRoot, decision, feedback: implFeedback,
      sopGaps: analysis.sopGaps, alertDesign: analysis.alertDesign,
    });
    await ctx.task(validateTask, { repoRoot });
    verifyResult = await ctx.task(verifyTask, { repoRoot, decision });
    if (verifyResult.pass) break;
    implFeedback = (verifyResult.requiredFixes || []).join('; ') || verifyResult.summary || 'Address review findings';
    ctx.log('warn', `Verify failed (score ${verifyResult.score}); refining: ${implFeedback}`);
  }

  // Deploy gate — branch/commit/push/PR is destructive-git + deploy; always break.
  let integration = null;
  let integrateFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    const approval = await ctx.breakpoint({
      question:
        `Changes verified (score ${verifyResult && verifyResult.score}). ` +
        `Create branch issue-130-k3s-upgrade-guardrails from origin/main, commit the deliverables, push, and open a PR closing #130?`,
      title: 'Issue #130 — integration (branch + commit + push + PR)',
      context: { runId: ctx.runId, verify: verifyResult },
      options: ['Approve', 'Request changes'],
      expert: 'owner',
      tags: ['approval-gate', 'deploy', 'destructive-git'],
      previousFeedback: integrateFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    if (!approval.approved) {
      integrateFeedback = approval.response || approval.feedback || 'Integration not approved';
      // If owner rejects integration, stop here without failing the run.
      return {
        success: true,
        integrated: false,
        reason: integrateFeedback,
        verify: verifyResult,
      };
    }
    integration = await ctx.task(integrateTask, { repoRoot, repo, decision });
    break;
  }

  // Phase 6: follow-ups + PR test plan.
  const followups = await ctx.task(followupsTask, {
    repoRoot, repo, prUrl: integration && integration.prUrl,
  });

  return {
    success: true,
    integrated: true,
    prUrl: integration && integration.prUrl,
    branch: integration && integration.branch,
    verify: verifyResult,
    followups,
  };
}
