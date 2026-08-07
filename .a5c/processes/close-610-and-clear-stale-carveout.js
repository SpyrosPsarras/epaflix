#!/usr/bin/env node
/**
 * @process project/close-610-and-clear-stale-carveout
 * @description #610's torrent no longer exists in qBittorrent, so the hash
 * carve-out added to protect it now guards nothing. Verify that from live
 * state, retire the stale carve-out in git, close #610 with evidence, and
 * keep its one still-valid ask - detection of missingFiles/error torrents -
 * alive as its own issue instead of losing it in a closed ticket.
 *
 * @skill gitops specializations/devops-sre-platform/skills/gitops/SKILL.md
 * @agent platform-engineer specializations/devops-sre-platform/agents/platform-engineer/AGENT.md
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

export async function process(inputs, ctx) {
  const {
    repo = 'SpyrosPsarras/epaflix',
    repoRoot = '/home/spy/Documents/Epaflix/k3s-swarm-proxmox-triage',
    issue = 610,
    staleHash = 'bf83bdcb',
    branch = 'chore-retire-stale-keep-hash-610',
  } = inputs || {};

  const evidence = await ctx.task(confirmAbsenceTask, { repo, repoRoot, issue, staleHash });

  const approval = await ctx.breakpoint({
    title: `#${issue} - retire the stale carve-out and close`,
    question:
      `Live evidence below. The plan: remove the ${staleHash} hash carve-out from the census (the category carve-out still covers manual-import), ` +
      `close #${issue}, and open a follow-up for its one remaining valid ask, a missingFiles/error detector.\n\n` +
      evidence.stdout +
      '\n\nApprove the PR, the closure and the follow-up?',
    options: ['Approve all three', 'Hold', 'Stop'],
    context: {
      runId: ctx.runId,
      issue,
      staleHash,
      safety: 'KEEP_CATEGORIES still protects manual-import, so removing the hash prefix does not expose the one healthy torrent in that category.',
    },
    expert: 'owner',
    tags: ['approval-gate', 'deploy', 'external-text'],
  });

  if (!approval.approved) {
    return { success: false, stopped: true, reason: approval.response || 'held', evidence };
  }

  const shipped = await ctx.task(shipCarveoutRemovalTask, {
    repo, repoRoot, issue, staleHash, branch,
    evidence: evidence.stdout,
    ownerDecision: approval.response || '',
  });

  const verified = await ctx.task(verifyTask, { repo, repoRoot, issue, staleHash });

  return {
    success: shipped.merged && verified.exitCode === 0,
    prUrl: shipped.prUrl,
    followUpIssue: shipped.followUpIssue,
    verified,
    metadata: { processId: 'project/close-610-and-clear-stale-carveout', timestamp: ctx.now() },
  };
}

export const confirmAbsenceTask = defineTask('confirm-610-absence', (args, taskCtx) => ({
  kind: 'shell',
  title: `Confirm ${args.staleHash} is gone and nothing is in missingFiles or error`,
  shell: {
    command: [
      'set -uo pipefail',
      `cd ${JSON.stringify(args.repoRoot)}`,
      `QU=$(sops -d --extract '["qbittorrent_webui_username"]' .github/instructions/secrets.enc.yaml)`,
      `QP=$(sops -d --extract '["qbittorrent_webui_password"]' .github/instructions/secrets.enc.yaml)`,
      `P=39010`,
      `kubectl -n servarr port-forward service/qbittorrent "$P:8080" >/tmp/i610-pf.log 2>&1 &`,
      `PF=$!; trap 'kill $PF 2>/dev/null || true' EXIT`,
      `for i in $(seq 1 40); do curl -fsS --max-time 5 "http://127.0.0.1:$P/api/v2/app/version" >/dev/null 2>&1 && break; sleep 2; done`,
      `CJ=$(mktemp)`,
      `curl -s -c "$CJ" --max-time 20 -H "Referer: http://127.0.0.1:$P" -d "username=$QU&password=$QP" "http://127.0.0.1:$P/api/v2/auth/login" >/dev/null`,
      `unset QU QP`,
      `curl -fsS -b "$CJ" --max-time 30 "http://127.0.0.1:$P/api/v2/torrents/info" > /tmp/i610-torrents.json`,
      `rm -f "$CJ"`,
      `echo '=== the #610 torrent ==='`,
      `jq -r '[.[]|select(.hash|startswith("${args.staleHash}"))] | if length==0 then "${args.staleHash}: ABSENT from the client" else (.[]|"present state=\\(.state)") end' /tmp/i610-torrents.json`,
      `echo '=== broken-state torrents anywhere ==='`,
      `jq -r '[.[]|select(.state=="missingFiles" or .state=="error")]|"missingFiles or error: \\(length)"' /tmp/i610-torrents.json`,
      `echo '=== manual-import, the category the carve-out also covers ==='`,
      `jq -r '[.[]|select(.category=="manual-import")]|.[]|"  \\(.hash[:8]) state=\\(.state) up_MB=\\((.uploaded/1048576)|floor)"' /tmp/i610-torrents.json`,
      `echo '=== the carve-outs as they stand in git ==='`,
      `grep -n 'KEEP_HASH_PREFIXES\\|KEEP_CATEGORIES' 2-k3s/maintenance/orphan-census-cronjob.yaml | head`,
      `test -z "$(jq -r '[.[]|select(.hash|startswith("${args.staleHash}"))]|.[].hash' /tmp/i610-torrents.json)"`,
      `echo 'CONFIRMED: the carve-out protects a torrent that no longer exists'`,
    ].join('\n'),
    expectedExitCode: 0,
    timeout: 300000,
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['shell', 'qbittorrent', 'issue-610'],
}));

export const shipCarveoutRemovalTask = defineTask('ship-carveout-removal', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Retire the stale carve-out, close #610, open the detection follow-up',
  execution: { model: 'gpt-5.6-sol', harness: 'pi' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'GitOps engineer and backlog maintainer',
      task: 'Ship the approved change, close the issue with evidence, and keep the surviving ask alive as its own issue.',
      context: args,
      instructions: [
        `Work only inside ${args.repoRoot}. Read CLAUDE.md first.`,
        `git fetch origin, git checkout -B ${args.branch} origin/main.`,
        `In 2-k3s/maintenance/orphan-census-cronjob.yaml, retire the ${args.staleHash} entry from KEEP_HASH_PREFIXES. Explain in the comment that the torrent it protected is gone from the client and that KEEP_CATEGORIES still covers manual-import, so the protection that matters is unchanged.`,
        'Keep the mechanism itself: leave KEEP_HASH_PREFIXES in place as an empty tuple with its comment, so the next person has an obvious place to add a hash. Do not delete the feature.',
        'Update the selftest if it asserts on that hash, and run it.',
        'Validate: python3 -m py_compile on the embedded script, run --selftest, parse the YAML with yaml.safe_load_all.',
        'Commit with a conventional-commit subject and no issue number in it. Push, open a PR, run and tick the pre-merge test-plan boxes, then rebase, wait for validate, and merge with gh pr merge --merge.',
        `Close #${args.issue} with a comment carrying the live evidence: the torrent is absent, nothing is in missingFiles or error, and manual-import holds one healthy torrent.`,
        `Open ONE follow-up issue for the surviving checkbox of #${args.issue}: a periodic check for torrents in missingFiles or error state, cross-linking #482 and #607 which are the same silent-failure class, and #835 which is the pattern to copy. Use the Finding / Current state / Desired outcome / Notes shape.`,
        'Never print a secret. Never fetch raw *arr or Traefik logs.',
        'Return only the requested JSON object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'merged', 'mergeCommit', 'followUpIssue', 'summary'],
      properties: {
        prUrl: { type: 'string' },
        merged: { type: 'boolean' },
        mergeCommit: { type: 'string' },
        followUpIssue: { type: 'number' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'gitops', 'issue-610'],
}));

export const verifyTask = defineTask('verify-610-closeout', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Verify the carve-out is gone from main and the issue is closed',
  shell: {
    command: [
      'set -euo pipefail',
      `cd ${JSON.stringify(args.repoRoot)}`,
      `git fetch origin --quiet`,
      `test "$(gh issue view ${args.issue} --repo ${args.repo} --json state --jq .state)" = CLOSED`,
      `! git show origin/main:2-k3s/maintenance/orphan-census-cronjob.yaml | grep -q '"${args.staleHash}"'`,
      `git show origin/main:2-k3s/maintenance/orphan-census-cronjob.yaml | grep -q 'KEEP_CATEGORIES'`,
      `git status --short | grep -v '^?? ' | grep . && exit 1 || true`,
      `echo 'stale carve-out retired on main, category protection intact, #${args.issue} closed'`,
    ].join('\n'),
    expectedExitCode: 0,
    timeout: 300000,
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['shell', 'verification', 'issue-610'],
}));
