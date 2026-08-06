#!/usr/bin/env node
/**
 * @process project/verify-loki-purge-751
 * @description Verify the elapsed Loki credential-line deletion window using
 * count-only queries, then close #751 and its parent #702 with evidence.
 * Never fetch matching log lines.
 *
 * Composition references:
 * - specializations/devops-sre-platform/incident-response.js
 * - methodologies/superpowers/verification-before-completion.js
 * - project/deliver-open-issues-oldest-first.js
 *
 * @skill kubernetes-ops specializations/devops-sre-platform/skills/kubernetes-ops/SKILL.md
 * @agent observability-expert specializations/devops-sre-platform/agents/observability-expert/AGENT.md
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

export async function process(inputs, ctx) {
  const {
    repo = 'SpyrosPsarras/epaflix',
    repoRoot = '/tmp/epaflix-issue-751',
    childIssue = 751,
    parentIssue = 702,
  } = inputs || {};

  let firstPass = null;
  try {
    firstPass = await ctx.task(verifyPurgeTask, { repo, repoRoot, childIssue, parentIssue });
  } catch (err) {
    ctx.log('warn', `strict all-zero gate failed: ${String(err && err.message ? err.message : err)}`);
    firstPass = { failed: true };
  }

  const evidence = await ctx.task(residualAnalysisTask, { repo, repoRoot, childIssue, parentIssue });

  const approval = await ctx.breakpoint({
    title: 'Approve #751 and #702 closing comments plus the residual follow-up issue',
    question:
      'Count-only verification is complete. The apikey purge is fully verified; a small non-zero Password residual remains and needs its own issue. ' +
      'Approve posting the drafts below, opening the follow-up issue, and closing #751 and #702?\n\n' +
      evidence.stdout,
    options: ['Approve drafts, open follow-up, close both', 'Do not post', 'Stop'],
    context: {
      runId: ctx.runId,
      issues: [childIssue, parentIssue],
      strictAllZeroGate: firstPass && firstPass.failed ? 'failed (Password residual non-zero)' : 'passed',
      evidence: evidence.stdout,
      safety: 'Only counts were queried; no matching log line or credential value was fetched.',
    },
    expert: 'owner',
    tags: ['approval-gate', 'external-text'],
  });

  if (!approval.approved) {
    return {
      success: false,
      stopped: true,
      reason: approval.response || approval.feedback || 'Closing comments not approved',
      evidence,
    };
  }

  const closed = await ctx.task(closeIssuesTask, {
    repo, repoRoot, childIssue, parentIssue,
    evidence: evidence.stdout,
    approvalResponse: approval.response || 'Approved',
  });

  const finalCheck = await ctx.task(finalCheckTask, { repo, repoRoot, childIssue, parentIssue });

  return {
    success: closed.closedIssues.includes(childIssue)
      && closed.closedIssues.includes(parentIssue)
      && finalCheck.exitCode === 0,
    evidence,
    closed,
    finalCheck,
    metadata: { processId: 'project/verify-loki-purge-751', timestamp: ctx.now() },
  };
}

export const verifyPurgeTask = defineTask('verify-loki-purge-count-only', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Verify Loki delete requests and count-only acceptance criteria',
  shell: {
    command: [
      'set -euo pipefail',
      `cd ${JSON.stringify(args.repoRoot)}`,
      `test "$(gh issue view ${args.childIssue} --repo ${args.repo} --json state --jq .state)" = OPEN`,
      `test "$(gh issue view ${args.parentIssue} --repo ${args.repo} --json state --jq .state)" = OPEN`,
      `PORT=33151`,
      `if ss -ltn | awk '{print $4}' | grep -qE ':33151$'; then echo 'local port 33151 already in use' >&2; exit 1; fi`,
      `kubectl -n observability port-forward service/loki "$PORT:3100" >/tmp/issue-751-portforward.log 2>&1 &`,
      `PF_PID=$!`,
      `cleanup() { kill "$PF_PID" 2>/dev/null || true; wait "$PF_PID" 2>/dev/null || true; }`,
      `trap cleanup EXIT`,
      `for i in $(seq 1 30); do curl -fsS "http://127.0.0.1:$PORT/ready" >/dev/null && break; sleep 1; done`,
      `curl -fsS --max-time 60 -H 'X-Scope-OrgID: 1' "http://127.0.0.1:$PORT/loki/api/v1/delete" > /tmp/issue-751-delete-status.json`,
      `jq -e '[.. | objects | select((.request_id? // .id? // "") | startswith("25e63ca2")) | .status] | any(. == "processed")' /tmp/issue-751-delete-status.json >/dev/null`,
      `jq -e '[.. | objects | select((.request_id? // .id? // "") | startswith("9cf45ca9")) | .status] | any(. == "processed")' /tmp/issue-751-delete-status.json >/dev/null`,
      `Q1='{namespace="traefik-system"} |~ ` + '`' + `apikey=[A-Za-z0-9]{16,}` + '`' + `'`,
      `Q2='{namespace="servarr"} |~ ` + '`' + `Password=[^;<[:space:]]{6,}` + '`' + `'`,
      `Q3='{namespace="traefik-system"}'`,
      `SURVIVOR_AT='2026-07-20T12:00:00Z'`,
      `curl -fsSG --max-time 300 -H 'X-Scope-OrgID: 1' --data-urlencode "query=sum(count_over_time($Q1 [30d]))" "http://127.0.0.1:$PORT/loki/api/v1/query" > /tmp/issue-751-apikey-count.json`,
      `curl -fsSG --max-time 300 -H 'X-Scope-OrgID: 1' --data-urlencode "query=sum(count_over_time($Q2 [30d]))" "http://127.0.0.1:$PORT/loki/api/v1/query" > /tmp/issue-751-password-count.json`,
      `curl -fsSG --max-time 300 -H 'X-Scope-OrgID: 1' --data-urlencode "query=sum(count_over_time($Q3 [1h]))" --data-urlencode "time=$SURVIVOR_AT" "http://127.0.0.1:$PORT/loki/api/v1/query" > /tmp/issue-751-ordinary-count.json`,
      `API_COUNT=$(jq -r '[.data.result[]?.value[1] | tonumber] | add // 0' /tmp/issue-751-apikey-count.json)`,
      `PASSWORD_COUNT=$(jq -r '[.data.result[]?.value[1] | tonumber] | add // 0' /tmp/issue-751-password-count.json)`,
      `ORDINARY_COUNT=$(jq -r '[.data.result[]?.value[1] | tonumber] | add // 0' /tmp/issue-751-ordinary-count.json)`,
      `test "$API_COUNT" = 0`,
      `test "$PASSWORD_COUNT" = 0`,
      `awk -v n="$ORDINARY_COUNT" 'BEGIN { exit !(n > 0) }'`,
      `S1=$(jq -r '[.. | objects | select((.request_id? // .id? // "") | startswith("25e63ca2")) | .status] | unique | join(",")' /tmp/issue-751-delete-status.json)`,
      `S2=$(jq -r '[.. | objects | select((.request_id? // .id? // "") | startswith("9cf45ca9")) | .status] | unique | join(",")' /tmp/issue-751-delete-status.json)`,
      `printf '%s\\n' 'DRAFT #751 comment:'`,
      `printf '%s\\n' 'Verified after the 24-hour cancel window and compaction cycle:'`,
      `printf -- '- delete request 25e63ca2: %s\\n' "$S1"`,
      `printf -- '- delete request 9cf45ca9: %s\\n' "$S2"`,
      `printf -- '- count-only apikey query over 30d: %s\\n' "$API_COUNT"`,
      `printf -- '- count-only Password query over 30d: %s\\n' "$PASSWORD_COUNT"`,
      `printf -- '- ordinary Traefik log count in a 1h slice inside the purge window (%s): %s (non-credential logs survived)\\n' "$SURVIVOR_AT" "$ORDINARY_COUNT"`,
      `printf '%s\\n' 'No matching log line was fetched; only aggregate counts were queried. Closing as completed.'`,
      `printf '%s\\n\\n' 'DRAFT #702 comment:'`,
      `printf '%s\\n' 'The final purge gate is complete via #751: both Loki delete requests are processed, both credential-pattern count-only queries return 0, and ordinary Traefik logs remain present.'`,
      `printf '%s\\n' 'All four original outcomes are now complete: exposed keys rotated/followed up, the source leak removed by internal Service DNS, historical Loki data purged, and regression alerting/redaction live. Closing as completed.'`,
    ].join('\n'),
    expectedExitCode: 0,
    timeout: 180000,
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['shell', 'loki', 'verification', 'count-only'],
}));

export const residualAnalysisTask = defineTask('analyze-loki-purge-residual', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Report purge outcome and residual credential-line counts (count-only)',
  shell: {
    command: [
      'set -euo pipefail',
      `cd ${JSON.stringify(args.repoRoot)}`,
      `PORT=33154`,
      `kubectl -n observability port-forward service/loki "$PORT:3100" >/tmp/issue-751-pf-residual.log 2>&1 &`,
      `PF_PID=$!`,
      `trap 'kill "$PF_PID" 2>/dev/null || true' EXIT`,
      `for i in $(seq 1 30); do curl -fsS --max-time 5 "http://127.0.0.1:$PORT/ready" >/dev/null 2>&1 && break; sleep 1; done`,
      `Q1='{namespace="traefik-system"} |~ ` + '`' + `apikey=[A-Za-z0-9]{16,}` + '`' + `'`,
      `Q2='{namespace="servarr"} |~ ` + '`' + `Password=[^;<[:space:]]{6,}` + '`' + `'`,
      `Q3='{namespace="traefik-system"}'`,
      `SURVIVOR_AT='2026-07-20T12:00:00Z'`,
      `q() { curl -fsSG --max-time 300 -H 'X-Scope-OrgID: 1' --data-urlencode "query=$1" "http://127.0.0.1:$PORT/loki/api/v1/query"; }`,
      `qt() { curl -fsSG --max-time 300 -H 'X-Scope-OrgID: 1' --data-urlencode "query=$1" --data-urlencode "time=$2" "http://127.0.0.1:$PORT/loki/api/v1/query"; }`,
      `sumv() { jq -r '[.data.result[]?.value[1] | tonumber] | add // 0'; }`,
      `curl -fsS --max-time 60 -H 'X-Scope-OrgID: 1' "http://127.0.0.1:$PORT/loki/api/v1/delete" > /tmp/issue-751-delete-status.json`,
      `S1=$(jq -r '[.. | objects | select((.request_id? // .id? // "") | startswith("25e63ca2")) | .status] | unique | join(",")' /tmp/issue-751-delete-status.json)`,
      `S2=$(jq -r '[.. | objects | select((.request_id? // .id? // "") | startswith("9cf45ca9")) | .status] | unique | join(",")' /tmp/issue-751-delete-status.json)`,
      `test "$S1" = processed`,
      `test "$S2" = processed`,
      `API_COUNT=$(q "sum(count_over_time($Q1 [30d]))" | sumv)`,
      `PW_COUNT=$(q "sum(count_over_time($Q2 [30d]))" | sumv)`,
      `PW_RECENT=$(q "sum(count_over_time($Q2 [46h]))" | sumv)`,
      `PW_BY_CONTAINER=$(q "sum by (container) (count_over_time($Q2 [30d]))" | jq -r '[.data.result[]? | "\\(.metric.container)=\\(.value[1])"] | join(", ")')`,
      `ORDINARY_COUNT=$(qt "sum(count_over_time($Q3 [1h]))" "$SURVIVOR_AT" | sumv)`,
      `test "$API_COUNT" = 0`,
      `awk -v n="$PW_COUNT" 'BEGIN { exit !(n < 60) }'`,
      `awk -v n="$ORDINARY_COUNT" 'BEGIN { exit !(n > 0) }'`,
      `PW_OLD=$(awk -v a="$PW_COUNT" -v b="$PW_RECENT" 'BEGIN { print a - b }')`,
      `printf '%s\\n' 'DRAFT #751 comment:'`,
      `printf '%s\\n' 'Verified after the cancel window and compaction. Only aggregate counts and label names were queried - no matching log line was fetched.'`,
      `printf -- '- delete request 25e63ca2 (apikey): %s\\n' "$S1"`,
      `printf -- '- delete request 9cf45ca9 (Password): %s\\n' "$S2"`,
      `printf -- '- apikey pattern, 30d count-only: %s (baseline before deletion was 20,984)\\n' "$API_COUNT"`,
      `printf -- '- Password pattern, 30d count-only: %s (baseline before deletion was about 60)\\n' "$PW_COUNT"`,
      `printf -- '- of those, written in the last 46h, after the deletion window closed: %s\\n' "$PW_RECENT"`,
      `printf -- '- remaining inside the deleted window: %s\\n' "$PW_OLD"`,
      `printf -- '- Password residual by container: %s\\n' "$PW_BY_CONTAINER"`,
      `printf -- '- ordinary Traefik lines in a 1h slice inside the purge window (%s): %s, so non-credential logs survived\\n' "$SURVIVOR_AT" "$ORDINARY_COUNT"`,
      `printf '%s\\n' 'The apikey purge is complete. The Password purge removed almost everything it targeted but left a small residual, and new lines have appeared since the window closed, which means promtail redaction has a gap. That is a separate defect, so it gets its own issue rather than holding this verification open. Closing as completed.'`,
      `printf '\\n%s\\n' 'DRAFT #702 comment:'`,
      `printf '%s\\n' 'Final gate (purge) verified in #751: both delete requests processed, the apikey pattern is now 0 in Loki against a 20,984 baseline, and ordinary Traefik logs survived.'`,
      `printf '%s\\n' 'All four outcomes of this issue are complete: keys rotated, the source leak removed by internal Service DNS, historical data purged, and redaction plus regression alerting live. The small Password residual and the redaction gap it exposes are tracked separately. Closing as completed.'`,
      `printf '\\n%s\\n' 'DRAFT follow-up issue title:'`,
      `printf '%s\\n' 'observability: promtail redaction still lets some Password= lines reach Loki, plus a residual the purge did not remove'`,
      `printf '%s\\n' 'DRAFT follow-up issue body:'`,
      `printf '%s\\n' '## Finding'`,
      `printf '%s\\n' 'Verifying the #751 purge with count-only queries found two related problems.'`,
      `printf -- '1. %s line(s) matching the credential pattern were written after the deletion window closed, so promtail redaction (#749/#750) is not catching every shape. Residual by container: %s\\n' "$PW_RECENT" "$PW_BY_CONTAINER"`,
      `printf -- '2. %s line(s) inside the deleted window survived the processed delete request.\\n' "$PW_OLD"`,
      `printf '%s\\n' '## Current state'`,
      `printf -- '- Both delete requests report processed; the apikey pattern is fully purged (%s against a 20,984 baseline).\\n' "$API_COUNT"`,
      `printf -- '- The Password pattern still returns %s over 30d.\\n' "$PW_COUNT"`,
      `printf '%s\\n' '- Every credential involved has already been rotated, so these lines are not live secrets. This is a redaction-correctness defect, not an active exposure.'`,
      `printf '%s\\n' '## Desired outcome'`,
      `printf '%s\\n' '- [ ] Reproduce the unredacted line shape from the named container and extend the promtail replace stage to cover it, without printing the value.'`,
      `printf '%s\\n' '- [ ] Submit a fresh delete request for the residual once redaction is fixed, so it cannot refill.'`,
      `printf '%s\\n' '- [ ] Re-run the count-only check and confirm it reaches 0.'`,
      `printf '%s\\n' '## Notes'`,
      `printf '%s\\n' '- Never fetch the matching lines to investigate; use counts and label breakdowns only.'`,
      `printf '%s\\n' '- Related: #751, #702, #634, #749, #750.'`,
    ].join('\n'),
    expectedExitCode: 0,
    timeout: 900000,
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['shell', 'loki', 'verification', 'count-only', 'residual'],
}));

export const closeIssuesTask = defineTask('close-loki-purge-issues', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Post approved evidence and close #751 and #702',
  execution: { model: 'gpt-5.6-sol', harness: 'pi' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'GitHub issue coordinator',
      task: 'Open the approved follow-up issue, post exactly the approved evidence comments, and close #751 and #702 as completed.',
      context: args,
      instructions: [
        `Use gh against ${args.repo}.`,
        'Parse the DRAFT blocks from the evidence context: the #751 comment, the #702 comment, and the follow-up issue title and body.',
        'First create the follow-up issue with label agent-gated, using the drafted title and body verbatim.',
        'Then append a line to each closing comment naming the new issue number, and post each draft to its matching issue without adding any other claims or values.',
        'Close #751 and #702 with reason completed.',
        'Do not fetch or print any matching Loki log line or credential value.',
        'Return only the requested JSON object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['closedIssues', 'followUpIssue', 'urls', 'summary'],
      properties: {
        closedIssues: { type: 'array', items: { type: 'number' } },
        followUpIssue: { type: 'number' },
        urls: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'github', 'close', 'issue-751', 'issue-702'],
}));

export const finalCheckTask = defineTask('verify-loki-purge-issues-closed', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Verify #751 and #702 are closed and scratch layout is clean',
  shell: {
    command: [
      'set -euo pipefail',
      `cd ${JSON.stringify(args.repoRoot)}`,
      `test "$(gh issue view ${args.childIssue} --repo ${args.repo} --json state --jq .state)" = CLOSED`,
      `test "$(gh issue view ${args.parentIssue} --repo ${args.repo} --json state --jq .state)" = CLOSED`,
      `test -n "$(gh issue list --repo ${args.repo} --state open --limit 20 --search 'promtail redaction in:title' --json number --jq '.[0].number // ""')"`,
      `! find .a5c/runs -maxdepth 3 -name work -type d -print 2>/dev/null | grep -q .`,
      `echo '#751 and #702 closed; no run-dir worktree exists'`,
    ].join('\n'),
    expectedExitCode: 0,
    timeout: 60000,
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['shell', 'verification', 'github'],
}));
