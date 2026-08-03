/**
 * @process specializations/devops-sre-platform/cleanuparr-orphan-stalled
 * @description Investigate + fix qBittorrent stalled downloads that neither Cleanuparr nor
 *   Newtarr clean or replace. Confirmed pattern (series "Genius"): 9 torrents grabbed
 *   2026-05-08 then `downloadIgnored` out of Sonarr's queue while left in qbt — ORPHANED, so
 *   Cleanuparr QueueCleaner (which only strikes items present in an *arr queue) is structurally
 *   blind to them; episodes stay missing+monitored but huntarr (1 item/cycle, no blocklist of
 *   dead releases) recycles the same dead 0-seed grab. Fix = one-time removal of the orphaned
 *   dead torrents + blocklist + fresh search, and enable Cleanuparr DownloadCleaner unlinked/
 *   orphan rule so non-queue torrents get removed going forward; investigate the downloadIgnored
 *   path. Verify, then persist a follow-up issue + doc note per repo policy.
 *
 *   Live-change risk: deletes torrents WITH DATA from qbt and mutates Cleanuparr/*arr config —
 *   gated by a mandatory deploy breakpoint. Git/issue mutation gated separately.
 *
 * @inputs { repoRoot, namespace, seriesHint, cleanuparrUrl, sonarrUrl }
 * @outputs { success, rootCauseConfirmed, mitigationApplied, fixed, issueUrl, summary }
 *
 * @agent general-purpose (kubectl/exec/curl/qbt-api/git/gh executor + classification/verification)
 * @skill systematic-debugging superpowers:systematic-debugging
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';

// PHASE 1 — diagnose / orphan inventory (read-only)
const diagnoseTask = defineTask('diagnose', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Inventory stalled+orphaned torrents, Cleanuparr DownloadCleaner config, downloadIgnored cause (read-only)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr/Kubernetes SRE on the Epaflix k3s cluster',
      task:
        'Build the full evidence picture for stalled qbt downloads that are not being cleaned or replaced (the user reports ' +
        'MANY stalled; series "' + args.seriesHint + '" is the known example). DO NOT change anything — read-only.',
      context: { ...args },
      instructions: [
        'Apply systematic-debugging discipline: evidence first.',
        'Sonarr (main) at ' + args.sonarrUrl + ', X-Api-Key read from `kubectl -n ' + args.namespace + ' exec deploy/sonarr -- cat /config/config.xml`. Sonarr2 is the anime instance.',
        'Cleanuparr config = SQLite /config/cleanuparr.db (+ events.db) in deploy/cleanuparr; no sqlite3 CLI — use python3 in-pod.',
        'qbt creds are in cleanuparr.db download_clients table (or /config/qBittorrent/qBittorrent.conf). Use the qbt WebUI API read-only to list ALL torrents with state+category+name+hash+progress+num_seeds+added_on.',
        '1) ORPHAN/STALLED INVENTORY: list every torrent in state stalledDL/metaDL/missingFiles (and note stalledUP separately — those are usually fine seeders). For each broken one, determine whether ANY arr (sonarr/sonarr2/radarr) queue references its hash (cross-match against each `/api/v3/queue`). A broken torrent in NO arr queue = ORPHAN. Group by series; flag the "' + args.seriesHint + '" set explicitly.',
        '2) CLEANUPARR DOWNLOADCLEANER: dump download_cleaner_configs / unlinked_configs (and any orphan/unlinked rule). Is an unlinked/orphan removal rule ENABLED? values? Confirm QueueCleaner stall rules are working but blind to orphans (events.db: stallstrike vs queueitemdeleted counts).',
        '3) downloadIgnored CAUSE: in Sonarr `/api/v3/history` for the orphaned items, confirm the grab->downloadIgnored timeline and find WHY they were ignored (manual remove? a Cleanuparr remove-from-arr-not-client? blocklist? category). This is the upstream defect that creates orphans.',
        '4) REPLACEMENT GAP: confirm the orphaned episodes are missing+monitored (so they SHOULD be re-grabbed) and that huntarr/newtarr is configured but recycles the same dead releases (note whether the pod is huntarr vX or the newtarr v1.0.0 rewrite — there is a suspected discrepancy).',
        'Produce an EXACT proposed fix: the precise list of orphan hashes safe to delete-with-data (only 0/low-seed, dead, no-arr-queue ones), the exact Cleanuparr DownloadCleaner unlinked-rule change, the blocklist+search steps, and the downloadIgnored remediation. Do NOT apply.',
        'Return ONLY the structured JSON result. Save raw captures under tasks/' + taskCtx.effectId + '/ if useful.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['stalledSummary', 'orphans', 'downloadCleanerConfig', 'downloadIgnoredCause', 'replacementGap', 'rootCause', 'proposedFix', 'summary'],
      properties: {
        stalledSummary: { type: 'object' },
        orphans: { type: 'array', items: { type: 'object' } },
        downloadCleanerConfig: { type: 'object' },
        downloadIgnoredCause: { type: 'string' },
        replacementGap: { type: 'string' },
        rootCause: { type: 'string' },
        proposedFix: { type: 'array', items: { type: 'object' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 2 — adversarial verify of the safe-delete list + fix (read-only)
const verifyTask = defineTask('verify-plan', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Adversarially verify the orphan delete-list + proposed fix (read-only)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Skeptical SRE reviewer guarding against deleting a wanted torrent',
      task:
        'Re-verify, against live state, that every hash on the proposed delete-list is TRULY orphaned + dead + safe to remove ' +
        'with data, and that the Cleanuparr DownloadCleaner change is correct. Default to EXCLUDING any hash you cannot prove ' +
        'is safe. Read-only.',
      context: { ...args },
      instructions: [
        'For each proposed-delete hash: re-confirm via qbt API it is stalledDL/metaDL with 0 (or near-0) seeds and not progressing, ' +
          'and re-confirm via every arr `/api/v3/queue` that NO arr references it (true orphan). Exclude any that is in an arr queue, ' +
          'seeding/completed-and-wanted, or actively downloading.',
        'Confirm the unlinked/orphan DownloadCleaner rule change is the right mechanism and will not nuke legitimately-seeding ' +
          'torrents (e.g. respects a seeding/ratio guard). Sharpen its exact values.',
        'Confirm the blocklist+search steps target the right series/episodes and that the downloadIgnored remediation is sound.',
        'Produce the FINAL safe delete-list (hashes + names) and the FINAL exact ordered fix steps. Read-only — apply nothing.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['confirmed', 'safeDeleteList', 'excluded', 'finalFix', 'risks', 'summary'],
      properties: {
        confirmed: { type: 'boolean' },
        safeDeleteList: { type: 'array', items: { type: 'object' } },
        excluded: { type: 'array', items: { type: 'object' } },
        finalFix: { type: 'array', items: { type: 'object' } },
        risks: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 3 — apply approved fix (live, destructive: delete-with-data)
const applyTask = defineTask('apply-fix', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Apply approved fix: remove orphan torrents, blocklist, enable DownloadCleaner unlinked rule, search',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr SRE applying an APPROVED fix on the live cluster',
      task: 'Apply EXACTLY the approved steps in order. Delete ONLY the approved safe-delete hashes. No scope creep.',
      context: { ...args },
      instructions: [
        'Backup Cleanuparr DBs first inside the pod (cp cleanuparr.db/events.db to timestamped .bak).',
        'Remove ONLY the approved orphan hashes from qbittorrent WITH data (qbt API deleteFiles=true). Re-verify each hash is still ' +
          'orphaned+dead immediately before deleting; if any now appears in an arr queue or is progressing, SKIP it and report.',
        'In Sonarr, blocklist the dead releases for the affected episodes (so re-search avoids the same corpse) and trigger a fresh ' +
          'episode/season search (EpisodeSearch / SeriesSearch command) so live releases are grabbed under proper queue tracking.',
        'Enable/configure the Cleanuparr DownloadCleaner unlinked/orphan removal rule per the approved exact values (edit cleanuparr.db; ' +
          'if a reload is needed, `kubectl -n ' + args.namespace + ' rollout restart deploy/cleanuparr` and wait). Preserve any seeding/ratio guard so it does not remove wanted seeders.',
        'Apply the approved downloadIgnored remediation if it is a config change (otherwise capture it as a follow-up note).',
        'Capture before/after evidence per step. If a step fails, STOP that step, do not improvise, keep earlier steps intact, report applied-vs-not.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['appliedSteps', 'deletedHashes', 'skippedHashes', 'backupTaken', 'residualRisk', 'summary'],
      properties: {
        appliedSteps: { type: 'array', items: { type: 'object' } },
        deletedHashes: { type: 'array', items: { type: 'string' } },
        skippedHashes: { type: 'array', items: { type: 'string' } },
        backupTaken: { type: 'boolean' },
        residualRisk: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 4 — verify the fix (read-only)
const verifyFixTask = defineTask('verify-fix', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify orphans gone, DownloadCleaner rule active, replacements grabbing',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE verifying the orphan-cleanup fix took effect',
      task: 'Confirm the fix worked. Read-only.',
      context: { ...args },
      instructions: [
        'Confirm each deleted hash is gone from qbt and there are no remaining orphaned stalled torrents for the affected series.',
        'Confirm the Cleanuparr DownloadCleaner unlinked/orphan rule is now enabled with the approved values (re-read cleanuparr.db) and ' +
          'Cleanuparr is healthy after any restart (pod Ready, logs clean, arrs+qbt Healthy).',
        'Confirm Sonarr blocklisted the dead releases and a fresh search was triggered; check `/api/v3/queue` + history for NEW grabs that ' +
          'are now properly queue-tracked (or note that no live release exists yet — that is acceptable, the loop is structurally fixed).',
        'Set fixed=true ONLY if orphans removed AND DownloadCleaner rule active. needsSoak=true if a full DownloadCleaner cron cycle / search cycle has not yet been observed.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['fixed', 'evidence', 'needsSoak', 'summary'],
      properties: {
        fixed: { type: 'boolean' },
        evidence: { type: 'array', items: { type: 'string' } },
        needsSoak: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 5 — persist follow-up (issue + doc note), gated
const wrapupTask = defineTask('wrapup', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Persist follow-up: gh issue + doc note (per repo policy)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE closing the loop on the Epaflix repo per its Critical Rules',
      task: 'Open a gh follow-up issue + doc note for the deferred items. Only the approved actions.',
      context: { ...args },
      instructions: [
        'Open a `gh issue` on ' + args.repo + ' using the enhancement shape (## Finding / ## Current state / ## Desired outcome / ## Notes) covering: ' +
          '(a) the downloadIgnored root cause + permanent remediation so orphans stop being created; (b) durable capture of the Cleanuparr ' +
          'DownloadCleaner unlinked-rule config (PVC-only — lost on rebuild), cross-link the related #138; (c) the huntarr-version-vs-newtarr-v1.0.0 ' +
          'discrepancy to confirm the migration actually rolled out (cross-link #131/#137); (d) huntarr 1-item/cycle backlog drain.',
        'Add a concise doc note to 2-k3s/08.servarr/RECOVERY-newtarr-cleanuparr.md (orphan-stalled incident + fix + DownloadCleaner unlinked rule).',
        'If a git change is needed: branch off origin/main, commit only the doc change, conventional message ending with ' +
          '`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`; push, open PR (body ends with the Generated-with-Claude-Code line), ' +
          'rebase onto origin/main, push --force-with-lease, wait for `validate`, then `gh pr merge <n> --merge`. No secrets, no .history, no .a5c files. ' +
          'If validate/merge cannot complete cleanly, leave the PR open and report it.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['issueUrl', 'docNote', 'gitAction', 'summary'],
      properties: {
        issueUrl: { type: 'string' },
        docNote: { type: 'string' },
        gitAction: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// ---------------------------------------------------------------------------
export async function process(inputs, ctx) {
  const cfg = {
    repoRoot: '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
    namespace: 'servarr',
    seriesHint: 'Genius',
    cleanuparrUrl: 'https://cleanuparr.epaflix.com',
    sonarrUrl: 'https://sonarr.epaflix.com',
    repo: 'SpyrosPsarras/epaflix',
    ...inputs,
  };

  ctx.log('info', `Orphan-stalled cleanup: seriesHint=${cfg.seriesHint}`);

  // PHASE 1 — diagnose (read-only)
  const diag = await ctx.task(diagnoseTask, {
    repoRoot: cfg.repoRoot, namespace: cfg.namespace, seriesHint: cfg.seriesHint,
    cleanuparrUrl: cfg.cleanuparrUrl, sonarrUrl: cfg.sonarrUrl,
  });
  ctx.log('info', `Diagnosis: orphans=${(diag.orphans || []).length}; rootCause=${diag.rootCause}`);

  // PHASE 2 — adversarial verify of the delete-list + fix (read-only)
  const vp = await ctx.task(verifyTask, { namespace: cfg.namespace, diagnosis: diag });
  ctx.log('info', `Plan verified=${vp.confirmed}; safeDelete=${(vp.safeDeleteList || []).length}; excluded=${(vp.excluded || []).length}`);

  // GATE 1 (deploy / destructive: delete-with-data) — mandatory before any mutation.
  const gate1 = await ctx.breakpoint({
    question:
      'Approve applying the LIVE orphan-stalled fix?\n\n' +
      'Root cause: ' + diag.rootCause + '\n' +
      'downloadIgnored cause: ' + diag.downloadIgnoredCause + '\n\n' +
      'SAFE DELETE-WITH-DATA list (' + (vp.safeDeleteList || []).length + ' torrents):\n' +
      JSON.stringify(vp.safeDeleteList, null, 2) + '\n\n' +
      'Excluded (kept): ' + JSON.stringify(vp.excluded) + '\n\n' +
      'Final ordered fix (delete + blocklist + search + enable DownloadCleaner unlinked rule):\n' +
      JSON.stringify(vp.finalFix, null, 2) + '\n\n' +
      'Risks: ' + JSON.stringify(vp.risks) + '\n\n' +
      'This DELETES torrents WITH DATA from qbt and changes Cleanuparr/*arr config. Apply now?',
    options: ['Approve fix', 'Request changes', 'Abort'],
    expert: 'owner',
    tags: ['deploy', 'destructive', 'servarr', 'approval-gate'],
  });
  if (!gate1.approved || (gate1.response || '').toLowerCase().includes('abort')) {
    ctx.log('warn', 'Fix not approved — stopping after read-only diagnosis.');
    return {
      success: false, rootCauseConfirmed: vp.confirmed, mitigationApplied: false, fixed: false,
      reason: 'fix-not-approved', rootCause: diag.rootCause, diagnosis: diag, plan: vp,
      feedback: gate1.response || gate1.feedback || '',
    };
  }

  // PHASE 3 — apply approved fix (live)
  const applied = await ctx.task(applyTask, {
    namespace: cfg.namespace, safeDeleteList: vp.safeDeleteList, finalFix: vp.finalFix,
    ownerFeedback: gate1.response || '',
  });
  ctx.log('info', `Applied: deleted=${(applied.deletedHashes || []).length}; skipped=${(applied.skippedHashes || []).length}`);

  // PHASE 4 — verify (read-only)
  let verify = await ctx.task(verifyFixTask, {
    namespace: cfg.namespace, deletedHashes: applied.deletedHashes, finalFix: vp.finalFix,
    seriesHint: cfg.seriesHint, sonarrUrl: cfg.sonarrUrl,
  });
  if (!verify.fixed) {
    const recover = await ctx.breakpoint({
      question:
        'Post-fix verification did NOT confirm success.\n' +
        'Evidence: ' + JSON.stringify(verify.evidence) + '\n' +
        'Summary: ' + verify.summary + '\n\nHow to proceed?',
      options: ['Re-verify (transient)', 'Continue anyway (accept state)', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (recover.response || '').toLowerCase();
    if (recover.approved && r.includes('re-verify')) {
      verify = await ctx.task(verifyFixTask, {
        namespace: cfg.namespace, deletedHashes: applied.deletedHashes, finalFix: vp.finalFix,
        seriesHint: cfg.seriesHint, sonarrUrl: cfg.sonarrUrl, attempt: 2,
      });
    } else if (!recover.approved || r.includes('stop')) {
      return { success: false, rootCauseConfirmed: vp.confirmed, mitigationApplied: true, fixed: false, reason: 'verification-stop', applied, verify };
    }
  }

  // GATE 2 (destructive-git / outward) — approve follow-up issue + doc note.
  const gate2 = await ctx.breakpoint({
    question:
      'Fix applied and verified (fixed=' + verify.fixed + (verify.needsSoak ? ', soak recommended' : '') + ').\n\n' +
      'Open a gh follow-up issue (downloadIgnored root-cause remediation, durable DownloadCleaner config, huntarr/newtarr version ' +
      'discrepancy, huntarr backlog drain) + doc note? Approve creating the issue + doc note (+ branch/PR if needed)?',
    options: ['Approve follow-up', 'Skip follow-up'],
    expert: 'owner',
    tags: ['destructive-git', 'outward-facing', 'approval-gate'],
  });

  let wrap = null;
  if (gate2.approved && !(gate2.response || '').toLowerCase().includes('skip')) {
    wrap = await ctx.task(wrapupTask, {
      repoRoot: cfg.repoRoot, repo: cfg.repo, namespace: cfg.namespace,
      rootCause: diag.rootCause, downloadIgnoredCause: diag.downloadIgnoredCause,
      finalFix: vp.finalFix, applied, verify, needsSoak: verify.needsSoak,
    });
    ctx.log('info', `Follow-up: issue=${wrap.issueUrl}; git=${wrap.gitAction}`);
  } else {
    ctx.log('warn', 'Follow-up skipped by owner.');
  }

  return {
    success: true,
    rootCauseConfirmed: vp.confirmed,
    rootCause: diag.rootCause,
    mitigationApplied: true,
    deletedCount: (applied.deletedHashes || []).length,
    fixed: verify.fixed,
    needsSoak: verify.needsSoak,
    issueUrl: wrap ? wrap.issueUrl : null,
    summary: verify.summary,
  };
}
