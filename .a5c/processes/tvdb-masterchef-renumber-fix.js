/**
 * @process specializations/devops-sre-platform/tvdb-masterchef-renumber-fix
 * @description 4th option for the MasterChef GR numbering problem (seriesId 89 in Sonarr HD, tvdb 328975).
 *   The owner wants TheTVDB's AIRED-ORDER numbering corrected so the CURRENT (2026) season is Season 10,
 *   matching IMDb and the broadcaster star.gr (and the Magico release group, which labels current episodes
 *   "Masterchef GR S10Exx"). TVDB currently shows 12 aired seasons (2010..2026); it is off by +2 — almost
 *   certainly because TVDB counts the two early Mega-era seasons (2010, 2012) that the broadcaster/IMDb
 *   exclude from the revival-era count (2017->2026 = S1..S10). The owner edited TVDB before but it was
 *   reverted (by user rafaella1593). They have re-authorized a direct TVDB edit and provided an
 *   authenticated session cookie.
 *
 *   Why it matters: Sonarr uses TVDB aired order. It searches the current season as S12, but every release
 *   on the indexer (Magico) is labelled S10 -> 0 matches -> S11/S12 stay empty, while 2026 episodes labelled
 *   S10 get mis-filed into TVDB's Season 10 (2024). Correcting TVDB to current=S10 makes Sonarr's search and
 *   filing line up with the releases.
 *
 *   Plan: (0) AUTH-verified READ-ONLY investigation of the exact TVDB structure + the precise renumber plan
 *   + the exact edit MECHANISM (per-season number edit vs per-episode reassignment; authenticated form POST
 *   with CSRF vs Playwright UI) + rollback; (1) APPLY the TVDB edit (live external mutation, owner-authorized);
 *   (2) VERIFY TVDB now shows 10 aired seasons with current=S10; (3) PROPAGATE to Sonarr (RefreshSeries on
 *   seriesId 89, confirm aired order re-numbers, re-search so Magico S10 matches + files correctly, sanity
 *   check the existing 76 files re-map); (4) WRAP UP with a scrubbed tracking issue + follow-ups.
 *
 *   SECURITY: the TVDB session cookie is a SECRET. It lives ONLY in a git-ignored runtime file
 *   (cookieFile, under .a5c/runs/_secrets). NEVER write it into .a5c/processes (git-tracked), commits,
 *   issues, docs, or task result JSON. SCRUB the media title from all committed git/issues — refer to the
 *   series only as seriesId 89 / tvdb 328975. TVDB editing is outward-facing + community-moderated (may be
 *   reverted); apply carefully and report honestly if it cannot fully complete.
 *
 * @inputs { repoRoot, repo, namespace, sonarrDeploy, sonarrSeriesId, tvdbId, tvdbSlug, cookieFile, targetCurrentSeason }
 * @outputs { success, tvdbEdited, tvdbSeasonsAfter, currentSeasonAfter, sonarrRefreshed, sonarrCurrentSeason, magicoMatches, issueOpened, summary }
 *
 * @agent general-purpose (authenticated HTTP/curl, Playwright MCP browser automation, Sonarr API, gh, verification)
 * @skill systematic-debugging superpowers:systematic-debugging
 * @skill verification-before-completion superpowers:verification-before-completion
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

// ---------------------------------------------------------------------------
// PHASE 0 — authenticated READ-ONLY investigation + exact renumber plan + edit mechanism
// ---------------------------------------------------------------------------
const investigateTask = defineTask('investigate', (args, taskCtx) => ({
  kind: 'agent',
  title: 'TVDB ' + (args && args.tvdbSlug) + ': confirm structure, author exact renumber plan + edit mechanism (AUTH, READ-ONLY)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Metadata SRE fixing a TheTVDB aired-order season-numbering error for seriesId ' + (args && args.sonarrSeriesId) + ' (tvdb ' + (args && args.tvdbId) + ')',
      task:
        'Investigate the EXACT current TheTVDB aired-order structure for ' + args.tvdbSlug + ' and author the precise, minimal, reversible edit plan to make the CURRENT (2026) season = Season ' + args.targetCurrentSeason + ' ' +
        '(matching IMDb + star.gr), plus determine the exact edit MECHANISM. STRICTLY READ-ONLY — do not mutate TVDB yet.',
      context: { ...args },
      instructions: [
        'SECURITY: the TVDB cookie is at ' + args.cookieFile + ' (git-ignored). Read it at runtime: `COOKIE=$(cat ' + args.cookieFile + ')`. NEVER print the cookie value into output.json, logs you keep, commits, or issues. Confirm the session is valid first: GET https://thetvdb.com/auth/getuser with header "Cookie: $COOKIE" + a browser User-Agent + "X-Requested-With: XMLHttpRequest" -> expect a JSON user object (user spypower / id 2795808).',
        'SCRUB: refer to the series only as seriesId ' + args.sonarrSeriesId + ' / tvdb ' + args.tvdbId + ' in output.json and any committed text.',
        'STRUCTURE: enumerate the current TVDB AIRED-ORDER seasons (currently 12, 2010..2026). For EACH season record: season number, episode count, and air-date range (first..last). Fetch https://thetvdb.com/series/' + args.tvdbSlug + '/allseasons/official and per-season pages /series/' + args.tvdbSlug + '/seasons/official/<n>. Cross-check against Sonarr (seriesId ' + args.sonarrSeriesId + ' /api/v3/series + /api/v3/episode) which mirrors TVDB.',
        'CROSS-REFERENCE the CORRECT numbering: IMDb and star.gr (https://www.star.gr/tv/psychagogia/masterchef/tag_1946) treat the current 2026 season as Season ' + args.targetCurrentSeason + '. Determine WHY TVDB is +2: most likely TVDB counts the 2010 and 2012 Mega-era seasons that the broadcaster/IMDb exclude from the revival-era count (2017->2026 = S1..S' + args.targetCurrentSeason + '). CONFIRM this by aligning the year ranges: map each real broadcast year to the desired season number and identify exactly which 2 TVDB seasons are the "extra" ones and how the remaining seasons must be renumbered so 2026=S' + args.targetCurrentSeason + '.',
        'EDIT MECHANISM (critical for feasibility): determine how TVDB v4 lets an authenticated editor change aired-season numbering. Inspect the edit UI/endpoints WITHOUT submitting: (a) is the season NUMBER editable per-SEASON (few edits) or only per-EPISODE seasonNumber (potentially hundreds)? (b) capture the exact edit URL(s), the HTTP method, the form field names, and where the CSRF token comes from (e.g. a meta[name=csrf-token] or a hidden _token input on the edit page) — fetch an edit page (e.g. /series/' + args.tvdbSlug + '/seasons/official/<n>/edit or the episode edit page) and extract the token + form fields. (c) decide the best method: authenticated form POST via curl (preferred if scriptable) OR Playwright MCP browser automation (search ToolSearch for mcp__plugin_playwright_playwright__* tools) if the UI requires JS. State the chosen method and why.',
        'Produce: (1) the EXACT ordered edit runbook (each concrete edit: which season/episodes, from->to number, the request or UI steps), (2) the rollback (how to restore the prior numbering), (3) a risk list (community revert by rafaella1593, moderation, rate limits, volume of edits), (4) an estimate of how many discrete edits are required. If the only feasible path is hundreds of manual episode edits, say so explicitly and propose the most efficient automation.',
        'Save raw captures (HTML, parsed season table, the edit-form field list WITHOUT secrets) under tasks/' + taskCtx.effectId + '/. Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['sessionValid', 'tvdbSeasons', 'desiredMapping', 'extraSeasons', 'editMechanism', 'csrfSource', 'editRunbook', 'editCount', 'rollback', 'risks', 'summary'],
      properties: {
        sessionValid: { type: 'boolean' },
        tvdbSeasons: { type: 'array', items: { type: 'object' } },
        desiredMapping: { type: 'array', items: { type: 'object' } },
        extraSeasons: { type: 'array', items: { type: ['number', 'string'] } },
        editMechanism: { type: 'string' },
        csrfSource: { type: 'string' },
        editRunbook: { type: 'array', items: { type: 'object' } },
        editCount: { type: 'number' },
        rollback: { type: 'array', items: { type: 'object' } },
        risks: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 0b — refine the edit plan on owner feedback / failed precheck (READ-ONLY)
const refinePlanTask = defineTask('refine-plan', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Refine the TVDB renumber plan/mechanism per feedback (READ-ONLY)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Metadata SRE refining the TVDB renumber plan',
      task: 'Revise the prior plan to address the feedback. Read-only; no TVDB mutation.',
      context: { ...args },
      instructions: [
        'Feedback to address: ' + (args.feedback || '(none)'),
        'Cookie at ' + args.cookieFile + ' (never print it). Re-verify any facts the feedback questions.',
        'Return the SAME schema as investigate with the revised editRunbook/editMechanism/risks.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['sessionValid', 'tvdbSeasons', 'desiredMapping', 'extraSeasons', 'editMechanism', 'csrfSource', 'editRunbook', 'editCount', 'rollback', 'risks', 'summary'],
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 1 — apply the TVDB edit (live external mutation, owner-authorized)
// ---------------------------------------------------------------------------
const applyEditTask = defineTask('apply-tvdb-edit', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Apply the TVDB renumber edit so current season = S' + (args && args.targetCurrentSeason) + ' (live, authenticated)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Metadata SRE executing the authorized TheTVDB aired-order renumber edit',
      task:
        'Execute the APPROVED edit runbook to renumber the TVDB aired-order seasons of ' + args.tvdbSlug + ' so the current (2026) season becomes Season ' + args.targetCurrentSeason + '. Use the chosen mechanism. ' +
        'Verify each edit took effect. This is a LIVE edit of a community wiki — be precise and careful.',
      context: { ...args },
      instructions: [
        'SECURITY: cookie at ' + args.cookieFile + ' (`COOKIE=$(cat ' + args.cookieFile + ')`). NEVER print/commit/log/issue the cookie value. SCRUB the title everywhere (seriesId ' + args.sonarrSeriesId + ' / tvdb ' + args.tvdbId + ').',
        'Re-verify the session (getuser) before editing. Re-fetch a fresh CSRF token immediately before submitting (tokens can rotate).',
        'Follow the APPROVED runbook EXACTLY: ' + JSON.stringify(args.editRunbook || []) + '. Mechanism: ' + (args.editMechanism || 'see plan') + '. CSRF source: ' + (args.csrfSource || 'see plan') + '.',
        'If the mechanism is authenticated form POST (curl): for each edit, GET the edit page to capture the current CSRF token + existing field values, then POST the modified form (preserve all required hidden fields). Check the response (HTTP status + that the value changed). Respect rate limits — small delays between edits; do not hammer.',
        'If the mechanism is Playwright (UI requires JS): use the Playwright MCP tools (find them via ToolSearch: query "playwright browser"). Set the TVDB session cookie in the browser context before navigating (navigate to thetvdb.com first, then add the two cookies from ' + args.cookieFile + ' via the appropriate Playwright cookie/eval mechanism), then drive the season-edit UI per the runbook. Take a screenshot/snapshot after each significant edit as evidence.',
        'Work INCREMENTALLY and idempotently: after each season/episode renumber, confirm it applied. If TVDB rejects an edit, requires moderation queue, or auto-reverts, CAPTURE the exact response and STOP that branch — record applied-vs-not so the orchestrator can decide. Do NOT brute-force against repeated rejections.',
        'If the plan requires many episode edits, proceed methodically and report progress counts. If you cannot complete all edits (volume/rate-limit/moderation), apply as many as cleanly possible and report the partial state precisely + how to finish manually.',
        'Save evidence (responses/screenshots, applied-edit log WITHOUT the cookie) under tasks/' + taskCtx.effectId + '/. Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['editsApplied', 'editsTotal', 'fullyApplied', 'rejectionsOrReverts', 'method', 'summary'],
      properties: {
        editsApplied: { type: 'number' },
        editsTotal: { type: 'number' },
        fullyApplied: { type: 'boolean' },
        rejectionsOrReverts: { type: 'array', items: { type: 'string' } },
        method: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 2 — verify TVDB now shows the corrected numbering (authenticated read)
// ---------------------------------------------------------------------------
const verifyTvdbTask = defineTask('verify-tvdb', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify TVDB aired order now has current season = S' + (args && args.targetCurrentSeason),
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Metadata SRE verifying the TVDB renumber result',
      task: 'Confirm TheTVDB aired-order now reflects the corrected numbering (current 2026 season = Season ' + args.targetCurrentSeason + ', total seasons reduced from 12 to ' + args.targetCurrentSeason + ', episode counts/dates sane). Read-only.',
      context: { ...args },
      instructions: [
        'Cookie at ' + args.cookieFile + ' (never print it). Re-fetch /series/' + args.tvdbSlug + '/allseasons/official and the (new) current-season page. Record the seasons list after the edit (number + episode count + year range).',
        'Confirm: total aired seasons now = ' + args.targetCurrentSeason + '; the 2026 season is now Season ' + args.targetCurrentSeason + '; no seasons were orphaned/duplicated; episode counts are preserved (no episodes lost).',
        'If TVDB still shows the OLD numbering (edit did not stick / was reverted / is in a moderation queue), set corrected=false and explain.',
        'SCRUB the title. Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['corrected', 'seasonsAfter', 'currentSeasonAfter', 'episodesPreserved', 'summary'],
      properties: {
        corrected: { type: 'boolean' },
        seasonsAfter: { type: 'array', items: { type: 'object' } },
        currentSeasonAfter: { type: ['number', 'string', 'null'] },
        episodesPreserved: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 3 — propagate to Sonarr + confirm Magico releases now match (live)
// ---------------------------------------------------------------------------
const propagateSonarrTask = defineTask('propagate-sonarr', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Refresh Sonarr seriesId ' + (args && args.sonarrSeriesId) + ' onto corrected TVDB numbering + confirm Magico S' + (args && args.targetCurrentSeason) + ' matches',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr SRE propagating the corrected TVDB numbering into Sonarr',
      task:
        'Refresh Sonarr (seriesId ' + args.sonarrSeriesId + ') so it adopts the corrected TVDB aired numbering, confirm the current season is now S' + args.targetCurrentSeason + ', and verify Magico\'s "Masterchef GR S' + args.targetCurrentSeason + 'Exx" releases now MATCH (interactive search) so episodes file into the right season.',
      context: { ...args },
      instructions: [
        'SCRUB: seriesId ' + args.sonarrSeriesId + ' / tvdb ' + args.tvdbId + ' only. Sonarr (HD) API: `kubectl -n ' + args.namespace + ' exec deploy/' + args.sonarrDeploy + ' -- sh -c \'KEY=$(grep -o "<ApiKey>[^<]*" /config/config.xml | sed "s/<ApiKey>//"); curl -s http://localhost:8989/...\'`.',
        'RefreshSeries: POST /api/v3/command {"name":"RefreshSeries","seriesId":' + args.sonarrSeriesId + '} and poll the command to completion. Then GET /api/v3/series/' + args.sonarrSeriesId + ' and record the new season list (numbers + episodeFileCount/totalEpisodeCount + the year range per season via /api/v3/episode). Confirm the 2026 season is now S' + args.targetCurrentSeason + '.',
        'NOTE existing files: the 76 existing files were filed under the OLD TVDB "Season 10" (2024). After the renumber, verify whether Sonarr re-maps them correctly (they may now need a rescan/rename) — run a "RescanSeries" (or RefreshSeries already rescans) and report episodeFileCount per season. If files now sit under a wrong season due to the renumber, note it (do NOT mass-delete; recommend a rename/manual-import follow-up).',
        'MAGICO MATCH PROOF: pick a current-season (2026) monitored missing episode that should now be S' + args.targetCurrentSeason + 'Exx, run an interactive release search GET /api/v3/release?episodeId=<id>, and confirm Magico releases titled "Masterchef GR S' + args.targetCurrentSeason + 'Exx" now appear as grabbable (not rejected for wrong season). Optionally trigger SeriesSearch and confirm a grab for the current season files into S' + args.targetCurrentSeason + '. Capture evidence.',
        'Do NOT change season monitoring beyond what is needed; do NOT delete files. Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['sonarrRefreshed', 'sonarrCurrentSeason', 'seasonFileCounts', 'magicoMatches', 'existingFilesRemapNote', 'summary'],
      properties: {
        sonarrRefreshed: { type: 'boolean' },
        sonarrCurrentSeason: { type: ['number', 'string', 'null'] },
        seasonFileCounts: { type: 'object' },
        magicoMatches: { type: 'boolean' },
        existingFilesRemapNote: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 4 — wrap up: scrubbed tracking issue + follow-ups (NO cookie, NO title)
// ---------------------------------------------------------------------------
const wrapupTask = defineTask('wrapup', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Document the TVDB renumber fix + Sonarr propagation on a scrubbed tracking issue + follow-ups',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE recording the TVDB renumber fix per the Epaflix repo Critical Rules',
      task: 'Persist the outcome as a gh issue on ' + args.repo + ', scrubbed of the show title AND the cookie. Only state what is actually true.',
      context: { ...args },
      instructions: [
        'SECURITY/SCRUB (mandatory): NEVER write the TVDB cookie or the show/release title anywhere. Refer to the series ONLY as "Sonarr seriesId ' + args.sonarrSeriesId + ' (tvdb ' + args.tvdbId + ')".',
        'STATE OF PLAY (be accurate): root cause = TVDB aired-order had 12 seasons (counted the 2010/2012 Mega-era seasons) while IMDb/star.gr count the revival era so current 2026 = S' + args.targetCurrentSeason + '; Sonarr (TVDB-based) therefore searched S12 while Magico labels current episodes S' + args.targetCurrentSeason + ' -> 0 matches + mis-filing. ' +
          'TVDB edit: ' + (args.tvdbEdited ? 'applied (current season now S' + args.targetCurrentSeason + ')' : 'NOT fully applied — ' + (args.tvdbNote || 'see result')) + '. ' +
          'Sonarr: ' + (args.sonarrRefreshed ? 'refreshed; current season now S' + args.sonarrCurrentSeason + '; Magico match=' + args.magicoMatches : 'not refreshed/see result') + '.',
        'SEARCH existing issues first (`gh issue list --repo ' + args.repo + ' --state open --limit 100`); if a MasterChef/seriesId-89 tracking issue already exists from earlier, UPDATE it (comment) rather than duplicating.',
        'OPEN or UPDATE a tracking issue (shape `## Finding` / `## Current state` / `## Desired outcome` / `## Notes`): document the root cause, the TVDB renumber that was applied (current=S' + args.targetCurrentSeason + '), the Sonarr refresh result + Magico match, and the existing-files remap note. ' +
          'Follow-ups: (a) TVDB community revert risk — user rafaella1593 reverted before; monitor and, if reverted, escalate to a TVDB moderator with the IMDb/star.gr evidence; (b) if existing 76 files now sit under a wrong season after renumber, a rename/manual-import cleanup; (c) Jellyfin still 0 replicas + legacy mounts (#256). Cross-link any earlier MasterChef issue.',
        'Do NOT commit to git. No secrets, no cookie, no titles. Return ONLY the structured JSON result (issue number/url).',
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
    sonarrSeriesId: inputs.sonarrSeriesId || 89,
    tvdbId: inputs.tvdbId || 328975,
    tvdbSlug: inputs.tvdbSlug || 'masterchef-gr',
    cookieFile: inputs.cookieFile || '/home/spy/Documents/Epaflix/k3s-swarm-proxmox/.a5c/runs/_secrets/tvdb-cookie.txt',
    targetCurrentSeason: inputs.targetCurrentSeason || 10,
  };

  ctx.log('info', `TVDB renumber fix: ${cfg.tvdbSlug} (tvdb ${cfg.tvdbId}) -> current season = S${cfg.targetCurrentSeason}; Sonarr seriesId ${cfg.sonarrSeriesId}`);

  // PHASE 0 — investigate (auth, read-only) + owner gate + refine loop
  let plan = await ctx.task(investigateTask, { ...cfg });
  ctx.log('info', `Plan: sessionValid=${plan.sessionValid}; extraSeasons=${JSON.stringify(plan.extraSeasons)}; mechanism=${plan.editMechanism}; editCount=${plan.editCount}`);
  if (!plan.sessionValid) {
    return { success: false, reason: 'tvdb-session-invalid', tvdbEdited: false, plan, summary: 'TVDB session cookie not valid — cannot proceed.' };
  }

  let lastFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    const gateA = await ctx.breakpoint({
      question: 'Approve the TVDB renumber plan? Make current(2026) season = S' + cfg.targetCurrentSeason + ' (remove/renumber the +2 extra seasons), mechanism=' + plan.editMechanism + ', ~' + plan.editCount + ' edits, then propagate to Sonarr. Reply "abort" to stop after read-only investigation.',
      title: 'TVDB Renumber Plan Gate',
      context: { runId: ctx.runId, tvdbSeasons: plan.tvdbSeasons, desiredMapping: plan.desiredMapping, editMechanism: plan.editMechanism, editCount: plan.editCount, risks: plan.risks },
      expert: 'owner',
      tags: ['approval-gate', 'external-mutation'],
      previousFeedback: lastFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    if (gateA.approved && !(gateA.response || '').toLowerCase().includes('abort')) break;
    if ((gateA.response || '').toLowerCase().includes('abort') || (!gateA.approved && attempt === 2)) {
      ctx.log('warn', 'TVDB edit not approved — stopping after read-only investigation.');
      return { success: false, reason: 'edit-not-approved', tvdbEdited: false, plan, feedback: gateA.response || '' };
    }
    lastFeedback = gateA.response || gateA.feedback || 'Changes requested';
    plan = await ctx.task(refinePlanTask, { ...cfg, prior: plan, feedback: lastFeedback });
  }

  // PHASE 1 — apply the TVDB edit (live external mutation)
  const edit = await ctx.task(applyEditTask, { ...cfg, editRunbook: plan.editRunbook, editMechanism: plan.editMechanism, csrfSource: plan.csrfSource });
  ctx.log('info', `Edit: applied=${edit.editsApplied}/${edit.editsTotal}; fullyApplied=${edit.fullyApplied}; rejections=${(edit.rejectionsOrReverts || []).length}`);

  // PHASE 2 — verify TVDB
  const vtv = await ctx.task(verifyTvdbTask, { ...cfg });
  ctx.log('info', `TVDB verify: corrected=${vtv.corrected}; currentSeasonAfter=${vtv.currentSeasonAfter}; episodesPreserved=${vtv.episodesPreserved}`);

  if (!vtv.corrected) {
    // Edit didn't fully land (rejected/reverted/moderation/volume). Stop before touching Sonarr; document honestly.
    const wrapPartial = await ctx.task(wrapupTask, {
      ...cfg, tvdbEdited: false, tvdbNote: (edit.rejectionsOrReverts || []).join('; ') || edit.summary,
      sonarrRefreshed: false, sonarrCurrentSeason: null, magicoMatches: false,
    });
    return {
      success: false, reason: 'tvdb-not-corrected', tvdbEdited: false,
      editsApplied: edit.editsApplied, editsTotal: edit.editsTotal,
      issueOpened: wrapPartial.trackingIssue, summary: vtv.summary,
    };
  }

  // PHASE 3 — propagate to Sonarr + confirm Magico match
  const prop = await ctx.task(propagateSonarrTask, { ...cfg });
  ctx.log('info', `Sonarr: refreshed=${prop.sonarrRefreshed}; currentSeason=${prop.sonarrCurrentSeason}; magicoMatches=${prop.magicoMatches}`);

  // PHASE 4 gate — wrap-up (owner-gated; auto-approved in yolo)
  const gateB = await ctx.breakpoint({
    question: 'TVDB corrected (current=S' + vtv.currentSeasonAfter + ') and Sonarr refreshed (current=S' + prop.sonarrCurrentSeason + ', Magico match=' + prop.magicoMatches + '). Approve WRAP-UP (scrubbed tracking issue + follow-ups)? Reply "stop here" to skip.',
    title: 'TVDB Fix Wrap-up Gate',
    context: { runId: ctx.runId, currentSeasonAfter: vtv.currentSeasonAfter, magicoMatches: prop.magicoMatches },
    expert: 'owner',
    tags: ['approval-gate'],
  });
  if (!gateB.approved || (gateB.response || '').toLowerCase().includes('stop here')) {
    ctx.log('warn', 'Stopped before wrap-up by owner.');
    return { success: true, partial: true, reason: 'stopped-before-wrapup', tvdbEdited: true, currentSeasonAfter: vtv.currentSeasonAfter, sonarrCurrentSeason: prop.sonarrCurrentSeason, magicoMatches: prop.magicoMatches };
  }

  const wrap = await ctx.task(wrapupTask, {
    ...cfg, tvdbEdited: true, sonarrRefreshed: prop.sonarrRefreshed,
    sonarrCurrentSeason: prop.sonarrCurrentSeason, magicoMatches: prop.magicoMatches,
  });
  ctx.log('info', `Wrap-up: trackingIssue=${wrap.trackingIssue}; followUps=${JSON.stringify(wrap.followUpIssues)}`);

  return {
    success: true,
    tvdbEdited: true,
    tvdbSeasonsAfter: vtv.seasonsAfter && vtv.seasonsAfter.length,
    currentSeasonAfter: vtv.currentSeasonAfter,
    sonarrRefreshed: prop.sonarrRefreshed,
    sonarrCurrentSeason: prop.sonarrCurrentSeason,
    magicoMatches: prop.magicoMatches,
    issueOpened: wrap.trackingIssue,
    followUpIssues: wrap.followUpIssues,
    summary: wrap.summary,
  };
}
