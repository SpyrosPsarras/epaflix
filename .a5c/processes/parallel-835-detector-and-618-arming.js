#!/usr/bin/env node
/**
 * @process project/parallel-835-detector-and-618-arming
 * @description Two independent pieces of work in parallel, deliberately given
 * to different kinds of agent:
 *   #835 - build the blind-*arr-queue detector. Implementation work, so a
 *          full-tool agent in an isolated worktree, PR only, never merged by
 *          the agent.
 *   #618 - arm the orphan reaper. This deletes private-tracker torrents with
 *          hit-and-run exposure, so it gets a READ-ONLY planning agent that
 *          produces a risk assessment and an exact procedure. No agent flips
 *          ARMED.
 *
 * @skill kubernetes-ops specializations/devops-sre-platform/skills/kubernetes-ops/SKILL.md
 * @agent sre-expert specializations/devops-sre-platform/agents/sre-expert/AGENT.md
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

export async function process(inputs, ctx) {
  const {
    repo = 'SpyrosPsarras/epaflix',
    repoRoot = '/home/spy/Documents/Epaflix/k3s-swarm-proxmox-triage',
    detectorIssue = 835,
    armingIssue = 618,
    offLimits = [723, 792, 800],
  } = inputs || {};

  const [detector, arming] = await ctx.parallel.all([
    () => ctx.task(buildDetectorTask, { repo, repoRoot, issue: detectorIssue, offLimits }),
    () => ctx.task(planArmingTask, { repo, repoRoot, issue: armingIssue }),
  ]);

  const decision = await ctx.breakpoint({
    title: 'Review both results - one PR to merge, one plan to accept or reject',
    question:
      `#${detectorIssue} DETECTOR, built and pushed as a PR, not merged:\n${detector.summary}\n` +
      `PR: ${detector.prUrl}\n\n` +
      `#${armingIssue} ARMING, analysis only, nothing changed:\n${arming.recommendation}\n\n` +
      `RISK THE PLAN IDENTIFIES:\n${arming.risk}\n\n` +
      'Reply with what to do: merge the detector PR, and separately whether to arm the reaper now, later, or not at all.',
    options: ['Merge detector, decide arming separately', 'Hold both', 'Stop'],
    context: {
      runId: ctx.runId,
      detectorPr: detector.prUrl,
      armingRecommendation: arming.recommendation,
      armingBlockers: arming.blockers,
      irreversible: 'Arming the reaper deletes torrents and their data. Four of the current orphans are private-tracker torrents.',
    },
    expert: 'owner',
    tags: ['approval-gate', 'deploy', 'destructive'],
  });

  if (!decision.approved) {
    return { success: false, stopped: true, reason: decision.response || 'held', detector, arming };
  }

  const finished = await ctx.task(finishDetectorTask, {
    repo, repoRoot,
    issue: detectorIssue,
    prUrl: detector.prUrl,
    ownerDecision: decision.response || '',
  });

  return {
    success: finished.merged === true,
    detector: finished,
    arming,
    metadata: { processId: 'project/parallel-835-detector-and-618-arming', timestamp: ctx.now() },
  };
}

export const buildDetectorTask = defineTask('build-blind-queue-detector', (args, taskCtx) => ({
  kind: 'agent',
  title: `Build the blind-*arr-queue detector for #${args.issue}`,
  execution: { model: 'gpt-5.6-sol', harness: 'pi' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'SRE building a detector in a Kustomize + ArgoCD GitOps repo',
      task: `Implement #${args.issue}: detect an *arr whose queue has gone blind, and alert on it. Open a PR. Do not merge.`,
      context: args,
      instructions: [
        'Work in an isolated git worktree under /tmp, never in the main checkout and never in another worktree.',
        'Read the issue and CLAUDE.md first. Follow the repo conventions for CronJobs in 2-k3s/maintenance and alert rules in 2-k3s/10.observability.',
        'Model the new job on the existing siblings so it fits: orphan-census-cronjob.yaml and prowlarr-indexer-health-cronjob.yaml. Reuse the same credential sources rather than inventing new ones.',
        'The signal: grabs carrying a downloadId inside a recent window, with neither a queue row nor an import. Never scrape *arr or Traefik logs - they contain API keys.',
        'Cover sonarr, sonarr2 and radarr; they run the same image and the same defect applies.',
        'Follow the sibling exit-code contract so the existing alerting picks it up, and add a scoped PrometheusRule if that is how the siblings surface.',
        'Include a --selftest path with cases for: blind queue detected, idle library not flagged, and an import that already happened not flagged.',
        'Validate for real: python3 -m py_compile on any embedded script, run the selftest, and parse every changed YAML with yaml.safe_load_all. Report the commands you ran and their output.',
        'Commit with a conventional-commit subject that carries no issue number, push the branch, open a PR whose body has a ## Test plan with the pre-merge boxes you actually executed.',
        'Do not merge, do not force-push over anyone, do not touch issues ' + JSON.stringify(args.offLimits) + ', and never print a secret value.',
        'Return only the requested JSON object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'filesChanged', 'validation', 'summary'],
      properties: {
        prUrl: { type: 'string' },
        filesChanged: { type: 'array', items: { type: 'string' } },
        validation: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'implementation', 'issue-835'],
}));

export const planArmingTask = defineTask('plan-reaper-arming', (args, taskCtx) => ({
  kind: 'agent',
  title: `Assess arming the orphan reaper for #${args.issue} - read only`,
  execution: { model: 'gpt-5.6-sol', harness: 'pi' },
  agent: {
    name: 'Plan',
    prompt: {
      role: 'Infrastructure architect assessing an irreversible deletion switch',
      task: `Produce a risk assessment and an exact procedure for #${args.issue}, arming the orphan reaper. Change nothing.`,
      context: args,
      instructions: [
        'READ ONLY. Do not edit files, do not commit, do not run kubectl commands that mutate anything, and do not flip ARMED.',
        'Read the issue, 2-k3s/maintenance/orphan-census-cronjob.yaml, and the census output from the run today.',
        'The census now reports 4 orphans, all private-tracker torrents, one at 39.89% progress and three near zero, ages 236h to 734h.',
        'Assess the hit-and-run exposure specifically: deleting a private-tracker torrent can carry a ratio or seeding obligation, and the repo has a standing rule that private trackers are never auto-deleted.',
        'Establish whether arming would delete data as well as the torrent entry, and what the reaper does about the keep-categories and keep-hash carve-outs.',
        'Check whether #706, the private stalled rule split named in the census output, is a prerequisite: the census itself warns that without it new orphans keep being manufactured.',
        'Say plainly whether arming now is safe, and if not, what must happen first, in order.',
        'Give an exact rollback: what to change back, and what is unrecoverable once a torrent and its data are gone.',
        'Return only the requested JSON object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['recommendation', 'risk', 'blockers', 'procedure', 'rollback', 'summary'],
      properties: {
        recommendation: { type: 'string' },
        risk: { type: 'string' },
        blockers: { type: 'array', items: { type: 'string' } },
        procedure: { type: 'array', items: { type: 'string' } },
        rollback: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'planning', 'read-only', 'issue-618'],
}));

export const finishDetectorTask = defineTask('finish-detector-pr', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Rebase, merge and verify the detector PR',
  execution: { model: 'gpt-5.6-sol', harness: 'pi' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Epaflix release manager',
      task: 'Merge the approved detector PR through the semi-linear policy and prove it runs.',
      context: args,
      instructions: [
        `Honor the owner decision verbatim: ${JSON.stringify(args.ownerDecision)}. Merge the detector PR only if they said so.`,
        'Rebase onto origin/main, push with --force-with-lease, wait for validate to pass, confirm the branch is up to date, then gh pr merge --merge.',
        'Wait for ArgoCD to reconcile, then create a manual job from the new CronJob and report the real outcome, including whether it correctly stays quiet on a healthy *arr.',
        'Tick post-merge test-plan boxes by editing the PR description, never a comment.',
        'Never print a secret. Never fetch raw *arr logs.',
        'Return only the requested JSON object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['merged', 'mergeCommit', 'provedBy', 'summary'],
      properties: {
        merged: { type: 'boolean' },
        mergeCommit: { type: 'string' },
        provedBy: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'merge', 'issue-835'],
}));
