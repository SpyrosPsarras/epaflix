/**
 * @process specializations/devops-sre-platform/jellyfin-empty-anime-folder-fix
 * @description Owner-reported playback problem for an anime series in Jellyfin ("empty folder").
 *   DIAGNOSED LIVE (read-only) before authoring: the anime is Sonarr2 (anime instance) seriesId 2,
 *   tvdbId 348545 (title SCRUBBED — refer to it ONLY as seriesId 2 / tvdb 348545). Its on-disk folder
 *   /media/animes/<seriesId-2>/ EXISTS but is EMPTY (0 files); Sonarr2 reports episodeFileCount=0,
 *   sizeOnDisk=0 and has ZERO grab/import history for it. Seasons S3/S4/S5 are monitored=true, S0/S1/S2
 *   monitored=false; all seasons have 0 files. Root cause = the series was added but its monitored
 *   episodes were NEVER searched/grabbed/imported, so Jellyfin shows an empty, unplayable series entry.
 *   This is NOT corruption and NOT a path mismatch (the unified /media animes path and the legacy
 *   /mnt/k3s-animes export are the same ZFS data; both show the folder empty). Sonarr2 has 5 indexers
 *   (4 with automatic search incl an anime tracker), so a search can grab.
 *
 *   FIX (live, minimal, respects existing monitoring intent): trigger Sonarr2 to search the MONITORED
 *   missing episodes for seriesId 2 (S3/S4/S5), verify grabs enter the download queue (proving the
 *   search->grab->import pipeline works post-#195), and when episodes import, confirm files land under
 *   /media/animes/<seriesId-2>/ and trigger a Jellyfin library refresh so the series populates. Do NOT
 *   silently enable the unmonitored S1/S2 (owner content decision) and do NOT force-grab beyond what is
 *   monitored. Downloads are time-bounded/indexer-dependent: success = the fix pipeline is correctly
 *   initiated + verified working (or no-releases documented) + Jellyfin rescan handled + root cause and
 *   follow-ups recorded; remaining episodes fill in via Sonarr as they download.
 *
 *   IMPORTANT SECONDARY FINDING (follow-up, not today's playback fix): Jellyfin still mounts media via
 *   hostPath to the LEGACY pre-#195 split exports (/mnt/k3s-animes, /mnt/k3s-tvshows, /mnt/k3s-movies),
 *   NOT the unified servarr-media /media mount. It works now but will BREAK when issue #247 tears down
 *   those legacy exports/node mounts. Must be migrated to the unified /media before teardown.
 *
 * @inputs { repoRoot, repo, namespace, sonarr2Deploy, seriesId, tvdbId, monitoredSeasons }
 * @outputs { success, rootCause, searchTriggered, grabsObserved, importsObserved, jellyfinRescanned, jellyfinPlayable, issueOpened, followUpIssues, summary }
 *
 * @agent general-purpose (kubectl/exec, Sonarr2 + Jellyfin API, disk inspection, gh, verification)
 * @skill systematic-debugging superpowers:systematic-debugging
 * @skill verification-before-completion superpowers:verification-before-completion
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

// ---------------------------------------------------------------------------
// PHASE 0 — confirm root cause comprehensively + author fix plan (READ-ONLY)
// ---------------------------------------------------------------------------
const investigateTask = defineTask('investigate', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Confirm empty-anime-folder root cause (Sonarr2 seriesId ' + (args && args.seriesId) + ' / tvdb ' + (args && args.tvdbId) + ') + Jellyfin state + fix plan (READ-ONLY)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr/Jellyfin SRE on the Epaflix k3s cluster diagnosing an owner-reported anime playback ("empty folder") problem',
      task:
        'Comprehensively CONFIRM (read-only) why the anime Sonarr2 seriesId ' + args.seriesId + ' (tvdb ' + args.tvdbId + ') shows an empty folder / is unplayable in Jellyfin, ' +
        'and author the exact minimal fix plan. DO NOT change anything. Namespace=' + args.namespace + '.',
      context: { ...args },
      instructions: [
        'Apply systematic-debugging discipline: hard evidence only. SCRUB the show title from anything that will be committed/opened later — refer to it ONLY as seriesId ' + args.seriesId + ' / tvdb ' + args.tvdbId + '. You may read the title from the API for your own matching.',
        'SONARR2 series state: `kubectl -n ' + args.namespace + ' exec deploy/' + args.sonarr2Deploy + ' -- sh -c \'KEY=$(grep -o "<ApiKey>[^<]*" /config/config.xml | sed "s/<ApiKey>//"); curl -s http://localhost:8989/api/v3/series/' + args.seriesId + '?apikey=$KEY\'`. ' +
          'Record path, monitored, seriesType, qualityProfileId, and per-season monitored + episodeFileCount/totalEpisodeCount. Confirm episodeFileCount=0 overall. Also GET /api/v3/history/series?seriesId=' + args.seriesId + ' and confirm ZERO grab/import history (or record what history exists). GET /api/v3/queue and confirm seriesId ' + args.seriesId + ' is NOT already downloading.',
        'DISK (unified /media via Sonarr2): `kubectl -n ' + args.namespace + ' exec deploy/' + args.sonarr2Deploy + ' -- sh -c \'ls -la "<series.path>"; find "<series.path>" -type f | wc -l\'` — confirm the folder exists and is EMPTY (0 files). Use the exact series.path from the API.',
        'JELLYFIN side: confirm Jellyfin sees the same empty folder and how its mounts are wired. (a) `kubectl -n ' + args.namespace + ' get deploy/jellyfin -o yaml | grep -A3 hostPath` — RECORD that the media-animes/tvshows/movies volumes are hostPath to /mnt/k3s-animes, /mnt/k3s-tvshows, /mnt/k3s-movies (legacy pre-#195 exports), NOT the unified servarr-media /media PVC. (b) try `kubectl -n ' + args.namespace + ' exec deploy/jellyfin -- sh -c \'ls -la /media/animes/<folder>; find /media/animes/<folder> -type f | wc -l\'` (it may be slow — use a generous timeout, retry once); confirm jellyfin also sees 0 files (same ZFS data). (c) If you can reach the Jellyfin API (find its api key/token in /config or via the deployment env; Jellyfin listens on 8096), check whether the library has a stale/empty item for this series — optional, do not block on it.',
        'INDEXERS: GET /api/v3/indexer on Sonarr2 — confirm there are enabled indexers with enableAutomaticSearch=true (so a search can actually grab). Record their names/count (5 expected incl an anime tracker).',
        'STRAY FILES: `kubectl -n ' + args.namespace + ' exec deploy/' + args.sonarr2Deploy + ' -- sh -c \'find /media -iname "*<short-token>*" -type f 2>/dev/null | head\'` to confirm the episodes are not sitting un-imported somewhere else (e.g. /media/downloads). Distinguish the unrelated MOVIE (in /media/movies) from the TV series.',
        'AUTHOR the fix plan: (1) trigger Sonarr2 search for the MONITORED missing episodes of seriesId ' + args.seriesId + ' (the MissingEpisodeSearch/SeriesSearch command, or per-season SeasonSearch for the monitored seasons S3/S4/S5); do NOT enable S1/S2 monitoring (owner decision). (2) verify grabs enter the queue (proves pipeline). (3) when episodes import, confirm files under the series folder + trigger a Jellyfin library refresh so it populates. State the rollback (cancel queued grabs / blocklist if a bad release is grabbed — but a normal monitored search needs no rollback).',
        'RECORD the IMPORTANT follow-up: Jellyfin on legacy hostPath /mnt/k3s-* (will break at #247 teardown) -> migrate to unified /media. And note S1/S2 unmonitored as an owner decision.',
        'Save raw captures under tasks/' + taskCtx.effectId + '/. Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['seriesState', 'historyCount', 'folderEmpty', 'jellyfinMount', 'jellyfinSeesEmpty', 'indexers', 'strayFiles', 'rootCause', 'fixPlan', 'followUps', 'summary'],
      properties: {
        seriesState: { type: 'object' },
        historyCount: { type: 'number' },
        folderEmpty: { type: 'boolean' },
        jellyfinMount: { type: 'object' },
        jellyfinSeesEmpty: { type: ['boolean', 'string', 'null'] },
        indexers: { type: 'object' },
        strayFiles: { type: 'array', items: { type: 'string' } },
        rootCause: { type: 'string' },
        fixPlan: { type: 'object' },
        followUps: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 1 — trigger the Sonarr2 monitored-missing search (live)
// ---------------------------------------------------------------------------
const triggerFixTask = defineTask('trigger-fix', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Trigger Sonarr2 monitored-missing search for seriesId ' + (args && args.seriesId) + ' + observe initial grabs (live)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr SRE initiating the fix for the empty anime folder',
      task:
        'Trigger Sonarr2 to search the MONITORED missing episodes of seriesId ' + args.seriesId + ' (tvdb ' + args.tvdbId + ') and confirm the command is accepted + observe whether grabs begin. ' +
        'Respect existing monitoring: do NOT enable S1/S2; do NOT force-grab unmonitored content.',
      context: { ...args },
      instructions: [
        'SCRUB: refer to the series only as seriesId ' + args.seriesId + ' / tvdb ' + args.tvdbId + ' in any saved/committed text.',
        'API key: `kubectl -n ' + args.namespace + ' exec deploy/' + args.sonarr2Deploy + ' -- sh -c \'grep -o "<ApiKey>[^<]*" /config/config.xml | sed "s/<ApiKey>//"\'`. Sonarr2 listens on localhost:8989.',
        'TRIGGER the search via the Sonarr command API: POST /api/v3/command with body {"name":"MissingEpisodeSearch","seriesId":' + args.seriesId + '} OR, more targeted, {"name":"SeriesSearch","seriesId":' + args.seriesId + '} (SeriesSearch searches all monitored episodes of the series). ' +
          'Use curl inside the sonarr2 pod: `kubectl -n ' + args.namespace + ' exec deploy/' + args.sonarr2Deploy + ' -- sh -c \'KEY=$(...); curl -s -X POST "http://localhost:8989/api/v3/command?apikey=$KEY" -H "Content-Type: application/json" -d "{\\"name\\":\\"SeriesSearch\\",\\"seriesId\\":' + args.seriesId + '}"\'`. Capture the returned command id + status.',
        'POLL the command: GET /api/v3/command/<id> until status is completed/failed (or ~60-90s). Record the result.',
        'OBSERVE grabs: after the search completes, GET /api/v3/queue and GET /api/v3/history/series?seriesId=' + args.seriesId + ' — record any new grabbed releases (eventType=grabbed) and queued downloads for this series. It is OK if downloads are still queued/in-progress; the goal here is to confirm the search produced grabs (search->grab works). If NO releases were found by any indexer, record that explicitly (no-releases) — that is a legitimate, documented outcome (cannot fetch content that is not available).',
        'Do NOT modify season monitoring. Do NOT delete anything. Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['searchTriggered', 'commandId', 'commandStatus', 'grabsObserved', 'queuedCount', 'noReleasesFound', 'summary'],
      properties: {
        searchTriggered: { type: 'boolean' },
        commandId: { type: ['number', 'string', 'null'] },
        commandStatus: { type: ['string', 'null'] },
        grabsObserved: { type: 'number' },
        queuedCount: { type: 'number' },
        noReleasesFound: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 2 — verify pipeline + Jellyfin rescan (live, time-bounded)
// ---------------------------------------------------------------------------
const verifyTask = defineTask('verify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify download->import pipeline + trigger Jellyfin library refresh so seriesId ' + (args && args.seriesId) + ' populates',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr/Jellyfin SRE verifying the empty-folder fix end-to-end',
      task:
        'Verify the search->grab->download->import pipeline is working for seriesId ' + args.seriesId + ', and trigger a Jellyfin library refresh so the series populates once files land. ' +
        'Time-bounded: do not wait indefinitely for a full multi-season download.',
      context: { ...args },
      instructions: [
        'SCRUB: series referred to only as seriesId ' + args.seriesId + ' / tvdb ' + args.tvdbId + '.',
        'Prior trigger result: ' + JSON.stringify(args.trigger || {}) + '.',
        'POLL Sonarr2 for a BOUNDED window (~3-5 minutes, checking every ~30s): GET /api/v3/queue (series ' + args.seriesId + ' downloads progressing?), GET /api/v3/history/series?seriesId=' + args.seriesId + ' (any eventType=downloadFolderImported yet?), and GET /api/v3/series/' + args.seriesId + ' (episodeFileCount rising above 0?). Record the best state reached. ' +
          'If at least one episode IMPORTS within the window: confirm the file exists under the series folder (`kubectl -n ' + args.namespace + ' exec deploy/' + args.sonarr2Deploy + ' -- sh -c \'find "<series.path>" -type f | head\'`) and that it is a real media file (nonzero size).',
        'JELLYFIN refresh: trigger a Jellyfin library scan so it picks up new files. Find the Jellyfin API key/token (check /config/data or system config, or an env on the deployment) and POST /Library/Refresh (header X-Emby-Token or ?api_key=). If you cannot authenticate to the Jellyfin API, instead `kubectl -n ' + args.namespace + ' rollout restart deploy/jellyfin` is NOT desired (avoid) — prefer the API refresh; if no API access, document that a manual/owner library scan is needed and that Jellyfin will pick up the files on its next scheduled scan. Do NOT block success on Jellyfin auth.',
        'If files have imported AND Jellyfin was refreshed, optionally confirm via the Jellyfin API that the series item now has episodes (best-effort). ',
        'SUCCESS CRITERIA (be honest): the fix is considered correctly DELIVERED if (a) the search was triggered and (b) EITHER grabs/downloads are progressing or importing (pipeline works) OR indexers genuinely returned no releases (documented), AND (c) a Jellyfin rescan was triggered or the manual-scan need is documented. Full completion of all monitored episodes is NOT required within the run (Sonarr will finish downloading on its own). Set pipelineWorking accordingly and report importsObserved + episodeFileCountAfter honestly.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['pipelineWorking', 'importsObserved', 'episodeFileCountAfter', 'jellyfinRescanned', 'jellyfinPlayable', 'manualScanNeeded', 'summary'],
      properties: {
        pipelineWorking: { type: 'boolean' },
        importsObserved: { type: 'number' },
        episodeFileCountAfter: { type: 'number' },
        jellyfinRescanned: { type: ['boolean', 'string'] },
        jellyfinPlayable: { type: ['boolean', 'string', 'null'] },
        manualScanNeeded: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 3 — wrap up: open tracking issue + follow-ups (per repo policy)
// ---------------------------------------------------------------------------
const wrapupTask = defineTask('wrapup', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Open tracking issue for the empty-anime-folder fix + the Jellyfin-on-legacy-hostPath follow-up (scrubbed)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE recording the empty-anime-folder fix per the Epaflix repo Critical Rules',
      task: 'Persist the outcome as gh issues on ' + args.repo + ' (enhancement/bug shape), scrubbed of the show title. Only state what is actually true.',
      context: { ...args },
      instructions: [
        'SCRUB RULE (mandatory): NEVER write the show/release title in any issue, comment, or doc. Refer to the anime ONLY as "Sonarr2 seriesId ' + args.seriesId + ' (tvdb ' + args.tvdbId + ')".',
        'STATE OF PLAY (be accurate): root cause = seriesId ' + args.seriesId + ' had monitored seasons (S3/S4/S5) but 0 files + 0 grab/import history -> empty on-disk folder -> Jellyfin showed an empty/unplayable series. Fix initiated: Sonarr2 SeriesSearch triggered; ' +
          (args.pipelineWorking ? 'search->grab pipeline confirmed working (grabs/downloads observed)' : 'pipeline outcome: ' + (args.noReleasesFound ? 'no releases found by indexers (documented)' : 'see verify result')) + '; ' +
          'episodeFileCount after = ' + (args.episodeFileCountAfter != null ? args.episodeFileCountAfter : 'see result') + '; Jellyfin rescan = ' + (args.jellyfinRescanned || 'see result') + '. S1/S2 left UNMONITORED (owner content decision — not auto-enabled).',
        'SEARCH existing issues first (`gh issue list --repo ' + args.repo + ' --state open --limit 100`) to avoid duplicates. Then:',
        '(1) OPEN a tracking/bug issue for THIS report: title like "servarr: Sonarr2 seriesId ' + args.seriesId + ' (tvdb ' + args.tvdbId + ') empty folder — monitored seasons never downloaded; Jellyfin shows empty series". Use the shape `## Finding` / `## Current state` / `## Desired outcome` / `## Notes`. ' +
          'Document: empty folder, 0 history, monitored S3/S4/S5, search triggered + current download status, Jellyfin rescan status, and the S1/S2-unmonitored owner decision. If the series finished importing and is now playable, you may instead CLOSE this immediately as resolved with the evidence — otherwise leave it open as a "downloading, will populate" tracker. Cross-link #195/#247 where relevant.',
        '(2) OPEN a HIGH-VALUE follow-up issue: "servarr: migrate Jellyfin media mounts off legacy hostPath /mnt/k3s-{animes,tvshows,movies} onto the unified servarr-media /media (before #247 teardown)". ' +
          'Finding: jellyfin.yaml mounts media-animes/tvshows/movies as hostPath to the legacy pre-#195 split exports; these are slated for teardown in #247; when removed, Jellyfin loses all media. Desired: migrate Jellyfin to the unified servarr-media PVC at /media (read-only), mirroring the #250 bazarr/lingarr migration. Cross-link #195 #247 #250. This is genuinely important — flag it clearly.',
        '(3) If verify found a Jellyfin manual-scan was needed (no API access), note that in issue (1).',
        'Do NOT commit anything to git. No secrets. No show titles anywhere.',
        'Return ONLY the structured JSON result (issue numbers/URLs you opened or closed).',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['trackingIssue', 'jellyfinMigrationIssue', 'issueClosed', 'followUpIssues', 'summary'],
      properties: {
        trackingIssue: { type: 'string' },
        jellyfinMigrationIssue: { type: 'string' },
        issueClosed: { type: 'boolean' },
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
    sonarr2Deploy: inputs.sonarr2Deploy || 'sonarr2',
    seriesId: inputs.seriesId || 2,
    tvdbId: inputs.tvdbId || 348545,
    monitoredSeasons: inputs.monitoredSeasons || [3, 4, 5],
  };

  ctx.log('info', `Jellyfin empty-anime-folder fix: Sonarr2 seriesId ${cfg.seriesId} (tvdb ${cfg.tvdbId})`);

  // PHASE 0 — investigate (read-only) + owner gate
  const diag = await ctx.task(investigateTask, { ...cfg });
  ctx.log('info', `Diagnosis: folderEmpty=${diag.folderEmpty}; historyCount=${diag.historyCount}; jellyfinSeesEmpty=${diag.jellyfinSeesEmpty}`);

  const gateA = await ctx.breakpoint({
    question: 'Approve the empty-folder fix? Trigger Sonarr2 SeriesSearch for monitored episodes of seriesId ' + cfg.seriesId + ' (S3/S4/S5; S1/S2 left unmonitored), verify the download pipeline, then Jellyfin rescan. Reply "abort" to stop after diagnosis.',
    title: 'Empty-anime-folder Fix Gate',
    context: { runId: ctx.runId, rootCause: diag.rootCause, fixPlan: diag.fixPlan, followUps: diag.followUps },
    expert: 'owner',
    tags: ['approval-gate'],
  });
  if (!gateA.approved || (gateA.response || '').toLowerCase().includes('abort')) {
    ctx.log('warn', 'Fix not approved — stopping after read-only diagnosis.');
    return { success: false, reason: 'fix-not-approved', rootCause: diag.rootCause, diag, feedback: gateA.response || '' };
  }

  // PHASE 1 — trigger the search (live)
  const trig = await ctx.task(triggerFixTask, { ...cfg });
  ctx.log('info', `Search: triggered=${trig.searchTriggered}; status=${trig.commandStatus}; grabs=${trig.grabsObserved}; queued=${trig.queuedCount}; noReleases=${trig.noReleasesFound}`);
  if (!trig.searchTriggered) {
    return { success: false, reason: 'search-not-triggered', rootCause: diag.rootCause, trig, summary: trig.summary };
  }

  // PHASE 2 — verify pipeline + jellyfin rescan (live, bounded)
  const ver = await ctx.task(verifyTask, { ...cfg, trigger: trig });
  ctx.log('info', `Verify: pipelineWorking=${ver.pipelineWorking}; imports=${ver.importsObserved}; fileCountAfter=${ver.episodeFileCountAfter}; jellyfinRescanned=${ver.jellyfinRescanned}`);

  // Accept success if the pipeline works OR indexers genuinely had no releases (documented). Both are honest outcomes.
  const fixDelivered = ver.pipelineWorking || trig.noReleasesFound;
  if (!fixDelivered) {
    return { success: false, reason: 'pipeline-not-working', rootCause: diag.rootCause, trig, ver, summary: ver.summary };
  }

  // PHASE 3 gate — wrap-up (owner-gated; auto-approved in yolo)
  const gateB = await ctx.breakpoint({
    question: 'Fix initiated + verified (pipeline ' + (ver.pipelineWorking ? 'working' : 'no-releases documented') + '; Jellyfin rescan ' + ver.jellyfinRescanned + '). Approve WRAP-UP (open scrubbed tracking issue + the Jellyfin-legacy-hostPath migration follow-up)? Reply "stop here" to skip.',
    title: 'Empty-anime-folder Wrap-up Gate',
    context: { runId: ctx.runId, importsObserved: ver.importsObserved, jellyfinMount: diag.jellyfinMount },
    expert: 'owner',
    tags: ['approval-gate'],
  });
  if (!gateB.approved || (gateB.response || '').toLowerCase().includes('stop here')) {
    ctx.log('warn', 'Stopped before wrap-up by owner.');
    return { success: true, partial: true, reason: 'stopped-before-wrapup', rootCause: diag.rootCause, searchTriggered: true, pipelineWorking: ver.pipelineWorking };
  }

  const wrap = await ctx.task(wrapupTask, {
    ...cfg,
    pipelineWorking: ver.pipelineWorking,
    noReleasesFound: trig.noReleasesFound,
    episodeFileCountAfter: ver.episodeFileCountAfter,
    jellyfinRescanned: ver.jellyfinRescanned,
    manualScanNeeded: ver.manualScanNeeded,
  });
  ctx.log('info', `Wrap-up: trackingIssue=${wrap.trackingIssue}; jellyfinMigrationIssue=${wrap.jellyfinMigrationIssue}; closed=${wrap.issueClosed}`);

  return {
    success: true,
    rootCause: diag.rootCause,
    searchTriggered: trig.searchTriggered,
    grabsObserved: trig.grabsObserved,
    importsObserved: ver.importsObserved,
    jellyfinRescanned: ver.jellyfinRescanned,
    jellyfinPlayable: ver.jellyfinPlayable,
    issueOpened: wrap.trackingIssue,
    followUpIssues: [wrap.jellyfinMigrationIssue, ...(wrap.followUpIssues || [])].filter(Boolean),
    summary: wrap.summary,
  };
}
