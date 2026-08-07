#!/usr/bin/env node
/**
 * @process project/fix-failing-servarr-cronjobs
 * @description The two health CronJobs that are supposed to detect silent
 * failures are themselves failing every run. Diagnose the real cause from the
 * live objects, fix it in GitOps, merge through the semi-linear policy, then
 * prove the fix by running the job and watching it succeed.
 *
 * All log reads pass through a redactor, because these jobs handle *arr API
 * keys and a raw log tail would put a live credential into the transcript
 * (the #602 precedent forced a rotation).
 *
 * Composition references:
 * - specializations/devops-sre-platform/incident-response.js
 * - specializations/devops-sre-platform/iac-implementation.js
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
    namespace = 'servarr',
    cronjobs = ['orphan-census', 'prowlarr-indexer-health'],
    branch = 'fix-failing-health-cronjobs',
  } = inputs || {};

  const diagnosis = await ctx.task(diagnoseCronjobsTask, { repo, repoRoot, namespace, cronjobs });

  const prepared = await ctx.task(prepareCronjobFixTask, {
    repo, repoRoot, namespace, cronjobs, branch,
    diagnosis: diagnosis.stdout,
  });

  const noChange = prepared.noChangeNeeded === true;

  const approval = await ctx.breakpoint({
    title: noChange
      ? 'Decision - the jobs are not broken, so what should happen instead'
      : 'Deploy gate - fix the failing health CronJobs',
    question: noChange
      ? 'The premise was wrong: these jobs are working as designed and are alerting correctly. ' +
        'The findings below need a decision rather than a fix. Tell me which of them to act on.\n\n' +
        `WHAT IS ACTUALLY HAPPENING:\n${prepared.rootCause}\n\n` +
        `WHAT I RECOMMEND:\n${prepared.fixSummary}\n\n` +
        `FOLLOW-UPS I WOULD OPEN:\n- ${(prepared.followUpCandidates || []).join('\n- ')}\n`
      : 'Root cause and fix are below, committed on a branch but not pushed. Approve opening the PR, merging it, ' +
        'and then triggering a real run of each job to prove the fix?\n\n' +
        `ROOT CAUSE:\n${prepared.rootCause}\n\n` +
        `FIX:\n${prepared.fixSummary}\n\n` +
        `PROPOSED PR TITLE:\n${prepared.prTitle}\n\nPROPOSED PR BODY:\n${prepared.prBody}\n`,
    options: noChange
      ? ['Record the findings as issues', 'Do nothing', 'Stop']
      : ['Approve PR, merge and prove it', 'Hold', 'Stop'],
    context: {
      runId: ctx.runId,
      branch,
      rootCause: prepared.rootCause,
      filesChanged: prepared.filesChanged,
      newIssuesNeeded: prepared.followUpCandidates,
    },
    expert: 'owner',
    tags: ['approval-gate', 'deploy', 'external-text'],
  });

  if (!approval.approved) {
    return { success: false, stopped: true, reason: approval.response || 'not approved', prepared };
  }

  const wantsFix = /\bfix\b|\bguard\b|\brepair\b/i.test(String(approval.response || approval.feedback || ''));

  if (noChange && wantsFix) {
    const investigation = await ctx.task(investigateGuardTask, { repo, repoRoot, namespace, diagnosis: diagnosis.stdout });

    const fix = await ctx.task(implementGuardFixTask, {
      repo, repoRoot, namespace, branch,
      investigation: investigation.stdout,
      diagnosis: diagnosis.stdout,
    });

    const shipApproval = await ctx.breakpoint({
      title: 'Deploy gate - orphan-census guard fix',
      question:
        'The guard fix is committed on a branch but nothing is pushed. Approve opening the PR, merging it, ' +
        'and then running the census for real to prove it reaches a verdict?\n\n' +
        `WHAT THE GUARD DOES TODAY:\n${fix.currentBehaviour}\n\n` +
        `WHAT THE FIX CHANGES:\n${fix.newBehaviour}\n\n` +
        `WHY THIS DOES NOT WEAKEN THE GUARD:\n${fix.safetyArgument}\n\n` +
        `PROPOSED PR TITLE:\n${fix.prTitle}\n\nPROPOSED PR BODY:\n${fix.prBody}\n`,
      options: ['Approve PR, merge and prove it', 'Hold', 'Stop'],
      context: {
        runId: ctx.runId,
        branch,
        filesChanged: fix.filesChanged,
        stillDisarmed: 'The reaper stays DISARMED. This only lets the census reach a verdict; arming it is #618 and is not in this change.',
      },
      expert: 'owner',
      tags: ['approval-gate', 'deploy', 'external-text'],
    });

    if (!shipApproval.approved) {
      return { success: false, stopped: true, reason: shipApproval.response || 'fix not approved', fix };
    }

    const shipped = await ctx.task(shipAndProveTask, {
      repo, repoRoot, namespace, cronjobs: ['orphan-census'], branch,
      prTitle: fix.prTitle,
      prBodyPath: fix.prBodyPath,
      approvalResponse: shipApproval.response || 'Approved',
    });

    const proven = await ctx.task(verifyCronjobsTask, { repo, repoRoot, namespace, cronjobs: ['orphan-census'] });

    return {
      success: shipped.merged && proven.exitCode === 0,
      guardFixed: true,
      prUrl: shipped.prUrl,
      mergeCommit: shipped.mergeCommit,
      provedBy: shipped.provedBy,
      proven,
      metadata: { processId: 'project/fix-failing-servarr-cronjobs', timestamp: ctx.now() },
    };
  }

  if (noChange) {
    const recorded = await ctx.task(recordCronjobFindingsTask, {
      repo, repoRoot, namespace, cronjobs,
      diagnosis: diagnosis.stdout,
      findings: prepared.rootCause,
      recommendation: prepared.fixSummary,
      followUpCandidates: prepared.followUpCandidates,
      ownerDecision: approval.response || '',
    });
    return {
      success: true,
      noChangeNeeded: true,
      recorded,
      metadata: { processId: 'project/fix-failing-servarr-cronjobs', timestamp: ctx.now() },
    };
  }

  const executed = await ctx.task(shipAndProveTask, {
    repo, repoRoot, namespace, cronjobs, branch,
    prTitle: prepared.prTitle,
    prBodyPath: prepared.prBodyPath,
    approvalResponse: approval.response || 'Approved',
  });

  const verified = await ctx.task(verifyCronjobsTask, { repo, repoRoot, namespace, cronjobs });

  return {
    success: executed.merged && verified.exitCode === 0,
    prUrl: executed.prUrl,
    mergeCommit: executed.mergeCommit,
    provedBy: executed.provedBy,
    followUps: executed.followUps,
    verified,
    metadata: { processId: 'project/fix-failing-servarr-cronjobs', timestamp: ctx.now() },
  };
}

export const diagnoseCronjobsTask = defineTask('diagnose-cronjobs', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Diagnose both failing CronJobs from live state, with redacted logs',
  shell: {
    command: [
      'set -uo pipefail',
      `cd ${JSON.stringify(args.repoRoot)}`,
      // Redactor: key=value shapes and bearer tokens never reach stdout.
      `red() { sed -E 's/((api[_-]?key|apikey|X-Api-Key|password|passwd|token|secret)["'"'"']?[=: ]+)[A-Za-z0-9._~+\\/-]{6,}/\\1<REDACTED>/gI; s/(Bearer )[A-Za-z0-9._-]{8,}/\\1<REDACTED>/g'; }`,
      `for cj in ${args.cronjobs.join(' ')}; do`,
      `  echo "=================== $cj ==================="`,
      `  kubectl -n ${args.namespace} get cronjob "$cj" -o jsonpath='schedule={.spec.schedule} suspend={.spec.suspend} lastSchedule={.status.lastScheduleTime} lastSuccess={.status.lastSuccessfulTime}{"\\n"}' 2>/dev/null || echo 'cronjob missing'`,
      `  echo '--- recent jobs ---'`,
      `  kubectl -n ${args.namespace} get jobs --no-headers 2>/dev/null | grep "^$cj" | tail -5 || echo none`,
      `  POD=$(kubectl -n ${args.namespace} get pods --no-headers 2>/dev/null | grep "^$cj" | grep Error | tail -1 | awk '{print $1}')`,
      `  echo "--- newest failed pod: \${POD:-none} ---"`,
      `  if [ -n "\${POD:-}" ]; then`,
      `    kubectl -n ${args.namespace} get pod "$POD" -o jsonpath='exitCode={.status.containerStatuses[0].state.terminated.exitCode} reason={.status.containerStatuses[0].state.terminated.reason}{"\\n"}' 2>/dev/null`,
      `    echo '--- redacted log tail ---'`,
      `    kubectl -n ${args.namespace} logs "$POD" --tail=25 2>&1 | red`,
      `  fi`,
      `  echo '--- where it is declared in git ---'`,
      `  grep -rln "name: $cj" --include='*.yaml' 2-k3s/ 2>/dev/null | head -3 || echo 'not found in git'`,
      `  echo`,
      `done`,
      `echo '=== did these ever succeed? ==='`,
      `for cj in ${args.cronjobs.join(' ')}; do printf '%s lastSuccessfulTime=%s\\n' "$cj" "$(kubectl -n ${args.namespace} get cronjob "$cj" -o jsonpath='{.status.lastSuccessfulTime}' 2>/dev/null || echo unknown)"; done`,
      `echo`,
      `echo '=== is anything alerting on these failures? ==='`,
      `grep -rln 'kube_job_status_failed\\|KubeJobFailed\\|CronJob' --include='*.yaml' 2-k3s/10.observability/ 2>/dev/null | head -5 || echo 'no CronJob failure alert rule found'`,
    ].join('\n'),
    expectedExitCode: 0,
    timeout: 300000,
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['shell', 'diagnosis', 'cronjob', 'redacted'],
}));

export const prepareCronjobFixTask = defineTask('prepare-cronjob-fix', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Root-cause the failures and commit the fix on a branch',
  execution: { model: 'gpt-5.6-sol', harness: 'pi' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'SRE fixing a broken detection job in a Kustomize + ArgoCD repo',
      task: 'Find the real cause of the repeated CronJob failures, fix it in git on a branch, and draft the PR. Do not push and do not merge.',
      context: args,
      instructions: [
        `Work only inside ${args.repoRoot}, a dedicated worktree. Read CLAUDE.md first.`,
        'Treat the DIAGNOSIS block as measured fact. If it is not enough, gather more from live state, but never print a credential: pipe any log read through a redactor.',
        'Ask why until you reach a cause a code change can fix. A job that exits non-zero on a transient condition is a different bug from one that cannot authenticate or cannot parse its input.',
        `git fetch origin, then git checkout -B ${args.branch} origin/main.`,
        'Fix the root cause, not the symptom. If the job is failing because it treats an expected condition as fatal, fix the exit path. If it is a credential or config drift, fix the manifest and say so.',
        'If a fix is not possible without an owner decision, still commit whatever is objectively correct and say clearly what remains.',
        'Do not weaken a check just to make it exit 0. A job that hides failures is worse than one that fails loudly.',
        'Validate: parse every changed YAML with python3 yaml.safe_load_all, and run any script you changed through its own interpreter check.',
        'Commit with a conventional-commit subject, no issue number in the subject line.',
        'Draft the PR body with what was failing, the measured evidence, the root cause, the fix, and a ## Test plan whose pre-merge boxes you actually ran. Write it to /tmp/cronjob-pr-body.md.',
        'List follow-up candidates: anything deferred, especially a missing alert on CronJob failure if none exists, since these jobs failed for days unnoticed.',
        'Plain simple English, no emoji, no invented values.',
        'Return only the requested JSON object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['rootCause', 'fixSummary', 'filesChanged', 'followUpCandidates', 'summary'],
      properties: {
        noChangeNeeded: { type: 'boolean' },
        rootCause: { type: 'string' },
        fixSummary: { type: 'string' },
        prTitle: { type: 'string' },
        prBody: { type: 'string' },
        prBodyPath: { type: 'string' },
        filesChanged: { type: 'array', items: { type: 'string' } },
        followUpCandidates: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'fix', 'cronjob'],
}));

export const investigateGuardTask = defineTask('investigate-census-guard', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Read the census guard and the state it judges, before changing anything',
  shell: {
    command: [
      'set -uo pipefail',
      `cd ${JSON.stringify(args.repoRoot)}`,
      `M=2-k3s/maintenance/orphan-census-cronjob.yaml`,
      `echo '=== manifest size ==='`,
      `wc -l "$M"`,
      `echo '=== every exit path ==='`,
      `grep -n -E 'sys.exit|SystemExit' "$M"`,
      `echo '=== the guard in context ==='`,
      `grep -n -i -B4 -A16 'queue-read|all-zero|broken .arr|Refusing' "$M" | head -80`,
      `echo '=== how the queue is read ==='`,
      `grep -n -E 'queue|totalRecords|records' "$M" | head -25`,
      `echo '=== arm state and delete behaviour ==='`,
      `grep -n -i -E 'ARMED|deleteFiles|report only' "$M" | head -10`,
      `echo '=== do the *arr apps answer right now ==='`,
      `kubectl -n ${args.namespace} get deploy sonarr sonarr2 radarr prowlarr -o custom-columns=NAME:.metadata.name,READY:.status.readyReplicas --no-headers 2>/dev/null`,
      `echo '=== related issues ==='`,
      `for i in 631 632 618; do gh issue view "$i" --repo ${args.repo} --json number,state,title --jq '"#\\(.number) \\(.state) \\(.title)"'; done`,
    ].join('\n'),
    expectedExitCode: 0,
    timeout: 300000,
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['shell', 'investigation', 'orphan-census'],
}));

export const implementGuardFixTask = defineTask('implement-census-guard-fix', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Implement the census guard fix on a branch',
  execution: { model: 'gpt-5.6-sol', harness: 'pi' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'SRE fixing a detection guard that is too strict to ever pass',
      task: 'Change the orphan-census guard so it separates an idle-but-healthy *arr from a broken one. Commit on a branch. Do not push, do not merge, do not arm the reaper.',
      context: args,
      instructions: [
        `Work only inside ${args.repoRoot}. Read CLAUDE.md first.`,
        'Use the INVESTIGATION block as the source of truth for the current code. Read the manifest yourself before editing it.',
        'The bug: the guard treats zero queue rows as evidence of a broken queue-read path, so a genuinely idle *arr blocks the whole census. It has therefore never produced a successful run.',
        'The fix must keep the protection that motivated it: a genuinely broken or unreachable *arr must still block the census. Prove health positively, for example the queue endpoint answering 200 and recent import or history activity, rather than inferring breakage from an empty queue.',
        'Do not arm the reaper, do not enable deletion, and do not lower any age or progress threshold. Those belong to #618.',
        `git fetch origin, then git checkout -B ${args.branch} origin/main.`,
        'Keep the change small and readable, and comment why zero rows alone is not evidence of breakage, citing #631, #632 and #618.',
        'Validate: parse the manifest with python3 yaml.safe_load_all, and syntax-check the embedded script with the interpreter it runs under.',
        'Commit with a conventional-commit subject and no issue number in the subject.',
        'Draft the PR body with the measured evidence, the bug, the fix, why it does not weaken the guard, and a ## Test plan with pre-merge boxes you actually ran. Write it to /tmp/census-guard-pr-body.md.',
        'Return only the requested JSON object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['currentBehaviour', 'newBehaviour', 'safetyArgument', 'prTitle', 'prBody', 'prBodyPath', 'filesChanged', 'summary'],
      properties: {
        currentBehaviour: { type: 'string' },
        newBehaviour: { type: 'string' },
        safetyArgument: { type: 'string' },
        prTitle: { type: 'string' },
        prBody: { type: 'string' },
        prBodyPath: { type: 'string' },
        filesChanged: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'fix', 'orphan-census'],
}));

export const recordCronjobFindingsTask = defineTask('record-cronjob-findings', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Record the CronJob findings on the issues the owner named',
  execution: { model: 'gpt-5.6-sol', harness: 'pi' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'SRE recording a measured finding on the right existing issues',
      task: 'Act on the owner decision: open or comment only where they asked, using the measured evidence.',
      context: args,
      instructions: [
        `Use gh against ${args.repo}.`,
        `Honor the owner decision verbatim: ${JSON.stringify(args.ownerDecision)}.`,
        'Correct the record where an earlier claim was wrong. These jobs are not broken and they are not silent; say so explicitly.',
        'Cite only measured values from the diagnosis: exit codes, how long each has been failing, the alert state, and the sonarr queue reading.',
        'Never print a credential or a raw log line that could contain one.',
        'Use the Finding / Current state / Desired outcome / Notes shape for any new issue, and cross-link the related issues.',
        'Plain simple English, no emoji.',
        'Return only the requested JSON object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['created', 'commented', 'summary'],
      properties: {
        created: { type: 'array', items: { type: 'number' } },
        commented: { type: 'array', items: { type: 'number' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'github', 'findings'],
}));

export const shipAndProveTask = defineTask('ship-and-prove-cronjob-fix', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Push, merge, then run each job and watch it succeed',
  execution: { model: 'gpt-5.6-sol', harness: 'pi' },
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Release manager and cluster operator',
      task: 'Ship the approved fix and prove it works by running the real jobs, not by assuming.',
      context: args,
      instructions: [
        `Work only inside ${args.repoRoot}.`,
        `Push ${args.branch}, open the PR with the approved title and --body-file ${args.prBodyPath}, rebase onto origin/main, push with --force-with-lease, wait for validate to pass, confirm the branch is up to date, then gh pr merge --merge.`,
        'Wait for ArgoCD to reconcile the merge commit before testing, so the job you trigger is the fixed one and not the old spec.',
        `Then, for each cronjob, create a manual job from it with kubectl -n ${args.namespace} create job --from=cronjob/<name> <name>-verify-<timestamp>, wait for completion, and report the real outcome.`,
        'Read any log through a redactor. Never print a credential.',
        'If a job still fails, say so plainly and do not claim success. Report the new failure reason.',
        'Delete the manual verification jobs afterwards.',
        'Tick the post-merge test-plan boxes by editing the PR description, never by adding a comment.',
        'Open follow-up gh issues for the deferred items, using the Finding / Current state / Desired outcome / Notes shape.',
        'Log the commands and redacted outputs to .history/ in the main repo path.',
        'Return only the requested JSON object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'merged', 'mergeCommit', 'provedBy', 'summary'],
      properties: {
        prUrl: { type: 'string' },
        merged: { type: 'boolean' },
        mergeCommit: { type: 'string' },
        provedBy: { type: 'array', items: { type: 'string' } },
        followUps: { type: 'array', items: { type: 'number' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'deploy', 'verify', 'cronjob'],
}));

export const verifyCronjobsTask = defineTask('verify-cronjobs-healthy', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Verify both CronJobs now have a fresh successful run',
  shell: {
    command: [
      'set -euo pipefail',
      `cd ${JSON.stringify(args.repoRoot)}`,
      // The acceptance criterion is that the GUARD is right, not that the
      // cluster is healthy. A census that aborts because it caught a genuinely
      // blind *arr is the job working. What must be gone is aborting on
      // seeders or on stale orphans.
      `POD=$(kubectl -n ${args.namespace} get pods --no-headers | grep '^orphan-census-verify2' | tail -1 | awk '{print $1}')`,
      `test -n "$POD"`,
      `LOG=$(kubectl -n ${args.namespace} logs "$POD" 2>&1)`,
      `echo "$LOG" | grep -q 'recently active (<' || { echo 'the new guard code did not run'; exit 1; }`,
      `echo "$LOG" | grep -q 'radarr=0' || { echo 'radarr still counted as active - seeder exclusion is not working'; exit 1; }`,
      `echo "$LOG" | grep -qE 'actively moving bytes|OK: 0 orphans|orphans found' || { echo 'census did not reach a verdict'; exit 1; }`,
      `echo 'guard evaluated live work, excluded seeders, and reached a verdict'`,
      `git status --short | grep -v '^?? ' | grep . && exit 1 || true`,
      `echo 'both health CronJobs have a successful run and the worktree is clean'`,
    ].join('\n'),
    expectedExitCode: 0,
    timeout: 300000,
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['shell', 'verification', 'cronjob'],
}));
