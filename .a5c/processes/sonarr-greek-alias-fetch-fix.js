/**
 * @process specializations/devops-sre-platform/sonarr-greek-alias-fetch-fix
 * @description Owner reported Sonarr HD seriesId 273 (tvdb 473263, a Greek daily show) "not fetching new
 *   episodes". DIAGNOSED LIVE (read-only): it WAS auto-grabbing from the Magico indexer through S01E44
 *   (last grab 2026-05-27, release titled in GREEK "ΜΠΑΜΠΑ Σ'ΑΓΑΠΩ S01E44 720p"), then stopped — the 5
 *   newest aired+monitored episodes S01E45..E49 (1-9 Jun) have 0 files. The episodes ARE available on
 *   Magico under the GREEK title (raw Prowlarr search shows ΜΠΑΜΠΑ Σ'ΑΓΑΠΩ S01E45..E49 720p), but a Latin
 *   "dad i love you" search returns 0. Sonarr's series has NO alternate titles (alternateTitles: [],
 *   cleanTitle "dadiloveyou") and Skyhook/TVDB returns aliases:[] for tvdb 473263 — so Sonarr can no longer
 *   match the Greek-titled releases (the Greek alias that let E1..E44 match was apparently dropped on a
 *   metadata refresh). Root cause = MISSING GREEK ALIAS, not availability, not monitoring (all monitored),
 *   not quality profile.
 *
 *   Fix has two parts: (1) IMMEDIATE backlog — grab the 5 available Greek-titled releases now (interactive
 *   grab via Prowlarr/Sonarr release-push, with Sonarr manual-import fallback) so the missing episodes
 *   download; (2) DURABLE — restore the Greek title as a recognized alias so RSS/auto-search keeps matching
 *   the daily releases. Sonarr aliases come from the metadata source, so the durable fix is to add the Greek
 *   translation/alias for tvdb 473263 on TheTVDB then RefreshSeries (after the Skyhook cache catches up — a
 *   known lag). Editing TVDB needs a fresh authenticated session cookie (the prior one was deleted); if no
 *   cookie is present at cookieFile, the durable step is DOCUMENTED for the owner instead of applied.
 *
 *   SECURITY/SCRUB: any TVDB cookie lives ONLY in the git-ignored cookieFile (never in .a5c/processes/,
 *   commits, issues). Refer to the series ONLY as seriesId 273 / tvdb 473263 in committed git/issues.
 *
 * @inputs { repoRoot, repo, namespace, sonarrDeploy, prowlarrDeploy, seriesId, tvdbId, magicoIndexerId, missingEpIds, cookieFile }
 * @outputs { success, rootCause, immediateGrabbed, importsObserved, durableFixApplied, durableFixPlan, issueOpened, followUpIssues, summary }
 *
 * @agent general-purpose (Sonarr+Prowlarr API, kubectl/exec, optional authenticated TVDB edit, gh, verification)
 * @skill systematic-debugging superpowers:systematic-debugging
 * @skill verification-before-completion superpowers:verification-before-completion
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

// ---------------------------------------------------------------------------
// PHASE 0 — confirm root cause + author fix plan (READ-ONLY)
// ---------------------------------------------------------------------------
const investigateTask = defineTask('investigate', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Confirm Greek-alias fetch-failure root cause for seriesId ' + (args && args.seriesId) + ' (tvdb ' + (args && args.tvdbId) + ') + fix plan (READ-ONLY)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr SRE diagnosing why a Greek daily series stopped auto-fetching new episodes in Sonarr HD',
      task:
        'Comprehensively CONFIRM (read-only) the root cause for seriesId ' + args.seriesId + ' (tvdb ' + args.tvdbId + ') not fetching new episodes, and author the exact fix plan (immediate backlog grab + durable alias fix). Namespace=' + args.namespace + '. Do NOT change anything yet.',
      context: { ...args },
      instructions: [
        'SCRUB: refer to the series ONLY as seriesId ' + args.seriesId + ' / tvdb ' + args.tvdbId + ' in output.json / committed text (read the title from the API only for your own matching).',
        'Sonarr (HD) API helper: `kubectl -n ' + args.namespace + ' exec deploy/' + args.sonarrDeploy + ' -- sh -c \'KEY=$(grep -o "<ApiKey>[^<]*" /config/config.xml | sed "s/<ApiKey>//"); curl -s http://localhost:8989/api/v3/...\'`.',
        'CONFIRM monitoring + missing: GET /series/' + args.seriesId + ' (series + season monitored=true, qualityProfileId) and /episode?seriesId=' + args.seriesId + '. List the aired+monitored+missing episodes (expected S01E45..E49). Confirm alternateTitles is EMPTY.',
        'CONFIRM availability vs matching: GET /release?episodeId=<missing> for a couple of the missing episodes -> expect 0 releases (Sonarr searches the English title). Then raw Prowlarr search on the Magico indexer (id ' + args.magicoIndexerId + ') for the GREEK title: `kubectl -n ' + args.namespace + ' exec deploy/' + args.prowlarrDeploy + ' -- sh -c \'KEY=$(grep -o "<ApiKey>[^<]*" /config/config.xml | sed "s/<ApiKey>//"); curl -s "http://localhost:9696/api/v1/search?query=%CE%9C%CE%A0%CE%91%CE%9C%CE%A0%CE%91&indexerIds=' + args.magicoIndexerId + '&type=search&limit=60&apikey=$KEY"\'` -> expect the Greek-titled S01E45..E49 releases present. Record the exact release titles + their Prowlarr guid/downloadUrl/indexerId (needed to grab them).',
        'CONFIRM the alias gap: GET https://skyhook.sonarr.tv/v1/tvdb/shows/en/' + args.tvdbId + ' (Sonarr metadata) and confirm aliases:[] (no Greek alias). Note how E1..E44 were grabbed (history eventType=grabbed, indexer Magico) — the Greek alias was evidently present before and dropped on a refresh.',
        'TRY THE CHEAP FIX FIRST (still read-only-ish/no-harm): trigger RefreshSeries and re-check whether alternateTitles repopulates with the Greek alias (it likely will NOT, since Skyhook has none — but verify). Record result.',
        'AUTHOR the fix plan: (1) IMMEDIATE — grab the 5 Greek-titled releases for S01E45..E49 now (preferred: Sonarr release push POST /api/v3/release with the Prowlarr guid+indexerId mapped to the episode; fallback: Prowlarr grab -> download -> Sonarr manual import mapping each file to its episodeId). (2) DURABLE — add the Greek title "ΜΠΑΜΠΑ Σ\'ΑΓΑΠΩ" as a Greek (ell) translation/alias for tvdb ' + args.tvdbId + ' on TheTVDB, then RefreshSeries after Skyhook syncs, so RSS+auto-search match the daily releases. Note the durable step needs a fresh TVDB session cookie at ' + args.cookieFile + ' (check if present) and is subject to Skyhook propagation lag.',
        'Save raw captures under tasks/' + taskCtx.effectId + '/. Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['monitored', 'alternateTitlesEmpty', 'missingEpisodes', 'magicoReleases', 'aliasGapConfirmed', 'refreshRestoredAlias', 'rootCause', 'immediatePlan', 'durablePlan', 'cookiePresent', 'summary'],
      properties: {
        monitored: { type: 'boolean' },
        alternateTitlesEmpty: { type: 'boolean' },
        missingEpisodes: { type: 'array', items: { type: 'object' } },
        magicoReleases: { type: 'array', items: { type: 'object' } },
        aliasGapConfirmed: { type: 'boolean' },
        refreshRestoredAlias: { type: 'boolean' },
        rootCause: { type: 'string' },
        immediatePlan: { type: 'object' },
        durablePlan: { type: 'object' },
        cookiePresent: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 1 — immediate: grab the available Greek-titled releases for the backlog (live)
// ---------------------------------------------------------------------------
const immediateGrabTask = defineTask('immediate-grab', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Grab the available Greek-titled S01E45..E49 releases for seriesId ' + (args && args.seriesId) + ' (live)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr SRE fetching the missing-episode backlog that Sonarr could not auto-match',
      task:
        'Grab the available Magico Greek-titled releases for the missing monitored episodes (S01E45..E49) of seriesId ' + args.seriesId + ' so they download and import. Verify downloads start.',
      context: { ...args },
      instructions: [
        'SCRUB: seriesId ' + args.seriesId + ' / tvdb ' + args.tvdbId + ' only. API keys via the config.xml grep pattern (Sonarr :8989, Prowlarr :9696).',
        'Use the release map from the investigation (' + JSON.stringify(args.immediatePlan || {}) + '). Per missing episode that has a matching Greek release:',
        'PREFERRED — Sonarr release push: POST /api/v3/release to Sonarr with the body containing the release guid + indexerId (and, if required, episodeId) so Sonarr grabs that specific release and sends it to the download client. Confirm a 2xx + that it appears in /api/v3/queue.',
        'FALLBACK — if Sonarr refuses to grab because it cannot map the title to the episode: grab via Prowlarr (POST /api/v1/search with the release guid to grab/push to the download client), let it download, then in Sonarr do a manual import: POST /api/v3/command {"name":"ManualImport", files:[{path, seriesId:' + args.seriesId + ', episodeIds:[<epId>], quality:...}]} (or the /api/v3/manualimport endpoint) mapping each downloaded file to the correct S01Exx episodeId. Be precise mapping release Exx -> episodeId.',
        'Do NOT change season/episode monitoring. Do NOT delete anything. Grab ONLY the 5 backlog episodes (E45..E49), not the whole series.',
        'Report per-episode: grabbed? queued? imported? Capture evidence (queue entries, history grabbed/imported events). It is OK if downloads are still in progress at the end — the goal is the grab pipeline is moving for the backlog.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['grabbedCount', 'queuedCount', 'importsObserved', 'method', 'perEpisode', 'summary'],
      properties: {
        grabbedCount: { type: 'number' },
        queuedCount: { type: 'number' },
        importsObserved: { type: 'number' },
        method: { type: 'string' },
        perEpisode: { type: 'array', items: { type: 'object' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 2 — durable fix: restore the Greek alias (TVDB edit if cookie present, else document)
// ---------------------------------------------------------------------------
const durableFixTask = defineTask('durable-fix', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Durable fix: restore the Greek alias for tvdb ' + (args && args.tvdbId) + ' so auto-fetch resumes',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr/metadata SRE making the daily auto-fetch durable by restoring the Greek title alias',
      task:
        'Make Sonarr able to match the Greek-titled daily releases going forward, by adding the Greek title alias for tvdb ' + args.tvdbId + ' on TheTVDB (if an authenticated cookie is available) then refreshing Sonarr; otherwise document the exact owner steps.',
      context: { ...args },
      instructions: [
        'SCRUB: seriesId ' + args.seriesId + ' / tvdb ' + args.tvdbId + ' only. NEVER print/commit any cookie.',
        'Check for a cookie at ' + args.cookieFile + '. If ABSENT: do NOT attempt a TVDB edit — instead produce the precise documented runbook for the owner (add the Greek title "ΜΠΑΜΠΑ Σ\'ΑΓΑΠΩ" as a Greek/ell alias or translation on the TheTVDB series page for tvdb ' + args.tvdbId + ', save, then RefreshSeries in Sonarr after the Skyhook cache updates) and set durableFixApplied=false, needsCookie=true.',
        'If a cookie IS present and valid (GET /auth/getuser): find the TheTVDB series slug for tvdb ' + args.tvdbId + ', open the alias/translation edit for the series, ADD the Greek title as an alias (Greek language). Verify it saved on the TVDB page. Then trigger Sonarr RefreshSeries for seriesId ' + args.seriesId + ' and check whether alternateTitles now includes the Greek title (it may lag until Skyhook re-syncs from TVDB — report honestly; do not claim success if Sonarr alternateTitles is still empty).',
        'After (or instead of) the alias, VERIFY the durable outcome: once Sonarr has the Greek alias, an interactive search GET /release?episodeId=<missing> should return the Greek Magico releases as grabbable. Report whether matching now works or is pending Skyhook propagation.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['durableFixApplied', 'needsCookie', 'tvdbAliasAdded', 'sonarrAliasPresent', 'matchingWorksNow', 'pendingPropagation', 'ownerRunbook', 'summary'],
      properties: {
        durableFixApplied: { type: 'boolean' },
        needsCookie: { type: 'boolean' },
        tvdbAliasAdded: { type: 'boolean' },
        sonarrAliasPresent: { type: 'boolean' },
        matchingWorksNow: { type: 'boolean' },
        pendingPropagation: { type: 'boolean' },
        ownerRunbook: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 3 — wrap up: scrubbed tracking issue + follow-ups
// ---------------------------------------------------------------------------
const wrapupTask = defineTask('wrapup', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Document the Greek-alias fetch fix for seriesId ' + (args && args.seriesId) + ' (scrubbed) + follow-ups',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE recording the Greek-alias fetch fix per the Epaflix repo Critical Rules',
      task: 'Persist the outcome as a gh issue on ' + args.repo + ', scrubbed of the show title. Only state what is actually true.',
      context: { ...args },
      instructions: [
        'SCRUB (mandatory): NEVER write the show/release title. Refer to the series ONLY as "Sonarr seriesId ' + args.seriesId + ' (tvdb ' + args.tvdbId + ')". The Greek release title may be referenced generically as "the native (Greek) release title" — do not quote the actual title.',
        'STATE OF PLAY (accurate): root cause = Sonarr had NO Greek alias for the series (Skyhook aliases:[]), so it could not match the Greek-titled Magico releases for new episodes; auto-fetch worked through S01E44 then stopped at S01E45 (1 Jun). Availability/monitoring/quality were fine. ' +
          'Immediate: grabbed ' + (args.grabbedCount != null ? args.grabbedCount : '?') + ' of 5 backlog episodes (E45..E49); imports observed=' + (args.importsObserved != null ? args.importsObserved : '?') + '. ' +
          'Durable: ' + (args.durableFixApplied ? 'Greek alias added on TVDB + Sonarr refresh (matching ' + (args.matchingWorksNow ? 'works now' : 'pending Skyhook propagation') + ')' : 'NOT applied — needs a fresh TVDB session cookie; documented owner runbook') + '.',
        'SEARCH existing issues first (`gh issue list --repo ' + args.repo + ' --state open --limit 100`) to avoid duplicates. OPEN a tracking issue (shape `## Finding` / `## Current state` / `## Desired outcome` / `## Notes`): document root cause (missing Greek alias on a daily Greek show on the Magico indexer), the immediate backlog result, and the durable fix.',
        'Follow-ups: (a) DURABLE alias on TVDB if not yet applied (owner provides a session cookie; same TVDB-edit capability used for tvdb 328975) — this is required or the daily show keeps failing to auto-fetch; (b) Skyhook propagation lag means even after the TVDB alias, Sonarr adoption may take hours; (c) this class of bug (Greek-only release titles needing an alias) likely affects OTHER Greek daily shows on Magico — consider an audit; (d) revert risk on TVDB alias edits. Cross-link the tvdb 328975 numbering issue (#257) as related metadata work.',
        'No git commits, no secrets, no titles. Return ONLY the structured JSON result (issue number/url).',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['trackingIssue', 'followUpIssues', 'summary'],
      properties: {
        trackingIssue: { type: 'string' },
        followUpIssues: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ===========================================================================
// ORCHESTRATION
// ===========================================================================
export async function process(inputs, ctx) {
  const cfg = {
    repoRoot: inputs.repoRoot || '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
    repo: inputs.repo || 'SpyrosPsarras/epaflix',
    namespace: inputs.namespace || 'servarr',
    sonarrDeploy: inputs.sonarrDeploy || 'sonarr',
    prowlarrDeploy: inputs.prowlarrDeploy || 'prowlarr',
    seriesId: inputs.seriesId || 273,
    tvdbId: inputs.tvdbId || 473263,
    magicoIndexerId: inputs.magicoIndexerId || 18,
    missingEpIds: inputs.missingEpIds || [7044, 7045, 7046, 7207, 7208],
    cookieFile: inputs.cookieFile || '/home/spy/Documents/Epaflix/k3s-swarm-proxmox/.a5c/runs/_secrets/tvdb-cookie.txt',
  };

  ctx.log('info', `Greek-alias fetch fix: Sonarr seriesId ${cfg.seriesId} (tvdb ${cfg.tvdbId})`);

  // PHASE 0 — investigate (read-only) + owner gate
  const diag = await ctx.task(investigateTask, { ...cfg });
  ctx.log('info', `Diagnosis: aliasGap=${diag.aliasGapConfirmed}; refreshRestoredAlias=${diag.refreshRestoredAlias}; magicoReleases=${(diag.magicoReleases || []).length}; cookiePresent=${diag.cookiePresent}`);

  const gateA = await ctx.breakpoint({
    question: 'Approve the fix? Root cause = missing Greek alias (Skyhook aliases:[]) so Sonarr cannot match Magico\'s Greek-titled releases. Plan: (1) grab the 5 backlog episodes E45-E49 now, (2) durable = restore the Greek alias on TVDB' + (diag.cookiePresent ? '' : ' (no cookie present -> document for owner)') + ' + refresh. Reply "abort" to stop after diagnosis.',
    title: 'Greek-alias Fetch Fix Gate',
    context: { runId: ctx.runId, rootCause: diag.rootCause, immediatePlan: diag.immediatePlan, durablePlan: diag.durablePlan, cookiePresent: diag.cookiePresent },
    expert: 'owner',
    tags: ['approval-gate'],
  });
  if (!gateA.approved || (gateA.response || '').toLowerCase().includes('abort')) {
    ctx.log('warn', 'Fix not approved — stopping after read-only diagnosis.');
    return { success: false, reason: 'fix-not-approved', rootCause: diag.rootCause, diag, feedback: gateA.response || '' };
  }

  // PHASE 1 — immediate backlog grab
  const grab = await ctx.task(immediateGrabTask, { ...cfg, immediatePlan: diag.immediatePlan, magicoReleases: diag.magicoReleases });
  ctx.log('info', `Immediate grab: grabbed=${grab.grabbedCount}; queued=${grab.queuedCount}; imports=${grab.importsObserved}; method=${grab.method}`);

  // PHASE 2 — durable alias fix (TVDB edit if cookie present, else document)
  const durable = await ctx.task(durableFixTask, { ...cfg, durablePlan: diag.durablePlan, cookiePresent: diag.cookiePresent });
  ctx.log('info', `Durable: applied=${durable.durableFixApplied}; needsCookie=${durable.needsCookie}; matchingWorksNow=${durable.matchingWorksNow}; pendingPropagation=${durable.pendingPropagation}`);

  // PHASE 3 gate — wrap-up (owner-gated; auto-approved in yolo)
  const gateB = await ctx.breakpoint({
    question: 'Diagnosis done + backlog grab attempted (grabbed ' + grab.grabbedCount + '/5) + durable fix ' + (durable.durableFixApplied ? 'applied' : 'documented (needs cookie)') + '. Approve WRAP-UP (scrubbed tracking issue + follow-ups)? Reply "stop here" to skip.',
    title: 'Greek-alias Fix Wrap-up Gate',
    context: { runId: ctx.runId, grabbedCount: grab.grabbedCount, durableFixApplied: durable.durableFixApplied },
    expert: 'owner',
    tags: ['approval-gate'],
  });
  if (!gateB.approved || (gateB.response || '').toLowerCase().includes('stop here')) {
    ctx.log('warn', 'Stopped before wrap-up by owner.');
    return { success: true, partial: true, reason: 'stopped-before-wrapup', rootCause: diag.rootCause, immediateGrabbed: grab.grabbedCount, durableFixApplied: durable.durableFixApplied };
  }

  const wrap = await ctx.task(wrapupTask, {
    ...cfg,
    grabbedCount: grab.grabbedCount,
    importsObserved: grab.importsObserved,
    durableFixApplied: durable.durableFixApplied,
    matchingWorksNow: durable.matchingWorksNow,
  });
  ctx.log('info', `Wrap-up: trackingIssue=${wrap.trackingIssue}; followUps=${JSON.stringify(wrap.followUpIssues)}`);

  return {
    success: true,
    rootCause: diag.rootCause,
    immediateGrabbed: grab.grabbedCount,
    importsObserved: grab.importsObserved,
    durableFixApplied: durable.durableFixApplied,
    durableFixPlan: durable.ownerRunbook,
    issueOpened: wrap.trackingIssue,
    followUpIssues: wrap.followUpIssues,
    summary: wrap.summary,
  };
}
