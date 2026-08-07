#!/usr/bin/env node
/**
 * @process project/diagnose-sonarr-blind-queue-834
 * @description Sonarr grabs releases but its queue reports zero rows (#834, the
 * #631 shape recurring). Establish whether the rows appear late, whether the
 * grabs are reachable at all, and whether the known TrackedDownloadService
 * stale-cache defect (#705) explains it, before proposing any remedy.
 *
 * Every read is by API field or count. Raw Sonarr and Traefik log lines are
 * never fetched: they carry API keys (#602, #702).
 *
 * Composition references:
 * - specializations/devops-sre-platform/incident-response.js
 * - methodologies/shared/root-cause-diagnosis.js
 * - methodologies/superpowers/verification-before-completion.js
 *
 * @skill kubernetes-ops specializations/devops-sre-platform/skills/kubernetes-ops/SKILL.md
 * @agent sre-expert specializations/devops-sre-platform/agents/sre-expert/AGENT.md
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

export async function process(inputs, ctx) {
  const {
    repo = 'SpyrosPsarras/epaflix',
    repoRoot = '/home/spy/Documents/Epaflix/k3s-swarm-proxmox-triage',
    issue = 834,
    namespace = 'servarr',
  } = inputs || {};

  const evidence = await ctx.task(probeSonarrQueueTask, { repo, repoRoot, issue, namespace });

  const remedy = await ctx.breakpoint({
    title: `#${issue} - Sonarr queue is blind, choose the remedy`,
    question:
      'Measured evidence is below. The known workaround for this defect is a Sonarr restart, which is a live app action ' +
      'and can interrupt an in-flight import, so it needs your word.\n\n' +
      evidence.stdout +
      '\n\nReply "restart" to restart Sonarr and re-measure, "wait" to record the evidence and leave it running, or "stop".',
    options: ['Restart Sonarr and re-measure', 'Record only, do not touch it', 'Stop'],
    context: {
      runId: ctx.runId,
      issue,
      liveEffect: 'kubectl rollout restart deployment/sonarr - brief downtime, may interrupt an in-flight import',
      evidence: evidence.stdout,
    },
    expert: 'owner',
    tags: ['approval-gate', 'deploy'],
  });

  if (!remedy.approved) {
    return { success: false, stopped: true, reason: remedy.response || 'no remedy approved', evidence };
  }

  const applied = await ctx.task(applyRemedyTask, {
    repo, repoRoot, issue, namespace,
    ownerDecision: remedy.response || '',
    evidence: evidence.stdout,
  });

  const verified = await ctx.task(verifySonarrQueueTask, { repo, repoRoot, issue, namespace });

  return {
    success: verified.exitCode === 0,
    issue,
    outcome: applied.outcome,
    queueRowsAfter: applied.queueRowsAfter,
    commentUrl: applied.commentUrl,
    verified,
    metadata: { processId: 'project/diagnose-sonarr-blind-queue-834', timestamp: ctx.now() },
  };
}

export const probeSonarrQueueTask = defineTask('probe-sonarr-queue', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Measure the Sonarr queue against its own grabs, by API field only',
  shell: {
    command: [
      'set -uo pipefail',
      `cd ${JSON.stringify(args.repoRoot)}`,
      `SK=$(sops -d --extract '["sonarr_api_key"]' .github/instructions/secrets.enc.yaml)`,
      `echo "sonarr key length: \${#SK}"`,
      `P=38995`,
      `kubectl -n ${args.namespace} port-forward service/sonarr "$P:8989" >/tmp/i834-pf.log 2>&1 &`,
      `PF=$!; trap 'kill $PF 2>/dev/null || true' EXIT`,
      `for i in $(seq 1 30); do curl -fsS --max-time 5 "http://127.0.0.1:$P/ping" >/dev/null 2>&1 && break; sleep 1; done`,
      `api() { curl -fsS --max-time 30 -H "X-Api-Key: $SK" "http://127.0.0.1:$P/api/v3/$1"; }`,
      `echo '=== queue, several shapes (a row hidden by a filter is not a blind queue) ==='`,
      `api 'queue?pageSize=200' | jq -r '"plain            totalRecords=\\(.totalRecords)"'`,
      `api 'queue?pageSize=200&includeUnknownSeriesItems=true' | jq -r '"unknown-included totalRecords=\\(.totalRecords)"'`,
      `api 'queue/status' | jq -r '"queue/status     totalCount=\\(.totalCount) errors=\\(.errors) warnings=\\(.warnings) unknownCount=\\(.unknownCount // 0)"'`,
      `echo`,
      `echo '=== what Sonarr thinks it grabbed recently (ids only, no titles) ==='`,
      `api 'history?pageSize=10&eventType=1' | jq -r '.records[] | "\\(.date) grabbed seriesId=\\(.seriesId) downloadId=\\(.downloadId[:8] // "none")"'`,
      `echo`,
      `echo '=== are those downloadIds known to the client and to the queue ==='`,
      `api 'history?pageSize=10&eventType=1' | jq -r '[.records[].downloadId] | map(select(. != null)) | unique | "distinct downloadIds in the last 10 grabs: \\(length)"'`,
      `echo`,
      `echo '=== is anything stuck in Sonarr own view ==='`,
      `api 'health' | jq -r 'if length == 0 then "health: clean" else (.[] | "health: \\(.type) \\(.source)") end'`,
      `api 'command?pageSize=5' | jq -r '[.records[]? | select(.status != "completed")] | "commands not completed: \\(length)"' 2>/dev/null || echo 'command endpoint shape differs'`,
      `echo`,
      `echo '=== how long has the process been up (a stale in-memory cache needs a restart to clear) ==='`,
      `kubectl -n ${args.namespace} get pods -l app=sonarr -o jsonpath='{range .items[*]}{.metadata.name}{" started="}{.status.startTime}{" restarts="}{.status.containerStatuses[0].restartCount}{"\\n"}{end}'`,
      `echo`,
      `echo '=== does the sibling instance read its queue fine (same image, same pattern) ==='`,
      `S2=$(sops -d --extract '["sonarr2_api_key"]' .github/instructions/secrets.enc.yaml)`,
      `P2=38996`,
      `kubectl -n ${args.namespace} port-forward service/sonarr2 "$P2:8989" >/tmp/i834-pf2.log 2>&1 &`,
      `PF2=$!; trap 'kill $PF $PF2 2>/dev/null || true' EXIT`,
      `for i in $(seq 1 30); do curl -fsS --max-time 5 "http://127.0.0.1:$P2/ping" >/dev/null 2>&1 && break; sleep 1; done`,
      `curl -fsS --max-time 30 -H "X-Api-Key: $S2" "http://127.0.0.1:$P2/api/v3/queue?pageSize=50" | jq -r '"sonarr2 queue totalRecords=\\(.totalRecords)"'`,
      `unset SK S2`,
      `echo`,
      `echo '=== the census verdict that raised this ==='`,
      `kubectl -n ${args.namespace} logs "$(kubectl -n ${args.namespace} get pods --no-headers | grep '^orphan-census-verify2' | tail -1 | awk '{print $1}')" 2>/dev/null | grep -E 'arr queue rows|recently active' || echo 'verification pod already reaped'`,
    ].join('\n'),
    expectedExitCode: 0,
    timeout: 300000,
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['shell', 'sonarr', 'diagnosis', 'issue-834'],
}));

export const applyRemedyTask = defineTask('apply-sonarr-remedy', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Apply the owner remedy and record the outcome on the issue',
  execution: { model: 'gpt-5.6-sol', harness: 'pi' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Cluster operator working a live *arr defect',
      task: 'Do exactly what the owner chose, measure the result, and record it on the issue.',
      context: args,
      instructions: [
        `Honor the owner decision verbatim: ${JSON.stringify(args.ownerDecision)}.`,
        `If it is a restart: kubectl -n ${args.namespace} rollout restart deployment/sonarr, wait for the rollout to finish, then re-read the queue the same way the probe did and report the before and after row counts.`,
        'If it is record-only: change nothing live.',
        'Never fetch raw log lines from Sonarr or Traefik; they carry API keys. Use API fields and counts.',
        'Never print a secret. Printing a length is fine.',
        `Post one comment on #${args.issue} with the measured before and after, and say plainly whether the queue recovered, stayed blind, or is inconclusive.`,
        'If it recovered, say that a restart is a workaround and not a fix, and that the durable fix belongs to the upstream defect in #705.',
        'Do not close the issue.',
        'Log the commands and outputs to .history/ in the main repo path.',
        'Return only the requested JSON object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['outcome', 'queueRowsBefore', 'queueRowsAfter', 'commentUrl', 'summary'],
      properties: {
        outcome: { type: 'string' },
        queueRowsBefore: { type: 'number' },
        queueRowsAfter: { type: 'number' },
        commentUrl: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'remedy', 'issue-834'],
}));

export const verifySonarrQueueTask = defineTask('verify-sonarr-queue-outcome', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Verify Sonarr is serving and the issue carries the measurement',
  shell: {
    command: [
      'set -euo pipefail',
      `cd ${JSON.stringify(args.repoRoot)}`,
      `kubectl -n ${args.namespace} rollout status deployment/sonarr --timeout=120s >/dev/null`,
      `test "$(kubectl -n ${args.namespace} get deploy sonarr -o jsonpath='{.status.readyReplicas}')" = 1`,
      `test "$(gh issue view ${args.issue} --repo ${args.repo} --json state --jq .state)" = OPEN`,
      `test "$(gh issue view ${args.issue} --repo ${args.repo} --json comments --jq '.comments|length')" -ge 1`,
      `git status --short | grep -v '^?? ' | grep . && exit 1 || true`,
      `echo 'sonarr is serving, the measurement is recorded on the issue, worktree clean'`,
    ].join('\n'),
    expectedExitCode: 0,
    timeout: 300000,
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['shell', 'verification', 'issue-834'],
}));
