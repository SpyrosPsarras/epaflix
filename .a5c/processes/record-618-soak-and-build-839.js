#!/usr/bin/env node
/**
 * @process project/record-618-soak-and-build-839
 * @description Two things: record the owner's 4-day soak decision on #618 so a
 * future session picks it up with an explicit date and check procedure, then
 * build the missingFiles/error detector from #839 following the #835 pattern.
 *
 * @skill kubernetes-ops specializations/devops-sre-platform/skills/kubernetes-ops/SKILL.md
 * @agent sre-expert specializations/devops-sre-platform/agents/sre-expert/AGENT.md
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

export async function process(inputs, ctx) {
  const {
    repo = 'SpyrosPsarras/epaflix',
    repoRoot = '/home/spy/Documents/Epaflix/k3s-swarm-proxmox-triage',
    soakIssue = 618,
    detectorIssue = 839,
    soakUntil = '2026-08-11',
    branch = 'feat-missing-files-detector-839',
  } = inputs || {};

  const recorded = await ctx.task(recordSoakTask, { repo, repoRoot, soakIssue, soakUntil });

  const built = await ctx.task(buildDetectorTask, { repo, repoRoot, issue: detectorIssue, branch });

  const approval = await ctx.breakpoint({
    title: `#${detectorIssue} detector - approve PR and merge`,
    question:
      `#${soakIssue} soak note is posted, review date ${soakUntil}.\n\n` +
      `The #${detectorIssue} detector is committed and pushed as a PR, not merged.\n\n` +
      `WHAT IT DETECTS:\n${built.behaviour}\n\n` +
      `VALIDATION RUN:\n- ${(built.validation || []).join('\n- ')}\n\n` +
      `PR: ${built.prUrl}\n\nApprove merging it and proving it with a real run?`,
    options: ['Merge and prove it', 'Hold', 'Stop'],
    context: { runId: ctx.runId, prUrl: built.prUrl, filesChanged: built.filesChanged },
    expert: 'owner',
    tags: ['approval-gate', 'deploy'],
  });

  if (!approval.approved) {
    return { success: false, stopped: true, soakRecorded: recorded.commentUrl, prUrl: built.prUrl };
  }

  const shipped = await ctx.task(mergeAndProveTask, {
    repo, repoRoot, issue: detectorIssue, branch,
    prUrl: built.prUrl,
    ownerDecision: approval.response || '',
  });

  return {
    success: shipped.merged === true,
    soakRecorded: recorded.commentUrl,
    detector: shipped,
    metadata: { processId: 'project/record-618-soak-and-build-839', timestamp: ctx.now() },
  };
}

export const recordSoakTask = defineTask('record-618-soak', (args, taskCtx) => ({
  kind: 'agent',
  title: `Record the ${args.soakUntil} soak decision on #${args.soakIssue}`,
  execution: { model: 'gpt-5.6-sol', harness: 'pi' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Backlog maintainer writing a note for a future session',
      task: `Post one comment on #${args.soakIssue} recording the owner decision to soak until ${args.soakUntil}, then re-assess arming.`,
      context: args,
      instructions: [
        `Use gh against ${args.repo}. Do not close the issue and do not change anything live.`,
        `State the decision: both earlier blockers are resolved, so the remaining gap is track record. Wait until ${args.soakUntil}, then re-assess.`,
        'Write it so a session with no memory of today can act on it: give the exact checks to run and what result means go or no-go.',
        'The checks are: the census runs daily at 07:00Z; confirm several consecutive runs report the same 4 orphans with no new ones, and that held back stays 0.',
        'State the standing risk that does not change with time: DELETE_FILES is hardcoded true, so arming deletes data and is one-way.',
        'Note what was already settled so it is not re-litigated: #706 was verified applied on 2026-08-02, and the stale bf83bdcb carve-out was retired in PR #838.',
        'Plain simple English, no emoji, no invented values.',
        'Return only the requested JSON object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['commentUrl', 'summary'],
      properties: {
        commentUrl: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'github', 'issue-618', 'soak'],
}));

export const buildDetectorTask = defineTask('build-missing-files-detector', (args, taskCtx) => ({
  kind: 'agent',
  title: `Build the missingFiles/error detector for #${args.issue}`,
  execution: { model: 'gpt-5.6-sol', harness: 'pi' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'SRE adding a detector to a Kustomize + ArgoCD repo',
      task: `Implement #${args.issue}: report qBittorrent torrents stuck in missingFiles or error state. Open a PR, do not merge.`,
      context: args,
      instructions: [
        `Work only inside ${args.repoRoot}, on branch ${args.branch} off origin/main.`,
        'Copy the established pattern from 2-k3s/maintenance/blind-queue-check-cronjob.yaml (#835): same credential source, same exit-code contract, same --selftest discipline, same scoped PrometheusRule style in 2-k3s/10.observability/alertmanager-config/custom-alerts.yaml.',
        'REPORT ONLY. It must never delete, pause or modify a torrent. max_ratio_act is 0 precisely so private torrents are never removed automatically, and this detector must not become the thing that breaks that.',
        'Include a grace window so a torrent briefly in error during a recheck does not trip it, the same reasoning as GRACE_MINUTES in #835.',
        'Report the hash prefix, state, category and size only. Never print a torrent name: the repo forbids media titles in committed output.',
        'Validate for real and report the commands with their output: python3 -m py_compile, your --selftest, and yaml.safe_load_all on every changed file. Confirm each kustomization resources: path still resolves.',
        'kustomize build cannot run here because ksops is unavailable; say so rather than claiming it passed.',
        'Commit with a conventional-commit subject and no issue number in it. Push and open a PR with Closes #' + args.issue + ' and a ## Test plan whose pre-merge boxes you actually ran.',
        'Never print a secret value. Never fetch raw logs.',
        'Return only the requested JSON object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'behaviour', 'filesChanged', 'validation', 'summary'],
      properties: {
        prUrl: { type: 'string' },
        behaviour: { type: 'string' },
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
  labels: ['agent', 'implementation', 'issue-839'],
}));

export const mergeAndProveTask = defineTask('merge-and-prove-839', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Merge the detector and prove it with a real run',
  execution: { model: 'gpt-5.6-sol', harness: 'pi' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Epaflix release manager',
      task: 'Merge the approved PR through the semi-linear policy and prove the job runs.',
      context: args,
      instructions: [
        'Rebase onto origin/main, push with --force-with-lease, wait for validate, confirm the branch is up to date, then gh pr merge --merge.',
        'Wait for ArgoCD to reconcile, create a manual job from the new CronJob, and report the real outcome.',
        'The client currently holds zero torrents in missingFiles or error, so the expected result is a clean exit 0. If it reports a finding instead, do not call it a pass - investigate and report.',
        'Tick post-merge boxes by editing the PR description, never a comment. Leave no unchecked box without an issue behind it.',
        'Never print a secret or a torrent name.',
        'Return only the requested JSON object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['merged', 'mergeCommit', 'runResult', 'summary'],
      properties: {
        merged: { type: 'boolean' },
        mergeCommit: { type: 'string' },
        runResult: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'merge', 'issue-839'],
}));
