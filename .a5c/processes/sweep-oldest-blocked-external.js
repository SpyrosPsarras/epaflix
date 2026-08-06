#!/usr/bin/env node
/**
 * @process project/sweep-oldest-blocked-external
 * @description Work the oldest open non-AirVPN issues in number order. Re-check
 * each issue's external gate against the real upstream source, then close the
 * ones whose gate has genuinely cleared or which are superseded, and leave the
 * rest open with fresh evidence instead of a stale claim.
 *
 * Composition references:
 * - project/deliver-open-issues-oldest-first.js
 * - methodologies/superpowers/verification-before-completion.js
 * - specializations/devops-sre-platform/iac-implementation.js
 *
 * @skill gitops specializations/devops-sre-platform/skills/gitops/SKILL.md
 * @agent platform-engineer specializations/devops-sre-platform/agents/platform-engineer/AGENT.md
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

export async function process(inputs, ctx) {
  const {
    repo = 'SpyrosPsarras/epaflix',
    repoRoot = '/home/spy/Documents/Epaflix/k3s-swarm-proxmox-triage',
    issues = [218, 230, 260, 270, 290],
    offLimits = [723, 792, 800],
  } = inputs || {};

  const facts = await ctx.task(gatherGateFactsTask, { repo, repoRoot, issues, offLimits });

  const decision = await ctx.breakpoint({
    title: `Decide the oldest ${issues.length} non-AirVPN issues`,
    question:
      'Fresh upstream and repo evidence for each issue is below. Reply with which issues to close and which to leave open. ' +
      'Any issue you do not name stays open and untouched.\n\n' +
      facts.stdout,
    options: ['Close the ones I name', 'Leave all open', 'Stop'],
    context: {
      runId: ctx.runId,
      issues,
      evidence: facts.stdout,
      rule: 'Closing text is owner-approved external text; nothing is posted without this reply.',
    },
    expert: 'owner',
    tags: ['approval-gate', 'external-text'],
  });

  if (!decision.approved) {
    return {
      success: false,
      stopped: true,
      reason: decision.response || decision.feedback || 'No closures approved',
      facts,
    };
  }

  const applied = await ctx.task(applyDecisionsTask, {
    repo, repoRoot, issues, offLimits,
    ownerDecision: decision.response || decision.feedback || '',
    evidence: facts.stdout,
  });

  const verified = await ctx.task(verifySweepTask, {
    repo, repoRoot,
    closedIssues: applied.closedIssues || [],
    keptOpen: applied.keptOpen || [],
  });

  return {
    success: verified.exitCode === 0,
    closedIssues: applied.closedIssues,
    keptOpen: applied.keptOpen,
    followUps: applied.followUps,
    verified,
    metadata: { processId: 'project/sweep-oldest-blocked-external', timestamp: ctx.now() },
  };
}

export const gatherGateFactsTask = defineTask('gather-gate-facts', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Re-check each issue gate against the real upstream source',
  shell: {
    command: [
      'set -euo pipefail',
      `cd ${JSON.stringify(args.repoRoot)}`,
      `echo '=== current issue states ==='`,
      `for i in ${args.issues.join(' ')}; do gh issue view "$i" --repo ${args.repo} --json number,title,state,labels --jq '"#\\(.number) \\(.state) [\\([.labels[].name]|join(","))] \\(.title)"'; done`,
      `echo`,
      `echo '=== #218 gate: upstream seerr-team/seerr PR 2715 and latest release ==='`,
      `gh pr view 2715 --repo seerr-team/seerr --json number,state,mergedAt,title --jq '"PR 2715 \\(.state) merged=\\(.mergedAt // "never") \\(.title)"' 2>&1 | head -3`,
      `gh release list --repo seerr-team/seerr --limit 3 2>&1 | head -5 || echo 'no releases listed'`,
      `echo`,
      `echo '=== #270 gate: lingarr releases past 1.2.4 ==='`,
      `gh release list --repo lingarr-translate/lingarr --limit 5 2>&1 | head -8 || echo 'no releases listed'`,
      `echo 'live lingarr image pin in git:'`,
      `grep -rn 'lingarr' 2-k3s/08.servarr/lingarr/kustomization.yaml 2>/dev/null | grep -iE 'newTag|digest|image' | head -5 || true`,
      `echo`,
      `echo '=== #290 gate: does kube-prometheus-stack ship control-plane EndpointSlices yet ==='`,
      `echo 'static EndpointSlices still carried in git:'`,
      `grep -rln 'EndpointSlice' 2-k3s/10.observability/ 2>/dev/null | head -5 || echo none`,
      `helm repo list 2>/dev/null | grep -i prometheus-community || echo 'prometheus-community repo not configured locally'`,
      `echo`,
      `echo '=== #260 and #230: are they superseded by a still-open owner issue ==='`,
      `for i in 526 339; do gh issue view "$i" --repo ${args.repo} --json number,state,title --jq '"#\\(.number) \\(.state) \\(.title)"'; done`,
      `echo`,
      `echo '=== off-limits (another agent holds worktrees for these) ==='`,
      `echo '${args.offLimits.join(' ')}'`,
    ].join('\n'),
    expectedExitCode: 0,
    timeout: 300000,
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['shell', 'github', 'gate-check'],
}));

export const applyDecisionsTask = defineTask('apply-owner-decisions', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Close the owner-named issues with evidence, leave the rest open',
  execution: { model: 'gpt-5.6-sol', harness: 'pi' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'GitHub backlog maintainer for an infrastructure repo',
      task: 'Apply the owner decision exactly: close only the named issues, each with a short evidence-based comment drawn from the gathered facts.',
      context: args,
      instructions: [
        `Use gh against ${args.repo}.`,
        `Honor the owner decision verbatim: ${JSON.stringify(args.ownerDecision)}.`,
        'Close only issues the owner named. Never close an issue they did not name.',
        `Never touch issues ${JSON.stringify(args.offLimits)}; another agent is working them.`,
        'Each closing comment must cite only facts present in the gathered evidence: the gate state, why it is resolved or superseded, and the issue that now owns any remaining work.',
        'Use reason completed for finished work and not planned for superseded or declined work.',
        'Open a follow-up issue only if the owner asked for one.',
        'Write in plain, simple English. No emoji. No invented values.',
        'Return only the requested JSON object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['closedIssues', 'keptOpen', 'summary'],
      properties: {
        closedIssues: { type: 'array', items: { type: 'number' } },
        keptOpen: { type: 'array', items: { type: 'number' } },
        followUps: { type: 'array', items: { type: 'number' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'github', 'close'],
}));

export const verifySweepTask = defineTask('verify-sweep', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Verify the intended issues closed and the rest stayed open',
  shell: {
    command: [
      'set -euo pipefail',
      `cd ${JSON.stringify(args.repoRoot)}`,
      `for i in ${(args.closedIssues || []).join(' ') || ''}; do test "$(gh issue view "$i" --repo ${args.repo} --json state --jq .state)" = CLOSED; echo "#$i CLOSED"; done`,
      `for i in ${(args.keptOpen || []).join(' ') || ''}; do test "$(gh issue view "$i" --repo ${args.repo} --json state --jq .state)" = OPEN; echo "#$i still OPEN"; done`,
      `git status --short | grep -v '^?? .a5c/' | grep . && exit 1 || true`,
      `echo 'sweep verified; worktree carries no unintended change'`,
    ].join('\n'),
    expectedExitCode: 0,
    timeout: 180000,
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['shell', 'verification', 'github'],
}));
