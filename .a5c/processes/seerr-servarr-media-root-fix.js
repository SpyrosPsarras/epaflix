/**
 * @process specializations/devops-sre-platform/seerr-servarr-media-root-fix
 * @description Issue #250 (surfaced by #195 / #240 single-root /media mount). After the unified NFS
 *   export migration, Sonarr/Sonarr2/Radarr now report root folders under /media (/media/tvshows,
 *   /media/animes, /media/movies) and the legacy /tv /movies /animes roots no longer exist. Three
 *   consumers were left referencing the OLD roots, so new requests/subtitles silently break:
 *     1. Seerr (jellyseerr) still has its Sonarr service Default Root Folder = "/tv" (and Radarr
 *        "/movies"), so every auto-approved TV/movie request 400s with RootFolderExistsValidator
 *        ("Root folder '/tv' does not exist"). Reproduced live: request 479 (tmdb 124364 S4) FAILED.
 *        Seerr root folders are RUNTIME config (settings.json in the seerr PVC), NOT a manifest.
 *     2. bazarr + lingarr manifests still mount the old per-folder PVCs (servarr-media-movies -> /movies,
 *        servarr-media-tvshows -> /tv). bazarr's path_mappings are EMPTY, so it expects its own paths to
 *        match exactly what Sonarr/Radarr report (now /media/tvshows, /media/movies) -> subtitle file
 *        access is broken. Fix = mount the unified servarr-media PVC at a single /media root, exactly
 *        like sonarr/sonarr2/radarr/cleanuparr/qbittorrent (GitOps manifest change via branch+PR).
 *     3. Sonarr's 4 unmappedFolders under /media/tvshows — reconcile/document (live state already shows
 *        all 218 HD series + 106 anime series on /media paths with ZERO legacy /tv paths, so the unmapped
 *        folders are benign utility dirs + one orphan; verify, do not auto-mutate the library).
 *
 *   Live-change risk: edits the Seerr settings DB (runtime) and the storage mounts of two media apps
 *   (GitOps). Seerr settings.json is backed up before edit; the new activeDirectory must EXACTLY match a
 *   live Sonarr/Radarr root folder (validated against /api/v3/rootfolder). The bazarr/lingarr remount goes
 *   through branch -> PR -> rebase -> validate -> --merge per the repo merge policy, so ArgoCD selfHeal
 *   applies it. End-to-end proof = re-request 479 and confirm Sonarr accepts the add (no 400, serviceId
 *   populated, series present under /media/tvshows). Deploy/live-config steps are owner-gated.
 *
 * @inputs { repoRoot, repo, namespace, branch, issue, seerrRoots, requestId, tmdbId }
 * @outputs { success, seerrFixed, tvRequestProven, manifestsMigrated, prUrl, argoHealthy, unmappedReconciled, followUpIssues, issueClosed, summary }
 *
 * @agent general-purpose (kubectl/exec, Seerr+Sonarr+Radarr+bazarr API, git/gh, kustomize, verification)
 * @skill systematic-debugging superpowers:systematic-debugging
 * @skill verification-before-completion superpowers:verification-before-completion
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

// ---------------------------------------------------------------------------
// PHASE 0 — capture live baseline + author exact fix plan (READ-ONLY)
// ---------------------------------------------------------------------------
const captureStateTask = defineTask('capture-state', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Capture live Seerr/Sonarr/Radarr/bazarr/lingarr state + author exact fix plan (READ-ONLY)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr/Kubernetes SRE on the Epaflix k3s cluster scoping issue #250 (old /tv /movies roots after #195 single /media mount)',
      task:
        'Capture the COMPLETE current live state behind issue #250 and author the exact, ordered, reversible fix plan. ' +
        'DO NOT change anything — strictly read-only. Namespace=' + args.namespace + ', repo=' + args.repo + '.',
      context: { ...args },
      instructions: [
        'Apply systematic-debugging discipline: gather hard evidence via live API/kubectl, never assume.',
        'SEERR settings (runtime, in the seerr PVC): `kubectl -n ' + args.namespace + ' exec deploy/seerr -- cat /app/config/settings.json`. Record the EXACT current ' +
          'activeDirectory for each radarr[] and sonarr[] service entry (by id + name), their is4k flag, and the main.apiKey. Expected stale values: Radarr activeDirectory ' +
          '"/movies", Sonarr "/tv", Sonarr2 "/animes". Note: changing is4k is OUT OF SCOPE for #250 (tracked separately) — record it but do not plan to touch it.',
        'SONARR HD + SONARR2 + RADARR root folders (live, authoritative target values): for each, ' +
          '`kubectl -n ' + args.namespace + ' exec deploy/<app> -- sh -c \'KEY=$(grep -o "<ApiKey>[^<]*" /config/config.xml | sed "s/<ApiKey>//"); curl -s http://localhost:<port>/api/v3/rootFolder?apikey=$KEY\'` ' +
          '(sonarr/sonarr2 port 8989, radarr 7878). Record each root folder path + accessible + the unmappedFolders list. Confirm the target roots are /media/tvshows (sonarr), ' +
          '/media/animes (sonarr2), /media/movies (radarr) and that NO /tv|/movies|/animes (non-/media) root remains.',
        'SONARR SERIES PATHS: pull /api/v3/series for sonarr + sonarr2 and confirm whether any series.path still starts with a legacy /tv|/movies|/animes (NOT /media). ' +
          'Report counts of /media-prefixed vs legacy-prefixed series for each. (Prior live check showed 0 legacy paths — re-verify.)',
        'UNMAPPED FOLDERS: classify each of Sonarr HD\'s unmappedFolders under /media/tvshows. Distinguish benign utility dirs (e.g. import/private/template) from real ' +
          'orphan series folders that have no matching series record. Do NOT propose auto-adding/deleting anything — just classify and recommend (manual owner decision).',
        'BAZARR + LINGARR manifests: read ' + args.repoRoot + '/2-k3s/08.servarr/bazarr/bazarr.yaml and .../lingarr/lingarr.yaml. Record the current volumeMounts ' +
          '(/movies via servarr-media-movies, /tv via servarr-media-tvshows) and volumes. Compare to sonarr.yaml/radarr.yaml (single servarr-media -> /media). ' +
          'Read bazarr live path_mappings: `kubectl -n ' + args.namespace + ' exec deploy/bazarr -- cat /config/config/config.yaml` (expect path_mappings: [] and path_mappings_movie: []), ' +
          'which means bazarr REQUIRES its own paths to equal what Sonarr/Radarr report -> the only correct fix is to mount the unified servarr-media at /media (no path mapping needed). ' +
          'For lingarr, inspect its runtime media-path config (it connects to Sonarr/Radarr and uses their reported paths); note whether lingarr has any internal source-directory/path setting that must also be repointed to /media after the remount (flag as PVC-only follow-up if so).',
        'REQUEST 479 baseline: via the Seerr API (X-Api-Key from settings.json main.apiKey; use node http inside the seerr pod since curl is absent) GET /api/v1/request/479 and ' +
          'record status (4 = FAILED expected), serviceId (null expected), type (tv), tmdbId (' + args.tmdbId + '), media.status. This is the before-state to invert.',
        'AUTHOR the fix plan with three parts: (A) Seerr runtime: set each service activeDirectory to its live root folder string — radarr -> /media/movies, sonarr -> /media/tvshows, ' +
          'sonarr2 -> /media/animes — back up settings.json first, restart seerr, verify persisted; (B) GitOps manifest: bazarr + lingarr both drop the two media volumes/mounts and gain a ' +
          'single servarr-media -> /media mount (matching sonarr/radarr), via branch ' + args.branch + ' -> PR -> rebase -> validate -> --merge; (C) verify: re-request 479 end-to-end + reconcile/document unmapped folders. ' +
          'State the exact rollback for each part (restore settings.json backup + restart; revert the manifest PR + ArgoCD re-sync).',
        'Save raw captures under tasks/' + taskCtx.effectId + '/. Do NOT put any media release/show titles in committed artifacts later (scrub rule); in this read-only capture you may name folders as returned by the API.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['seerrServices', 'liveRootFolders', 'legacySeriesPaths', 'unmappedFolders', 'bazarrLingarrMounts', 'bazarrPathMappings', 'request479', 'fixPlan', 'rollbackPlan', 'risks', 'summary'],
      properties: {
        seerrServices: { type: 'array', items: { type: 'object' } },
        liveRootFolders: { type: 'object' },
        legacySeriesPaths: { type: 'object' },
        unmappedFolders: { type: 'array', items: { type: 'object' } },
        bazarrLingarrMounts: { type: 'object' },
        bazarrPathMappings: { type: 'object' },
        request479: { type: 'object' },
        fixPlan: { type: 'object' },
        rollbackPlan: { type: 'array', items: { type: 'object' } },
        risks: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 0b — refine plan after owner feedback (READ-ONLY)
const refinePlanTask = defineTask('refine-plan', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Refine the #250 fix plan per owner feedback (READ-ONLY)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr SRE refining the #250 fix plan after owner review',
      task: 'Revise the prior plan to address the owner feedback. Read-only; no changes applied.',
      context: { ...args },
      instructions: [
        'Owner feedback to address: ' + (args.feedback || '(none)'),
        'Re-verify any live facts the feedback questions; keep the additive/recovery-first ordering and the exact target root strings.',
        'Return the SAME schema as capture-state with the revised fixPlan/rollbackPlan/risks.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['seerrServices', 'liveRootFolders', 'legacySeriesPaths', 'unmappedFolders', 'bazarrLingarrMounts', 'bazarrPathMappings', 'request479', 'fixPlan', 'rollbackPlan', 'risks', 'summary'],
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 1 — fix Seerr runtime root folders (live config, reversible via backup)
// ---------------------------------------------------------------------------
const seerrFixTask = defineTask('seerr-fix', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Repoint Seerr service Default Root Folders to /media/{tvshows,animes,movies}; back up settings.json; restart seerr; verify',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr SRE applying the Seerr runtime root-folder fix (the root cause of the #250 request 400s)',
      task:
        'Edit the Seerr settings.json in the seerr PVC so each service Default Root Folder (activeDirectory) matches its LIVE Sonarr/Radarr root folder, ' +
        'back up first, restart seerr, and verify it persisted + Seerr can fetch the new roots. Reversible via the backup.',
      context: { ...args },
      instructions: [
        'PRECHECK: re-fetch the live root folders (sonarr /media/tvshows, sonarr2 /media/animes, radarr /media/movies) and confirm each target string EXACTLY matches an ' +
          'accessible root folder. If a target does not match a live root folder, STOP and report — do not write a non-existent path.',
        'BACK UP: `kubectl -n ' + args.namespace + ' exec deploy/seerr -- cp /app/config/settings.json /app/config/settings.json.bak-issue250`. Confirm the backup exists and is non-empty.',
        'EDIT settings.json in place inside the pod (use node to parse+mutate+write the JSON safely, NOT sed): for the radarr[] entry set activeDirectory="' + args.seerrRoots.radarr + '"; ' +
          'for the sonarr[] entry named like the HD Sonarr set activeDirectory="' + args.seerrRoots.sonarr + '"; for the Sonarr2/anime entry set activeDirectory="' + args.seerrRoots.sonarr2 + '". ' +
          'Do NOT modify is4k, hostname, apiKey, or any other field (is4k mismap is a SEPARATE issue, out of scope for #250). Preserve JSON structure/formatting as much as possible.',
        'RESTART seerr so it reloads settings: `kubectl -n ' + args.namespace + ' rollout restart deploy/seerr && kubectl -n ' + args.namespace + ' rollout status deploy/seerr --timeout=180s`.',
        'VERIFY persisted: after restart, `kubectl -n ' + args.namespace + ' exec deploy/seerr -- cat /app/config/settings.json` and confirm the three activeDirectory values are the new /media/* strings. ' +
          'Then via the Seerr API confirm Seerr can resolve the Sonarr/Radarr root folders (e.g. GET /api/v1/service/sonarr/<id> and /service/radarr/<id> return the new /media root in their rootFolders/serverId and that the default selected matches). ' +
          'If verification fails, restore the backup, restart, and report.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['backupCreated', 'servicesUpdated', 'seerrRestarted', 'persisted', 'rootFoldersResolvable', 'summary'],
      properties: {
        backupCreated: { type: 'boolean' },
        servicesUpdated: { type: 'array', items: { type: 'object' } },
        seerrRestarted: { type: 'boolean' },
        persisted: { type: 'boolean' },
        rootFoldersResolvable: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 2 — prove the TV request path end-to-end (live verify, refinable)
// ---------------------------------------------------------------------------
const verifyTvRequestTask = defineTask('verify-tv-request', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Retry request 479 (tmdb ' + (args && args.tmdbId) + ') end-to-end: Seerr -> Sonarr add at /media/tvshows, no 400',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr SRE proving the Seerr root-folder fix end-to-end',
      task:
        'Re-drive the previously FAILED TV request 479 (tmdbId ' + args.tmdbId + ') through Seerr and confirm Sonarr now ACCEPTS the add (no RootFolderExistsValidator 400, ' +
        'series created under /media/tvshows, Seerr request leaves FAILED). Read Seerr + Sonarr logs as proof.',
      context: { ...args },
      instructions: [
        'Attempt ' + (args.attempt || 1) + '. Prior feedback: ' + (args.feedback || '(none)') + '.',
        'Use the Seerr API (main.apiKey from settings.json; node http inside the seerr pod). First GET /api/v1/request/479 to see current status. To retry a FAILED request, ' +
          'POST /api/v1/request/479/retry (jellyseerr retry endpoint). If retry is unavailable or the request object is unrecoverable, DELETE the failed request then re-create it ' +
          '(POST /api/v1/request with mediaType=tv, mediaId from the tmdb lookup, the appropriate seasons) so Seerr re-sends the add to Sonarr with the now-correct /media/tvshows root.',
        'Confirm SUCCESS with hard evidence: (a) Seerr request 479 (or its replacement) status is no longer 4/FAILED and mediaInfo.serviceId is non-null; ' +
          '(b) Seerr logs show the Sonarr add succeeded (no "Request failed with status code 400" / RootFolderExistsValidator); ' +
          '(c) Sonarr (`/api/v3/series` lookup by tvdb/title) shows the series added with path under /media/tvshows. Capture the relevant log lines.',
        'If it STILL 400s, capture the exact Sonarr/Seerr error and set proven=false with a precise failureReason (e.g. activeDirectory still wrong, root folder mismatch, ' +
          'season/monitor payload issue) so the plan can be refined and the Seerr fix re-applied.',
        'Do NOT print any media release/show title into anything that will be committed; refer to it as request 479 / tmdb ' + args.tmdbId + '.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['proven', 'requestStatusAfter', 'serviceIdAfter', 'sonarrSeriesAdded', 'seriesPath', 'failureReason', 'summary'],
      properties: {
        proven: { type: 'boolean' },
        requestStatusAfter: { type: ['number', 'string', 'null'] },
        serviceIdAfter: { type: ['number', 'string', 'null'] },
        sonarrSeriesAdded: { type: 'boolean' },
        seriesPath: { type: ['string', 'null'] },
        failureReason: { type: ['string', 'null'] },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 3 — migrate bazarr + lingarr manifests to the unified /media mount (git, branch+PR, NO merge)
// ---------------------------------------------------------------------------
const manifestMigrateTask = defineTask('manifest-migrate', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Migrate bazarr + lingarr to a single servarr-media -> /media mount; kustomize build; branch + open PR (no merge)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'GitOps SRE migrating bazarr + lingarr onto the unified /media mount (matching sonarr/radarr)',
      task:
        'On a fresh branch off origin/main, change bazarr.yaml + lingarr.yaml so each mounts the UNIFIED servarr-media PVC at a single /media root (dropping the old ' +
        'servarr-media-movies -> /movies and servarr-media-tvshows -> /tv mounts), validate with kustomize build, and open a PR. DO NOT MERGE — merge is a separate gated step.',
      context: { ...args },
      instructions: [
        'Branch: `cd ' + args.repoRoot + ' && git fetch origin && git switch -c ' + args.branch + ' origin/main`.',
        'EDIT 2-k3s/08.servarr/bazarr/bazarr.yaml: replace the two volumeMounts (name: movies -> /movies, name: tv -> /tv) with a single { name: media, mountPath: /media }, ' +
          'and replace the two volumes (movies -> claimName servarr-media-movies, tv -> claimName servarr-media-tvshows) with a single { name: media, persistentVolumeClaim: { claimName: servarr-media } }. ' +
          'Mirror sonarr.yaml/radarr.yaml exactly (same servarr-media claim, same /media mountPath). bazarr path_mappings are empty so /media/tvshows + /media/movies will then match Sonarr/Radarr with no remap.',
        'EDIT 2-k3s/08.servarr/lingarr/lingarr.yaml: same migration — single { name: media, mountPath: /media } volumeMount + single servarr-media volume, dropping the /movies and /tv mounts. ' +
          'Keep the /app/config mount untouched.',
        'Update the comment block in 2-k3s/08.servarr/_shared/storage/media-pvcs.yaml: bazarr + lingarr are now migrated to the unified servarr-media /media mount, so the legacy ' +
          'servarr-media-movies / servarr-media-tvshows PV/PVC no longer have any consumer (they remain ONLY as the #195 soak rollback and are now eligible for teardown in the #195 teardown follow-up). ' +
          'Do NOT delete the legacy PV/PVC in this PR (they are the soak rollback; teardown is a separate follow-up).',
        'VALIDATE: run `kustomize build 2-k3s/08.servarr` (use the repo-pinned kustomize) and confirm it renders clean. Confirm no plaintext kind: Secret was introduced (pre-commit guard).',
        'COMMIT (conventional message, e.g. `fix(servarr): mount bazarr + lingarr on unified /media root (#250, #195)`, body explaining the old /tv /movies roots are gone post-#195) ' +
          'ending with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Push and `gh pr create` against ' + args.repo + ' with a ## Test plan checklist ' +
          '(ArgoCD Synced/Healthy; bazarr+lingarr pods remount /media; both can list /media/tvshows + /media/movies; bazarr subtitle path access OK; lingarr healthy). PR body ends with the ' +
          '"🤖 Generated with [Claude Code](https://claude.com/claude-code)" line. DO NOT MERGE. No media titles, no secrets, no .a5c/.history/artifacts in the commit.',
        'Return ONLY the structured JSON result (prUrl + branch + filesChanged + kustomizeBuildPassed).',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'branch', 'filesChanged', 'kustomizeBuildPassed', 'summary'],
      properties: {
        prUrl: { type: 'string' },
        branch: { type: 'string' },
        filesChanged: { type: 'array', items: { type: 'string' } },
        kustomizeBuildPassed: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 4 — merge PR -> ArgoCD sync -> verify bazarr/lingarr remount + reconcile unmapped (live deploy)
// ---------------------------------------------------------------------------
const deployVerifyTask = defineTask('deploy-verify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Merge PR (rebase->validate->merge), ArgoCD sync, verify bazarr/lingarr on /media + reconcile Sonarr unmapped folders',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'GitOps/Servarr SRE completing the bazarr/lingarr cutover onto the unified /media mount',
      task:
        'Merge the bazarr/lingarr migration PR per the repo merge policy, let ArgoCD reconcile the servarr Application Healthy with bazarr+lingarr remounted at /media, ' +
        'verify both can access /media/tvshows + /media/movies and remain healthy, and reconcile/document Sonarr\'s 4 unmappedFolders. ',
      context: { ...args },
      instructions: [
        'MERGE per repo policy: on branch ' + args.branch + ' `git fetch origin && git rebase origin/main && git push --force-with-lease`; wait for the required `validate` ' +
          'check (`gh pr checks ' + args.prUrl + ' --watch`); then `gh pr merge ' + args.prUrl + ' --merge`. If validate fails or the branch cannot fast-forward, fix and re-push; do not force-merge.',
        'ARGOCD: confirm the servarr Application reconciles to Synced/Healthy (`kubectl -n argocd get applications.argoproj.io servarr -o jsonpath=...` or argocd CLI). bazarr + lingarr ' +
          'Deployments should roll to new pods. Wait for `kubectl -n ' + args.namespace + ' rollout status deploy/bazarr` and `deploy/lingarr` (timeout 180s each).',
        'VERIFY REMOUNT: `kubectl -n ' + args.namespace + ' describe pod` (or get -o yaml) for the new bazarr + lingarr pods — confirm a SINGLE servarr-media volume mounted at /media ' +
          'and that /movies and /tv mounts are GONE. Then exec into each: `ls /media` shows tvshows/movies/animes/downloads; bazarr can `ls /media/tvshows /media/movies` and ' +
          'reach a real series/movie path that Sonarr/Radarr report (confirms empty path_mappings now resolve). For lingarr, confirm it is healthy and, if it has an internal media-path/source-directory ' +
          'setting, that it points at /media (repoint live if needed and flag as a PVC-only follow-up). Capture evidence.',
        'RECONCILE UNMAPPED: re-fetch Sonarr HD /api/v3/rootFolder unmappedFolders. Classify: utility dirs (import/private/template) are expected/benign; any orphan series folder with ' +
          'no series record is documented for owner decision (do NOT auto-add or delete). Confirm there are no series whose path is still legacy /tv (there were 0). Record the final unmapped list + classification.',
        'If merge/validate cannot complete cleanly, leave the PR open and report (do not claim deployed).',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['merged', 'argoHealthy', 'bazarrOnMedia', 'lingarrOnMedia', 'mediaAccessOk', 'unmappedReconciled', 'unmappedClassification', 'summary'],
      properties: {
        merged: { type: 'boolean' },
        argoHealthy: { type: 'boolean' },
        bazarrOnMedia: { type: 'boolean' },
        lingarrOnMedia: { type: 'boolean' },
        mediaAccessOk: { type: 'boolean' },
        unmappedReconciled: { type: 'boolean' },
        unmappedClassification: { type: 'array', items: { type: 'object' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
// PHASE 5 — wrap up: tick #250 boxes, run PR test plan, open follow-ups, close #250
// ---------------------------------------------------------------------------
const wrapupTask = defineTask('wrapup', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Execute #250 checklist + manifest PR test plan, open follow-ups, close #250 (per repo policy)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE closing out issue #250 per the Epaflix repo Critical Rules',
      task: 'Persist the outcome: tick the #250 desired-outcome boxes, run the merged PR test plan, open follow-up issues, and close #250. Only the actions that are actually TRUE.',
      context: { ...args },
      instructions: [
        'STATE OF PLAY (be accurate in all text): Seerr service Default Root Folders repointed to /media/tvshows (Sonarr), /media/animes (Sonarr2), /media/movies (Radarr) — runtime config, ' +
          'settings.json backed up; request 479 retried end-to-end and Sonarr add ' + (args.tvRequestProven ? 'SUCCEEDED (no 400, serviceId populated)' : 'NOT yet proven — note honestly') + '. ' +
          'bazarr + lingarr migrated to the single servarr-media /media mount via PR ' + (args.prUrl || '(see PR)') + ' (ArgoCD ' + (args.argoHealthy ? 'Synced/Healthy' : 'NOT confirmed healthy') + '). ' +
          'Sonarr unmappedFolders reconciled = benign utility dirs + orphan(s) documented (no library auto-mutation). is4k mismap on Sonarr2 is OUT OF SCOPE (separate issue).',
        'EDIT ISSUE #250 BODY (not a comment) to tick the desired-outcome checkboxes that are now TRUE: Seerr root folders set; bazarr+lingarr migrated; unmapped reconciled/documented; request 479 retried & confirmed. ' +
          'Strike-through with a short reason any box not done. Then post ONE summary comment with evidence (root folder strings, request 479 after-status, PR link, ArgoCD health).',
        'EXECUTE THE PR TEST PLAN of the merged manifest PR ' + (args.prUrl || '') + ': tick each box that is now TRUE by EDITING the PR description (NEVER a new comment); strike-through any deferred step with a reason. Be truthful.',
        'FOLLOW-UPS (gh issues on ' + args.repo + ', enhancement shape `## Finding` / `## Current state` / `## Desired outcome` / `## Notes`, cross-link #195 #240 #250). Open ONLY ones that do not already exist (search first): ' +
          '(a) the legacy servarr-media-movies / servarr-media-tvshows PV/PVC + the 4 old NFS exports/node mounts are now consumer-free (bazarr/lingarr migrated) and ready for teardown after soak — if a #195 teardown follow-up already exists, ADD A COMMENT noting this precondition is met instead of opening a duplicate; ' +
          '(b) Seerr Sonarr2 is4k mismap (out of #250 scope) — cross-link any existing issue, do not duplicate; ' +
          '(c) any orphan unmapped folder needing an owner decision (re-add as a series or remove the folder); ' +
          '(d) if lingarr needed a live internal media-path repoint, codify it (PVC-only -> SOPS seed / config follow-up).',
        (args.closeIssue
          ? 'CLOSE #250: after ticking the boxes and posting the summary, `gh issue close 250 --repo ' + args.repo + '` with a closing note. Only close if Seerr is fixed AND request 479 proven AND bazarr/lingarr merged+healthy; if any of those is not done, DO NOT close — leave #250 open with a status comment.'
          : 'DO NOT close #250. Post a status comment summarizing what was done and what remains, and leave it OPEN.'),
        'No secrets, no .history, no .a5c, no artifacts committed. No media release/show titles in any commit, PR, issue, or doc (refer to request 479 / tmdb ' + args.tmdbId + ' / seriesId only).',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['issueBoxesTicked', 'prTestPlanExecuted', 'followUpIssues', 'issueClosed', 'summary'],
      properties: {
        issueBoxesTicked: { type: 'boolean' },
        prTestPlanExecuted: { type: 'boolean' },
        followUpIssues: { type: 'array', items: { type: 'string' } },
        issueClosed: { type: 'boolean' },
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
    branch: inputs.branch || 'issue-250-bazarr-lingarr-media-mount',
    issue: inputs.issue || 250,
    seerrRoots: inputs.seerrRoots || { sonarr: '/media/tvshows', sonarr2: '/media/animes', radarr: '/media/movies' },
    requestId: inputs.requestId || 479,
    tmdbId: inputs.tmdbId || 124364,
  };

  ctx.log('info', `#250 servarr root-folder fix: Seerr roots -> ${JSON.stringify(cfg.seerrRoots)}; bazarr/lingarr -> unified /media`);

  // PHASE 0 — capture + plan (read-only), owner gate, refine loop
  let plan = await ctx.task(captureStateTask, { ...cfg });
  ctx.log('info', `Baseline: request479.status=${plan.request479 && plan.request479.status}; risks=${(plan.risks || []).length}`);

  let lastFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    const gateA = await ctx.breakpoint({
      question: 'Approve the #250 fix plan? (A) Seerr root folders -> /media/{tvshows,animes,movies} [runtime], (B) bazarr+lingarr -> unified /media mount [PR], (C) re-request 479 + reconcile unmapped. Reply "abort" to stop.',
      title: '#250 Fix Plan Gate',
      context: {
        runId: ctx.runId,
        fixPlan: plan.fixPlan,
        seerrServices: plan.seerrServices,
        liveRootFolders: plan.liveRootFolders,
        risks: plan.risks,
      },
      expert: 'owner',
      tags: ['approval-gate'],
      previousFeedback: lastFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    if (gateA.approved && !(gateA.response || '').toLowerCase().includes('abort')) break;
    if ((gateA.response || '').toLowerCase().includes('abort') || (!gateA.approved && attempt === 2)) {
      ctx.log('warn', 'Plan not approved — stopping after read-only capture.');
      return { success: false, reason: 'plan-not-approved', seerrFixed: false, tvRequestProven: false, manifestsMigrated: false, plan, feedback: gateA.response || gateA.feedback || '' };
    }
    lastFeedback = gateA.response || gateA.feedback || 'Changes requested';
    plan = await ctx.task(refinePlanTask, { ...cfg, prior: plan, feedback: lastFeedback });
  }

  // PHASE 1 — Seerr runtime root-folder fix (live config)
  const seerr = await ctx.task(seerrFixTask, { ...cfg });
  ctx.log('info', `Seerr fix: persisted=${seerr.persisted}; rootFoldersResolvable=${seerr.rootFoldersResolvable}; servicesUpdated=${(seerr.servicesUpdated || []).length}`);
  if (!seerr.persisted || !seerr.rootFoldersResolvable) {
    return { success: false, reason: 'seerr-fix-failed', seerrFixed: false, tvRequestProven: false, manifestsMigrated: false, seerr, summary: seerr.summary };
  }

  // PHASE 2 — prove TV request end-to-end (live verify, refine loop re-applying the seerr fix if needed)
  let tv = await ctx.task(verifyTvRequestTask, { ...cfg, attempt: 1 });
  ctx.log('info', `Request 479 retry: proven=${tv.proven}; statusAfter=${tv.requestStatusAfter}; serviceIdAfter=${tv.serviceIdAfter}`);
  for (let attempt = 2; attempt <= 3 && !tv.proven; attempt++) {
    ctx.log('warn', `TV request not yet proven (${tv.failureReason}); re-checking Seerr config and retrying (attempt ${attempt}).`);
    await ctx.task(seerrFixTask, { ...cfg, feedback: tv.failureReason });
    tv = await ctx.task(verifyTvRequestTask, { ...cfg, attempt, feedback: tv.failureReason });
    ctx.log('info', `Request 479 retry attempt ${attempt}: proven=${tv.proven}; statusAfter=${tv.requestStatusAfter}`);
  }
  if (!tv.proven) {
    return { success: false, reason: 'tv-request-not-proven', seerrFixed: true, tvRequestProven: false, manifestsMigrated: false, tv, summary: tv.summary };
  }

  // PHASE 3 — bazarr/lingarr manifest migration (branch + PR, no merge)
  const pr = await ctx.task(manifestMigrateTask, { ...cfg });
  ctx.log('info', `Manifest PR: ${pr.prUrl}; kustomizeBuild=${pr.kustomizeBuildPassed}`);
  if (!pr.kustomizeBuildPassed || !pr.prUrl) {
    return { success: false, reason: 'manifest-pr-failed', seerrFixed: true, tvRequestProven: true, manifestsMigrated: false, pr, summary: pr.summary };
  }

  // PHASE 3 gate — deploy/merge (owner-gated; auto-approved in yolo)
  const gateB = await ctx.breakpoint({
    question: 'Approve the bazarr/lingarr DEPLOY? This rebases+merges PR ' + pr.prUrl + ' (validate -> --merge) and lets ArgoCD remount bazarr+lingarr onto the unified /media. Reply "abort" to leave the PR open.',
    title: '#250 bazarr/lingarr Deploy Gate',
    context: { runId: ctx.runId, prUrl: pr.prUrl, filesChanged: pr.filesChanged },
    expert: 'owner',
    tags: ['approval-gate', 'deploy'],
  });
  if (!gateB.approved || (gateB.response || '').toLowerCase().includes('abort')) {
    ctx.log('warn', 'Deploy not approved — Seerr fix is live + proven; manifest PR left open.');
    return { success: true, partial: true, reason: 'deploy-not-approved', seerrFixed: true, tvRequestProven: true, manifestsMigrated: false, prUrl: pr.prUrl, feedback: gateB.response || '' };
  }

  // PHASE 4 — merge + ArgoCD sync + verify remount + reconcile unmapped (live)
  const dep = await ctx.task(deployVerifyTask, { ...cfg, prUrl: pr.prUrl, branch: pr.branch });
  ctx.log('info', `Deploy: merged=${dep.merged}; argoHealthy=${dep.argoHealthy}; bazarrOnMedia=${dep.bazarrOnMedia}; lingarrOnMedia=${dep.lingarrOnMedia}; mediaAccessOk=${dep.mediaAccessOk}`);
  if (!dep.merged || !dep.argoHealthy || !dep.bazarrOnMedia || !dep.lingarrOnMedia || !dep.mediaAccessOk) {
    return { success: false, reason: 'deploy-incomplete', seerrFixed: true, tvRequestProven: true, manifestsMigrated: dep.merged, prUrl: pr.prUrl, dep, summary: dep.summary };
  }

  // PHASE 5 gate — wrap-up (owner-gated; auto-approved in yolo)
  const gateC = await ctx.breakpoint({
    question: 'All #250 fixes applied + verified (Seerr roots fixed + request 479 proven; bazarr/lingarr on /media + ArgoCD Healthy). Approve WRAP-UP (tick #250 boxes, run PR test plan, open follow-ups, close #250)? Reply "stop here" to skip wrap-up.',
    title: '#250 Wrap-up Gate',
    context: { runId: ctx.runId, prUrl: pr.prUrl, unmapped: dep.unmappedClassification },
    expert: 'owner',
    tags: ['approval-gate'],
  });
  if (!gateC.approved || (gateC.response || '').toLowerCase().includes('stop here')) {
    ctx.log('warn', 'Stopped before wrap-up by owner.');
    return { success: true, partial: true, reason: 'stopped-before-wrapup', seerrFixed: true, tvRequestProven: true, manifestsMigrated: true, argoHealthy: true, prUrl: pr.prUrl };
  }

  const wrap = await ctx.task(wrapupTask, {
    ...cfg,
    prUrl: pr.prUrl,
    tvRequestProven: tv.proven,
    argoHealthy: dep.argoHealthy,
    unmappedClassification: dep.unmappedClassification,
    closeIssue: true,
  });
  ctx.log('info', `Wrap-up: boxesTicked=${wrap.issueBoxesTicked}; testPlan=${wrap.prTestPlanExecuted}; followUps=${JSON.stringify(wrap.followUpIssues)}; closed=${wrap.issueClosed}`);

  return {
    success: true,
    seerrFixed: true,
    tvRequestProven: tv.proven,
    manifestsMigrated: true,
    prUrl: pr.prUrl,
    argoHealthy: dep.argoHealthy,
    unmappedReconciled: dep.unmappedReconciled,
    followUpIssues: wrap.followUpIssues,
    issueClosed: wrap.issueClosed,
    summary: wrap.summary,
  };
}
