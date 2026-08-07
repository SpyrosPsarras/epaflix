#!/usr/bin/env node
/**
 * @process project/advance-370-and-339
 * @description Advance the two oldest issues that need no CI and no merge while
 * GitHub Actions is down: re-check #370's external TVDB gate against Skyhook and
 * live Sonarr, then run the read-only half of #339 (prove what the scoped ak-iac
 * role can actually do) and stop before the production IAM flip.
 *
 * Composition references:
 * - specializations/devops-sre-platform/incident-response.js
 * - methodologies/superpowers/verification-before-completion.js
 * - project/deliver-open-issues-oldest-first.js
 *
 * @skill kubernetes-ops specializations/devops-sre-platform/skills/kubernetes-ops/SKILL.md
 * @agent secops-expert specializations/devops-sre-platform/agents/secops-expert/AGENT.md
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

export async function process(inputs, ctx) {
  const {
    repo = 'SpyrosPsarras/epaflix',
    repoRoot = '/home/spy/Documents/Epaflix/k3s-swarm-proxmox-triage',
    tvdbIssue = 370,
    iamIssue = 339,
    seriesId = 89,
    tvdbId = 328975,
  } = inputs || {};

  const tvdbFacts = await ctx.task(checkTvdbGateTask, { repo, repoRoot, tvdbIssue, seriesId, tvdbId });

  const tvdbDecision = await ctx.breakpoint({
    title: `#${tvdbIssue} - TVDB gate re-check`,
    question:
      `Fresh evidence for #${tvdbIssue} is below. Say whether to close it, or leave it open with this evidence recorded.\n\n` +
      tvdbFacts.stdout,
    options: ['Leave open, record the re-check', 'Close it', 'Stop'],
    context: { runId: ctx.runId, issue: tvdbIssue, evidence: tvdbFacts.stdout },
    expert: 'owner',
    tags: ['approval-gate', 'external-text'],
  });

  if (!tvdbDecision.approved) {
    return { success: false, stopped: true, reason: tvdbDecision.response || 'stopped at the TVDB gate', tvdbFacts };
  }

  const tvdbApplied = await ctx.task(applyTvdbDecisionTask, {
    repo, repoRoot, tvdbIssue,
    ownerDecision: tvdbDecision.response || '',
    evidence: tvdbFacts.stdout,
  });

  const iamFacts = await ctx.task(probeScopedRoleTask, { repo, repoRoot, iamIssue });

  const iamDecision = await ctx.breakpoint({
    title: `#${iamIssue} - scoped ak-iac evidence, and whether to flip`,
    question:
      `Read-only probe of what ak-iac holds and what a scoped role would need is below. ` +
      `Approve posting this as a comment on #${iamIssue}? The production flip - removing ak-iac from authentik Admins - is NOT part of this approval.\n\n` +
      iamFacts.stdout,
    options: ['Post the findings, do not flip', 'Do not post', 'Stop'],
    context: {
      runId: ctx.runId,
      issue: iamIssue,
      evidence: iamFacts.stdout,
      excluded: 'Removing ak-iac from the authentik Admins group is deliberately not in this gate.',
    },
    expert: 'owner',
    tags: ['approval-gate', 'external-text'],
  });

  if (!iamDecision.approved) {
    return {
      success: true,
      tvdbApplied,
      iamPosted: false,
      note: 'TVDB half applied; IAM findings not posted at the owner request.',
    };
  }

  const iamPosted = await ctx.task(recordIamFindingsTask, {
    repo, repoRoot, iamIssue,
    evidence: iamFacts.stdout,
    ownerDecision: iamDecision.response || '',
  });

  const verified = await ctx.task(verifyBothTask, { repo, repoRoot, tvdbIssue, iamIssue });

  return {
    success: verified.exitCode === 0,
    tvdbApplied,
    iamPosted,
    verified,
    metadata: { processId: 'project/advance-370-and-339', timestamp: ctx.now() },
  };
}

export const checkTvdbGateTask = defineTask('check-tvdb-gate', (args, taskCtx) => ({
  kind: 'shell',
  title: `Re-check the TVDB numbering gate for #${args.tvdbIssue} against Skyhook and live Sonarr`,
  shell: {
    command: [
      'set -euo pipefail',
      `cd ${JSON.stringify(args.repoRoot)}`,
      `echo '=== what Skyhook serves today (public, no key) ==='`,
      `curl -fsS --max-time 30 "https://skyhook.sonarr.tv/v1/tvdb/shows/en/${args.tvdbId}" > /tmp/issue-370-skyhook.json`,
      `jq -r '"title=\\(.title) tvdbId=\\(.tvdbId) status=\\(.status)"' /tmp/issue-370-skyhook.json`,
      `jq -r '"season count served by Skyhook: \\([.seasons[]?.seasonNumber] | max)"' /tmp/issue-370-skyhook.json`,
      `jq -r '[.episodes[]? | select(.airDate != null and (.airDate | startswith("2026")))] | "2026 episodes sit in season: \\([.[].seasonNumber] | unique | tostring), count=\\(length)"' /tmp/issue-370-skyhook.json`,
      `echo`,
      `echo '=== live Sonarr for seriesId ${args.seriesId} ==='`,
      `SONARR_KEY=$(sops -d --extract '["sonarr_api_key"]' .github/instructions/secrets.enc.yaml)`,
      `echo "sonarr key length: \${#SONARR_KEY}"`,
      `PORT=38989`,
      `kubectl -n servarr port-forward service/sonarr "$PORT:8989" >/tmp/issue-370-pf.log 2>&1 &`,
      `PF=$!; trap 'kill $PF 2>/dev/null || true' EXIT`,
      `for i in $(seq 1 30); do curl -fsS --max-time 5 "http://127.0.0.1:$PORT/ping" >/dev/null 2>&1 && break; sleep 1; done`,
      `curl -fsS --max-time 60 -H "X-Api-Key: $SONARR_KEY" "http://127.0.0.1:$PORT/api/v3/series/${args.seriesId}" > /tmp/issue-370-series.json`,
      `unset SONARR_KEY`,
      `jq -r '"sonarr tvdbId=\\(.tvdbId) seasons=\\([.seasons[].seasonNumber]|max)"' /tmp/issue-370-series.json`,
      `jq -r '.seasons[] | select(.seasonNumber >= 9) | "S\\(.seasonNumber) monitored=\\(.monitored) files=\\(.statistics.episodeFileCount)/\\(.statistics.totalEpisodeCount)"' /tmp/issue-370-series.json`,
      `echo`,
      `echo '=== has the split happened? ==='`,
      `SKY_MAX=$(jq -r '[.seasons[]?.seasonNumber] | max' /tmp/issue-370-skyhook.json)`,
      `if [ "$SKY_MAX" -le 10 ]; then echo 'SPLIT LIKELY APPROVED: Skyhook now tops out at season '"$SKY_MAX"', so the revival may have been renumbered'; else echo 'SPLIT NOT APPLIED: Skyhook still serves the combined '"$SKY_MAX"'-season layout, so the +2 offset stands'; fi`,
      `echo`,
      `echo '=== stop-gap from #588 still in place? ==='`,
      `jq -r '.seasons[] | select(.seasonNumber == 10) | "S10 monitored=\\(.monitored) (false means the #523 mis-grab vector is still blocked)"' /tmp/issue-370-series.json`,
    ].join('\n'),
    expectedExitCode: 0,
    timeout: 300000,
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['shell', 'sonarr', 'gate-check', 'issue-370'],
}));

export const applyTvdbDecisionTask = defineTask('apply-tvdb-decision', (args, taskCtx) => ({
  kind: 'agent',
  title: `Record the #${args.tvdbIssue} re-check per the owner decision`,
  execution: { model: 'gpt-5.6-sol', harness: 'pi' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'GitHub backlog maintainer',
      task: `Apply the owner decision to issue #${args.tvdbIssue} using only the gathered evidence.`,
      context: args,
      instructions: [
        `Use gh against ${args.repo}.`,
        `Honor the owner decision verbatim: ${JSON.stringify(args.ownerDecision)}.`,
        'If the decision is to leave it open, post a short dated re-check comment stating what Skyhook serves now, what live Sonarr shows, whether the stop-gap is still in place, and that the gate has not moved. Do not close it.',
        'If the decision is to close, close with reason completed and a comment that states the evidence which justifies closing.',
        'Never invent a TVDB ruling. If no public update exists, say so plainly.',
        'Plain simple English, no emoji.',
        'Return only the requested JSON object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['action', 'issueState', 'summary'],
      properties: {
        action: { type: 'string' },
        issueState: { type: 'string' },
        commentUrl: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'github', 'issue-370'],
}));

export const probeScopedRoleTask = defineTask('probe-ak-iac-scope', (args, taskCtx) => ({
  kind: 'shell',
  title: `Read-only probe of ak-iac privileges for #${args.iamIssue}`,
  shell: {
    command: [
      'set -euo pipefail',
      `cd ${JSON.stringify(args.repoRoot)}`,
      `AK=$(sops -d --extract '["authentik_iac_service_account_token"]' .github/instructions/secrets.enc.yaml)`,
      `echo "token length: \${#AK}"`,
      `H="Authorization: Bearer $AK"`,
      `B=https://auth.epaflix.com/api/v3`,
      `echo '=== identity behind the token ==='`,
      `curl -fsS --max-time 30 -H "$H" "$B/core/users/me/" > /tmp/issue-339-me.json`,
      `jq -r '"username=\\(.user.username) pk=\\(.user.pk) superuser=\\(.user.is_superuser) groups=\\(.user.groups_obj // [] | map(.name) | tostring)"' /tmp/issue-339-me.json`,
      `echo`,
      `echo '=== how it gets superuser: group membership ==='`,
      `curl -fsS --max-time 30 -H "$H" "$B/core/groups/?page_size=200" > /tmp/issue-339-groups.json`,
      `jq -r '.results[] | select(.is_superuser == true) | "superuser group: \\(.name) pk=\\(.pk) members=\\(.users_obj // [] | map(.username) | tostring)"' /tmp/issue-339-groups.json`,
      `echo`,
      `echo '=== does a scoped role already exist ==='`,
      `curl -fsS --max-time 30 -H "$H" "$B/rbac/roles/?page_size=200" > /tmp/issue-339-roles.json`,
      `jq -r 'if (.results | length) == 0 then "no RBAC roles defined at all" else (.results[] | "role: \\(.name) pk=\\(.pk)") end' /tmp/issue-339-roles.json`,
      `echo`,
      `echo '=== what the IaC actually calls, taken from the repo, not from memory ==='`,
      `grep -rhoE '/api/v3/[a-z0-9_/-]+' --include='*.sh' --include='*.md' --include='*.yaml' --include='*.py' . 2>/dev/null | grep -v node_modules | sed 's#/api/v3/##' | cut -d/ -f1-2 | sort | uniq -c | sort -rn | head -15`,
      `echo`,
      `echo '=== object counts the IaC manages (proves blast radius of the flip) ==='`,
      `for ep in core/applications providers/all outposts/instances flows/instances core/groups; do n=$(curl -fsS --max-time 30 -H "$H" "$B/$ep/?page_size=1" | jq -r '.pagination.count // 0'); printf '%-22s %s\\n' "$ep" "$n"; done`,
      `unset AK`,
      `echo`,
      `echo '=== dependents that break if the token loses a permission ==='`,
      `grep -rl 'authentik_iac_service_account_token' --include='*.md' --include='*.sh' --include='*.yaml' . 2>/dev/null | grep -v node_modules | head -10 || echo 'no direct references in tracked files'`,
    ].join('\n'),
    expectedExitCode: 0,
    timeout: 300000,
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['shell', 'authentik', 'read-only', 'issue-339'],
}));

export const recordIamFindingsTask = defineTask('record-iam-findings', (args, taskCtx) => ({
  kind: 'agent',
  title: `Post the #${args.iamIssue} probe findings, do not flip anything`,
  execution: { model: 'gpt-5.6-sol', harness: 'pi' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Platform security engineer reporting a read-only finding',
      task: `Post one comment on #${args.iamIssue} summarising the probe. Change nothing in Authentik.`,
      context: args,
      instructions: [
        `Use gh against ${args.repo}.`,
        'Summarise only what the probe actually returned: how ak-iac obtains superuser, whether any scoped RBAC role exists yet, which API surfaces the repo really calls, and the object counts that set the blast radius.',
        'State clearly that nothing was changed and that the flip still needs its own attended window.',
        'Never print the token or any secret value. Token length is acceptable.',
        'Do not close the issue.',
        'Plain simple English, no emoji, no invented values.',
        'Return only the requested JSON object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['commentUrl', 'issueState', 'summary'],
      properties: {
        commentUrl: { type: 'string' },
        issueState: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'github', 'issue-339'],
}));

export const verifyBothTask = defineTask('verify-370-339', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Verify both issues ended in the intended state',
  shell: {
    command: [
      'set -euo pipefail',
      `cd ${JSON.stringify(args.repoRoot)}`,
      `for i in ${args.tvdbIssue} ${args.iamIssue}; do printf '#%s %s comments=%s\\n' "$i" "$(gh issue view "$i" --repo ${args.repo} --json state --jq .state)" "$(gh issue view "$i" --repo ${args.repo} --json comments --jq '.comments|length')"; done`,
      `test "$(gh issue view ${args.iamIssue} --repo ${args.repo} --json state --jq .state)" = OPEN`,
      `git status --short | grep -v '^?? ' | grep . && exit 1 || true`,
      `echo 'both issues recorded; the IAM flip remains open and unperformed; worktree clean'`,
    ].join('\n'),
    expectedExitCode: 0,
    timeout: 180000,
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['shell', 'verification'],
}));
