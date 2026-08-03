/**
 * @process specializations/devops-sre-platform/incident-response
 * @description Deliver Epaflix issue #135: confirm Sonarr2 (anime) hunt behaviour after the
 *   huntarr -> newtarr migration (#131/#132), where the hunt defaults changed from
 *   seasons_packs/3600 to episodes/900, and DECIDE whether to restore the old defaults in
 *   newtarr's JSON /config or keep the v1.0.0 defaults.
 *
 *   Framing this as a structured investigation (detect/triage -> investigate -> decide ->
 *   conditional mitigation -> verify -> post-incident) borrowed from the incident-response
 *   methodology, because the underlying concern is the known Sonarr2 importBlocked race
 *   (memory: project_sonarr2_huntarr_race) potentially being worsened by a more aggressive
 *   hunt cadence.
 *
 *   KEY INSIGHT (drives the design): the migration landed 2026-05-31 and today is 2026-06-06.
 *   At a 900s cadence that is hundreds of hunt cycles already elapsed, so the "observe over the
 *   next several hunt cycles" soak window in the issue has EFFECTIVELY ALREADY PASSED. We
 *   therefore observe the accumulated post-migration state NOW (importBlocked rate since the
 *   migration vs the documented pre-migration baseline) instead of literally sleeping for days.
 *
 *   Surfaces:
 *     - LIVE READ-ONLY: newtarr per-instance hunt config in the /config PVC (confirm the ACTUAL
 *       hunt_missing_mode + sleep_duration for the sonarr2 instance, not just the issue's claim),
 *       Sonarr2 importBlocked queue + history since the migration, newtarr hunt logs, Cleanuparr
 *       activity against sonarr2.
 *     - LIVE MUTATION (CONDITIONAL, gated): if the owner chooses "restore", edit newtarr's JSON
 *       /config for the sonarr2 instance (hunt_missing_mode=seasons_packs, sleep_duration=3600)
 *       and restart the pod. This is PVC runtime state, NOT git -> a declarative follow-up is
 *       opened (mirrors #174 for proxy_auth_bypass).
 *
 *   Breakpoints (user breakpointTolerance=low, alwaysBreakOn=[destructive-git, deploy]): a single
 *   DECISION+APPLY gate (the decision is also the authorization for the live /config mutation,
 *   which is a deploy-class change), plus a conditional anomaly gate if the apply verification
 *   fails. Read-only gather/analyze carry no breakpoint.
 *
 * @inputs { repoRoot, repo, issue, ns, newtarrApp, sonarr2, migrationDate,
 *           claimedOldMode, claimedOldSleep, claimedNewMode, claimedNewSleep }
 * @outputs { success, decision, raceWorsened, configChanged, appliedMode, appliedSleep,
 *            issueState, followUpIssues }
 *
 * @agent general-purpose (kubectl / sonarr API / curl / gh executor + verifier)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

const KUBECTL_HINT =
  'kubectl works DIRECTLY from repoRoot against the cluster (no SSH prefix needed). The newtarr ' +
  'app config lives in the /config PVC of the newtarr pod (newtarr v1.0.0 = JSON files, e.g. ' +
  '/config/sonarr.json or per-instance JSON + state dirs; the old huntarr SQLite huntarr.db may ' +
  'still be present on the volume but is NOT consumed by v1.0.0). Sonarr2 API key + URL are ' +
  'derivable from the live sonarr2 deployment (env / ConfigMap / the app config.xml in its pod). ' +
  'NOTE: newtarr per-app config may store the qBittorrent password in plaintext — treat any config ' +
  'JSON you read as sensitive and NEVER print secrets.';

// ---------------------------------------------------------------------------
// PHASE 1 — Gather live state (READ-ONLY).
// ---------------------------------------------------------------------------
const gatherTask = defineTask('gather-state', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Gather live newtarr hunt config + Sonarr2 importBlocked state since the migration',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Kubernetes/servarr SRE investigating an automation cadence change on the Epaflix k3s cluster',
      task:
        'Collect the exact live evidence needed to judge issue #' + args.issue + ': did the newtarr ' +
        'hunt-default change (claimed ' + args.claimedOldMode + '/' + args.claimedOldSleep + ' -> ' +
        args.claimedNewMode + '/' + args.claimedNewSleep + ') worsen the known Sonarr2 importBlocked ' +
        'race? DO NOT mutate anything. The migration landed ' + args.migrationDate + '.',
      context: { ...args, kubectlHint: KUBECTL_HINT },
      instructions: [
        KUBECTL_HINT,
        'NEWTARR ACTUAL CONFIG: exec into the newtarr pod (`kubectl -n ' + args.ns + ' exec deploy/' + args.newtarrApp + ' -- sh -c "..."`). Locate the JSON config files under /config (e.g. ls -R /config, cat the sonarr/radarr app config JSON). Record the ACTUAL hunt_missing_mode and sleep_duration (and hunt_missing_items, monitored_only) PER configured instance — there are typically THREE Sonarr-type instances (Sonarr HD, Sonarr2 anime, Radarr). Identify WHICH instance is Sonarr2 (anime) by its URL/name. The issue CLAIMS episodes/900 — confirm or correct that against the live file. Also note any leftover huntarr.db.',
        'SONARR2 IMPORTBLOCKED NOW: get the sonarr2 API key+base URL from the live sonarr2 pod (config.xml or env), then GET /api/v3/queue?pageSize=200&includeUnknownSeriesItems=true (and /api/v3/queue/details). Count rows with trackedDownloadState=importBlocked / trackedDownloadStatus=warning, capture their statusMessages titles (look for "Not a Custom Format upgrade", "One or more episodes expected ... not imported"), downloadIds, and the ADDED timestamp of each (so we can tell pre- vs post-' + args.migrationDate + ' residue apart).',
        'POST-MIGRATION RATE: use Sonarr2 history (GET /api/v3/history?eventType=grabbed and =downloadFolderImported and the importBlocked/failed events, pageSize large, sorted by date desc) to count how many NEW importBlocked/blocked-grab situations appeared AFTER ' + args.migrationDate + ' vs the documented pre-migration baseline (the May residue from project_sonarr2_huntarr_race was ~25 ep rows from one series; the Cleanuparr orphan work 2026-05-31 cleaned a batch). Distinguish stale March/May residue from genuinely new post-migration events.',
        'NEWTARR HUNT EVIDENCE: from newtarr logs (`kubectl -n ' + args.ns + ' logs deploy/' + args.newtarrApp + ' --since=168h | tail -n 400` and any /config/logs) confirm the real hunt cadence actually firing against the sonarr2 instance (interval between SeasonSearch/EpisodeSearch commands) and whether it is per-episode or per-season-pack in practice.',
        'CLEANUPARR COVERAGE: confirm whether Cleanuparr (deploy/cleanuparr in ' + args.ns + ') QueueCleaner has been striking sonarr2 importBlocked items — read its failed_import_patterns config and recent logs. (Per memory, its pattern list was historically too narrow to catch the "Not a Custom Format upgrade" status.)',
        'Return ONLY structured JSON facts with EXACT numbers and timestamps — not a recommendation yet.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['newtarrConfig', 'sonarr2Queue', 'postMigrationEvents', 'huntCadenceObserved', 'cleanuparrCoverage', 'summary'],
      properties: {
        newtarrConfig: { type: 'object' },
        sonarr2Queue: { type: 'object' },
        postMigrationEvents: { type: 'object' },
        huntCadenceObserved: { type: 'object' },
        cleanuparrCoverage: { type: 'object' },
        anomalies: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// PHASE 2 — Analyze + recommend (READ-ONLY, refine loop).
// ---------------------------------------------------------------------------
const analyzeTask = defineTask('analyze-race', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Decide if the race is worsened and recommend restore-vs-keep with evidence',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Reliability engineer reasoning about a media-automation feedback loop',
      task:
        'Using ONLY the gathered facts, judge whether the episodes/' + args.claimedNewSleep + ' newtarr ' +
        'defaults WORSENED, were NEUTRAL to, or IMPROVED the Sonarr2 importBlocked race vs the prior ' +
        args.claimedOldMode + '/' + args.claimedOldSleep + ' behaviour, and recommend whether to RESTORE ' +
        'seasons_packs/' + args.claimedOldSleep + ' in newtarr or KEEP the v1.0.0 defaults.',
      context: { ...args, feedback: args.feedback },
      instructions: [
        'Base the verdict on the EVIDENCE in gathered facts, not on theory alone. The theoretical concern: episodes/900 is a more frequent (15min vs 60min) and per-episode hunt -> more API-triggered searches -> more chances to race Sonarr2 add-search and produce the duplicate/"Not a Custom Format upgrade" stuck rows. But weigh that against the ACTUAL post-migration importBlocked count: if (near) zero NEW importBlocked rows appeared since ' + args.migrationDate + ', the race is NOT worsened in practice even if the cadence is more aggressive.',
        'Separate stale residue from new events: do not count March/May rows that predate the migration as evidence of newtarr making things worse.',
        'Acknowledge the real stickiness fix is Cleanuparr pattern coverage (the cadence governs the GRAB rate; Cleanuparr governs whether stuck rows ever clear) — but the issue asks specifically about the hunt params, so make a clear call on those and note the Cleanuparr angle as context/follow-up.',
        'Produce a CONCRETE proposed change if recommending restore: the EXACT newtarr JSON edit (which instance(s), which keys, old->new values). Decide whether the change should target ONLY the sonarr2 (anime) instance (where the race lives) or all instances, and justify it.',
        'Give a confidence level and the single strongest piece of evidence for the verdict.',
        'If prior feedback is in context (a rejected recommendation), incorporate it.',
        'Return ONLY structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['raceVerdict', 'recommendation', 'proposedChange', 'confidence', 'strongestEvidence', 'cleanuparrNote', 'summary'],
      properties: {
        raceVerdict: { type: 'string', enum: ['worsened', 'neutral', 'improved', 'inconclusive'] },
        recommendation: { type: 'string', enum: ['restore', 'keep', 'keep-and-watch'] },
        proposedChange: { type: 'object' },
        confidence: { type: 'string' },
        strongestEvidence: { type: 'string' },
        cleanuparrNote: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// PHASE 3 — Apply the config change (LIVE; CONDITIONAL on the owner choosing restore).
// ---------------------------------------------------------------------------
const applyTask = defineTask('apply-config', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Edit newtarr /config hunt params for the Sonarr2 instance and restart the pod',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE applying an approved newtarr runtime /config change over kubectl',
      task:
        'Apply EXACTLY the approved proposed change to newtarr\'s JSON /config (the owner chose to ' +
        'RESTORE the prior hunt behaviour). This is a runtime PVC change, not git.',
      context: { ...args, kubectlHint: KUBECTL_HINT },
      instructions: [
        KUBECTL_HINT,
        'Apply the approved change: ' + JSON.stringify(args.proposedChange) + '. First RE-READ the target JSON file in the newtarr pod and back it up to a timestamped copy inside /config before editing (e.g. cp file file.bak-issue135). Then edit ONLY the approved keys (hunt_missing_mode + sleep_duration, and any explicitly approved companions) for the approved instance(s). Use a precise in-pod edit (python3 -c json load/dump, or sed on the exact key) so unrelated keys are untouched. Preserve any plaintext secrets in the file verbatim; never print them.',
        'Restart newtarr so it reloads /config: `kubectl -n ' + args.ns + ' rollout restart deploy/' + args.newtarrApp + '` then `kubectl -n ' + args.ns + ' rollout status deploy/' + args.newtarrApp + ' --timeout=180s`. The config PVC persists across the restart.',
        'VERIFY the change took: re-read the JSON file post-restart and confirm the new values; confirm pod Running 1/1; from the newtarr logs confirm the next hunt cycle against the sonarr2 instance now uses the restored cadence/mode (or at minimum that the app loaded the config without error).',
        'Return ONLY structured JSON with before/after values and the backup path.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['applied', 'beforeValues', 'afterValues', 'backupPath', 'podHealthy', 'reloadConfirmed', 'summary'],
      properties: {
        applied: { type: 'boolean' },
        beforeValues: { type: 'object' },
        afterValues: { type: 'object' },
        backupPath: { type: 'string' },
        podHealthy: { type: 'boolean' },
        reloadConfirmed: { type: 'boolean' },
        anomalies: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// PHASE 4 — Closeout: record on #135, open follow-ups, update or close the issue.
// ---------------------------------------------------------------------------
const closeoutTask = defineTask('closeout', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Comment findings + decision on #135, open follow-ups, set issue state',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Maintainer closing out an investigation per Epaflix conventions',
      task: 'Record the investigation outcome and decision on issue #' + args.issue + ' and open warranted follow-ups.',
      context: { ...args },
      instructions: [
        'Comment on #' + args.issue + ' (`gh issue comment`) with: the ACTUAL observed newtarr hunt params for the Sonarr2 instance (correcting the issue claim if needed), the post-' + args.migrationDate + ' importBlocked count vs the pre-migration baseline, the race verdict (' + (args.analysis ? args.analysis.raceVerdict : 'see analysis') + '), the DECISION taken (' + args.decision + '), and — if a config change was applied — the before/after values and that newtarr reloaded cleanly. Use plain technical English. Refer to any affected series ONLY by seriesId, never by show/release name (repo convention).',
        'ISSUE STATE: if the decision resolves the issue (race not worsened + a clear keep/restore decision made and applied), CLOSE #' + args.issue + ' with `gh issue close` and a one-line reason. If the verdict was inconclusive or the owner chose keep-and-watch, leave it OPEN and add a short note on what further observation is pending.',
        'FOLLOW-UPS on ' + args.repo + ' (use the `## Finding / ## Current state / ## Desired outcome / ## Notes` shape, cross-link #' + args.issue + ', #131): open an issue to make newtarr hunt params DECLARATIVE (they live in the PVC /config, not git — same gap class as #174 for proxy_auth_bypass) IF a config change was applied or is anticipated. Open/append a Cleanuparr pattern-coverage hardening issue ("Not a Custom Format upgrade" / "One or more episodes expected ... not imported") ONLY if the gathered evidence shows that gap still exists and no existing open issue already tracks it (check first with `gh issue list`). Only open genuinely warranted issues; do not duplicate.',
        'Return ONLY structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['commented', 'issueState', 'followUpIssues', 'summary'],
      properties: {
        commented: { type: 'boolean' },
        issueState: { type: 'string' },
        followUpIssues: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// Orchestration
// ---------------------------------------------------------------------------
export async function process(inputs, ctx) {
  const cfg = {
    repoRoot: '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
    repo: 'SpyrosPsarras/epaflix',
    issue: '135',
    ns: 'servarr',
    newtarrApp: 'newtarr',
    sonarr2: 'sonarr2',
    migrationDate: '2026-05-31',
    claimedOldMode: 'seasons_packs',
    claimedOldSleep: 3600,
    claimedNewMode: 'episodes',
    claimedNewSleep: 900,
    ...inputs,
  };

  ctx.log('info', '#135 Sonarr2 hunt behaviour: gather -> analyze -> decide[BP] -> (conditional) apply -> closeout');

  // PHASE 1 — gather (read-only).
  const facts = await ctx.task(gatherTask, { ...cfg });
  ctx.log('info', `Gather: ${facts.summary}`);

  // PHASE 2 — analyze with a refine/review loop. The gate here is the DECISION + APPLY authorization.
  let analysis, lastFeedback = null, decision = null, applyChange = false;
  for (let attempt = 0; attempt < 3; attempt++) {
    analysis = await ctx.task(analyzeTask, {
      ...cfg, facts, feedback: lastFeedback || undefined, attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    ctx.log('info', `Analyze: verdict=${analysis.raceVerdict} rec=${analysis.recommendation} conf=${analysis.confidence}`);

    const gate = await ctx.breakpoint({
      question:
        'Issue #' + cfg.issue + ' — Sonarr2 hunt behaviour after the newtarr migration.\n\n' +
        'Observed (since ' + cfg.migrationDate + '): ' + facts.summary + '\n\n' +
        'Race verdict: ' + analysis.raceVerdict + ' (confidence ' + analysis.confidence + ')\n' +
        'Strongest evidence: ' + analysis.strongestEvidence + '\n' +
        'Recommendation: ' + analysis.recommendation + '\n' +
        'Proposed config change (if restoring): ' + JSON.stringify(analysis.proposedChange) + '\n' +
        'Cleanuparr note: ' + analysis.cleanuparrNote + '\n\n' +
        'Summary: ' + analysis.summary + '\n\n' +
        'DECIDE: "Restore" edits newtarr /config LIVE (seasons_packs/' + cfg.claimedOldSleep +
        ' for the Sonarr2 instance per the proposed change) and restarts the pod. "Keep defaults" ' +
        'makes no change and closes the issue. Choose:',
      options: ['Restore seasons_packs/3600', 'Keep v1.0.0 defaults', 'Request more analysis', 'Abort'],
      expert: 'owner',
      tags: ['decision-gate', 'deploy', 'approval-gate'],
      previousFeedback: lastFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });

    const r = (gate.response || '').toLowerCase();
    if (!gate.approved && r.includes('abort')) {
      ctx.log('warn', 'Owner aborted.');
      return { success: false, reason: 'aborted', facts, analysis };
    }
    if (r.includes('more analysis') || r.includes('request')) {
      lastFeedback = gate.feedback || gate.response || 'Provide deeper analysis';
      continue;
    }
    if (r.includes('restore')) { decision = 'restore'; applyChange = true; break; }
    if (r.includes('keep')) { decision = 'keep'; applyChange = false; break; }
    // Any other approved response: treat as needing clarification -> re-loop.
    lastFeedback = gate.feedback || gate.response || 'Clarify the decision';
  }

  if (!decision) {
    ctx.log('warn', 'No clear decision after 3 attempts; leaving issue open.');
    decision = 'undecided';
  }

  // PHASE 3 — conditional apply (LIVE), gated by the decision above.
  let apply = null;
  if (applyChange) {
    apply = await ctx.task(applyTask, { ...cfg, proposedChange: analysis.proposedChange });
    ctx.log('info', `Apply: applied=${apply.applied} healthy=${apply.podHealthy} reload=${apply.reloadConfirmed}`);
    if (!apply.applied || !apply.podHealthy) {
      const ag = await ctx.breakpoint({
        question:
          'The newtarr /config change did not apply cleanly.\n' +
          'applied=' + apply.applied + ' podHealthy=' + apply.podHealthy + ' reload=' + apply.reloadConfirmed + '\n' +
          'anomalies: ' + JSON.stringify(apply.anomalies) + '\n' +
          'before=' + JSON.stringify(apply.beforeValues) + ' after=' + JSON.stringify(apply.afterValues) + '\n' +
          'backup=' + apply.backupPath + '\n\nHow to proceed?',
        options: ['Continue to closeout (accept)', 'Stop here'],
        expert: 'owner',
        tags: ['anomaly-gate'],
      });
      if (!ag.approved || (ag.response || '').toLowerCase().includes('stop')) {
        return { success: false, reason: 'apply-failed', decision, facts, analysis, apply };
      }
    }
  }

  // PHASE 4 — closeout (record on #135, follow-ups, set state).
  const close = await ctx.task(closeoutTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo, issue: cfg.issue, migrationDate: cfg.migrationDate,
    decision, analysis, facts, apply,
  });
  ctx.log('info', `Closeout: #${cfg.issue}=${close.issueState}; follow-ups=${JSON.stringify(close.followUpIssues)}`);

  return {
    success: true,
    decision,
    raceWorsened: analysis ? analysis.raceVerdict === 'worsened' : null,
    configChanged: !!(apply && apply.applied),
    appliedMode: apply && apply.afterValues ? apply.afterValues.hunt_missing_mode : null,
    appliedSleep: apply && apply.afterValues ? apply.afterValues.sleep_duration : null,
    issueState: close.issueState,
    followUpIssues: close.followUpIssues,
  };
}
