/**
 * @process specializations/devops-sre-platform/cleanuparr-s04e07-triage
 * @description Incident triage + remediation for a single stalled torrent surfaced by issue #139:
 *   Sonarr seriesId 40 / S04E07, hash 828ea9eb36f00f821772d4d431dddf12ea6bd0c2, sitting as
 *   `stalledDL` in qbittorrent. Distinct from the S04E13 runaway (#138/PR #181) — left untouched by
 *   that fix. Triage whether Sonarr still wants the episode, decide whether to let it complete,
 *   replace the release, or remove the torrent + clear the Sonarr queue. Adversarially verify the
 *   chosen action, apply the APPROVED remediation live, verify the torrent is gone / queue clear and
 *   that it is NOT silently re-arming via the newtarr 15-min hunt + Sonarr autoRedownloadFailed
 *   (same re-arm mechanism as #138), then close #139 and open follow-ups per repo policy.
 *
 *   Live-change risk: remediation mutates running qbittorrent / Sonarr state (removes a download,
 *   clears a queue item, possibly unmonitors/blocklists) — gated by a mandatory deploy breakpoint.
 *   Closing #139 + any gh issue / doc / git mutation is gated by a separate outward-facing breakpoint.
 *
 * @inputs { repoRoot, namespace, seriesId, episode, hash, cleanuparrUrl, sonarrUrl, qbittorrentUrl, issueNumber, repo }
 * @outputs { success, sonarrStillWants, recommendedAction, remediationApplied, fixed, reArmRiskCleared, issueClosed, summary }
 *
 * @agent general-purpose (kubectl/exec/curl/git/gh executor + classification/verification)
 * @skill systematic-debugging superpowers:systematic-debugging
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

// ---------------------------------------------------------------------------
// PHASE 1 — triage (read-only): is the torrent real, does Sonarr still want it,
// and is it at risk of re-arming?
// ---------------------------------------------------------------------------
const triageTask = defineTask('triage', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Triage stalled S04E07 torrent (read-only)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr/Kubernetes SRE triaging a single stalled download on the Epaflix k3s cluster',
      task:
        'Triage the stalled torrent for Sonarr seriesId ' + args.seriesId + ' ' + args.episode +
        ' (hash ' + args.hash + ', reported state stalledDL in qbittorrent). DO NOT change anything — read-only. ' +
        'Determine: (a) does the torrent still exist and what is its real state/progress/peers? ' +
        '(b) does Sonarr still WANT this episode (monitored + missing)? (c) is it currently in the Sonarr queue, and ' +
        'with what trackedDownloadState/status? (d) is it striking in Cleanuparr or on a Sonarr/Cleanuparr blocklist? ' +
        '(e) is it at risk of silently re-arming via the newtarr 15-min hunt + Sonarr autoRedownloadFailed?',
      context: { ...args },
      instructions: [
        'Apply systematic-debugging discipline: gather evidence before forming a hypothesis; check event/queue timestamps ' +
          'before assuming a live loop (the #138 sibling turned out to be STALE March residue, not an active runaway).',
        'qbittorrent: find the torrent by hash ' + args.hash + '. Read its state, progress, num_seeds/num_peers, category, ' +
          'added_on, and time_active. Use the qbt WebUI API (read creds/url from the qbittorrent deploy env/config) or ' +
          '`kubectl -n ' + args.namespace + ' exec deploy/qbittorrent -- ...`. Confirm whether it is genuinely stalled/abandoned ' +
          '(no peers, no progress) vs slowly progressing.',
        'Sonarr (MAIN, category tv-sonarr — seriesId 40 lives there per prior triage, NOT sonarr2/anime; confirm): read the API ' +
          'key from config.xml (`kubectl -n ' + args.namespace + ' exec deploy/sonarr -- cat /config/config.xml`). Query the API: ' +
          'GET /api/v3/series/' + args.seriesId + ' and the episode for ' + args.episode + ' to get its episodeId + monitored flag + ' +
          'hasFile; GET /api/v3/queue (find the record matching this downloadId/hash, note trackedDownloadState, status, ' +
          'statusMessages, downloadId); GET /api/v3/blocklist (is this release/hash blocklisted?); GET /api/v3/history for the ' +
          'add->grab->stall cycle.',
        'Cleanuparr (v2, JSON config on PVC): check whether this hash is striking — read recent events/logs and the per-arr ' +
          'queue_cleaner failed_import_max_strikes (prior state: Sonarr striking OFF, failed_import_max_strikes=-1). ' +
          'Confirm whether Cleanuparr is even acting on this item.',
        'Re-arm risk: confirm the newtarr hunt cadence (hunt_missing_items / seasons_packs ~3600s GLOBAL per #135) and Sonarr ' +
          'autoRedownloadFailed. If S04E07 is monitored+missing, newtarr WILL re-hunt it and Sonarr may re-grab on failure — ' +
          'the same mechanism as #138. Note whether removing the torrent alone would just trigger an immediate re-grab.',
        'Decide a single recommendedAction from: "let-complete" (it is actually progressing / wanted and healthy), ' +
          '"replace" (remove this dead release, let Sonarr/newtarr grab a better one — only if a healthy alternative is likely), ' +
          '"remove-and-clear" (abandoned junk: remove torrent + clear Sonarr queue, and unmonitor or blocklist to prevent ' +
          're-arm), or "no-op" (nothing actionable / already resolved). Justify with evidence. Produce EXACT ordered steps ' +
          '(qbt removal, Sonarr queue DELETE with the right removeFromClient/blocklist/skipRedownload flags, monitor/blocklist ' +
          'toggles) for the recommended action. Do not apply them.',
        'Return ONLY the structured JSON result, not a plan. Save raw captures under tasks/' + taskCtx.effectId + '/ if useful.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['torrentState', 'sonarrStillWants', 'queueRecord', 'cleanuparrState', 'reArmRisk', 'recommendedAction', 'actionSteps', 'rationale', 'summary'],
      properties: {
        torrentState: { type: 'object' },
        sonarrStillWants: { type: 'boolean' },
        queueRecord: { type: 'object' },
        cleanuparrState: { type: 'object' },
        reArmRisk: { type: 'object' },
        recommendedAction: { type: 'string', enum: ['let-complete', 'replace', 'remove-and-clear', 'no-op'] },
        actionSteps: { type: 'array', items: { type: 'object' } },
        rationale: { type: 'string' },
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
// PHASE 2 — adversarially verify the recommended action (read-only)
// ---------------------------------------------------------------------------
const verifyActionTask = defineTask('verify-action', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Adversarially verify the recommended triage action',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Skeptical SRE reviewer trying to REFUTE a proposed triage action',
      task:
        'Try to refute the recommended action for the stalled S04E07 torrent. Default to refuted=true if the evidence is not ' +
        'airtight. Confirm only if evidence directly supports it. Read-only.',
      context: { ...args },
      instructions: [
        'Re-check the strongest counter-explanations: (a) is the torrent actually still progressing (so removal would lose a ' +
          'wanted download)? (b) if "remove-and-clear", will removing it WITHOUT unmonitoring/blocklisting just cause an ' +
          'immediate newtarr/Sonarr re-grab (re-arm)? (c) if "replace", is a healthy alternative release actually available, or ' +
          'will it just re-grab the same dead release? (d) is the Sonarr queue record stale vs live?',
        'Verify the claimed state against the live systems — do not trust the prior task blindly. Re-read qbt torrent state, the ' +
          'Sonarr queue record + episode monitored/hasFile, and the newtarr/autoRedownloadFailed re-arm settings.',
        'If the action holds, sharpen it into an exact, ordered, minimal set of steps (qbt removal flags, Sonarr queue DELETE ' +
          'query params, monitor/blocklist toggles) that ALSO closes the re-arm path where required. If refuted, give the ' +
          'corrected action + corrected steps.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['confirmed', 'finalAction', 'finalSteps', 'reArmClosedBySteps', 'risks', 'summary'],
      properties: {
        confirmed: { type: 'boolean' },
        finalAction: { type: 'string', enum: ['let-complete', 'replace', 'remove-and-clear', 'no-op'] },
        finalSteps: { type: 'array', items: { type: 'object' } },
        reArmClosedBySteps: { type: 'boolean' },
        risks: { type: 'array', items: { type: 'string' } },
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
// PHASE 3 — apply approved remediation (live change)
// ---------------------------------------------------------------------------
const remediateTask = defineTask('remediate', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Apply approved remediation for the stalled S04E07 torrent (live)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr SRE applying an APPROVED remediation on the live cluster',
      task:
        'Apply EXACTLY the approved remediation steps for hash ' + args.hash + ' (' + args.episode + '). ' +
        'Do only what was approved — no scope creep.',
      context: { ...args },
      instructions: [
        'Apply each approved step in order. Capture before/after evidence so the change is auditable.',
        'qbittorrent removal: remove the torrent (with or without data per the approved steps — abandoned junk with no library ' +
          'file may be removed with data; do NOT delete data for anything seeding into the library). Use the qbt API or ' +
          '`kubectl -n ' + args.namespace + ' exec deploy/qbittorrent -- ...`.',
        'Sonarr queue clear: DELETE the queue record with the approved flags (removeFromClient / blocklist / skipRedownload) so ' +
          'the queue item is cleared AND, where approved, the re-arm path is closed (unmonitor the episode and/or add to the ' +
          'Sonarr blocklist / Cleanuparr content-blocker so newtarr + autoRedownloadFailed do not immediately re-grab it).',
        'If editing any Cleanuparr config or content-blocker list on the PVC directly, take a backup copy first (cp to *.bak ' +
          'inside /config). Note that PVC-only changes are not in git (capture as a follow-up).',
        'If a step fails, STOP, do not improvise destructive actions, and report what was applied vs not.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['appliedSteps', 'torrentRemoved', 'queueCleared', 'reArmClosed', 'backupTaken', 'residualRisk', 'summary'],
      properties: {
        appliedSteps: { type: 'array', items: { type: 'object' } },
        torrentRemoved: { type: 'boolean' },
        queueCleared: { type: 'boolean' },
        reArmClosed: { type: 'boolean' },
        backupTaken: { type: 'boolean' },
        residualRisk: { type: 'array', items: { type: 'string' } },
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
// PHASE 4 — verify the remediation (read-only)
// ---------------------------------------------------------------------------
const verifyFixTask = defineTask('verify-fix', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify S04E07 is resolved and not re-arming',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE verifying the remediation actually resolved the stalled torrent without re-arming',
      task:
        'Confirm the stalled S04E07 torrent is resolved per the approved action and is NOT silently re-arming. Read-only.',
      context: { ...args },
      instructions: [
        'For a removal action: confirm hash ' + args.hash + ' is gone from qbittorrent and the Sonarr queue record is cleared. ' +
          'For let-complete: confirm it is genuinely progressing now. For no-op: confirm nothing needed doing.',
        'Re-arm check: confirm the episode monitored/blocklist state matches the approved target so newtarr 15-min hunt + Sonarr ' +
          'autoRedownloadFailed will NOT immediately re-grab the same dead release. If the action intentionally left it ' +
          'monitored (to grab a better release), confirm that is the intended state, not an accident.',
        'Set fixed=true ONLY if the queue/torrent state matches the approved action AND the re-arm path is in its intended ' +
          'state. List any residual concern (e.g. needs a short soak to confirm it does not reappear within ~2 hunt cycles).',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['fixed', 'reArmRiskCleared', 'evidence', 'needsSoak', 'summary'],
      properties: {
        fixed: { type: 'boolean' },
        reArmRiskCleared: { type: 'boolean' },
        evidence: { type: 'array', items: { type: 'string' } },
        needsSoak: { type: 'boolean' },
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
// PHASE 5 — close #139 + follow-ups (outward-facing), gated
// ---------------------------------------------------------------------------
const wrapupTask = defineTask('wrapup', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Close issue #139 + open follow-ups (per repo policy)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE closing the loop on the Epaflix repo per its Critical Rules',
      task:
        'Record the triage outcome on issue #' + args.issueNumber + ', close it if resolved, and open a gh follow-up for any ' +
        'deferred item (e.g. short soak to confirm it does not reappear, or durable capture of any PVC-only blocklist change). ' +
        'Only perform the approved actions.',
      context: { ...args },
      instructions: [
        'Add a closing comment to issue #' + args.issueNumber + ' on ' + args.repo + ' summarising: torrent state found, whether ' +
          'Sonarr still wanted the episode, the action taken, verification result, and the re-arm risk disposition. Close the ' +
          'issue (`gh issue close`) only if the verification confirmed it is resolved.',
        'Per CLAUDE.md: open a `gh issue` on ' + args.repo + ' for any follow-up using the enhancement shape ' +
          '(## Finding / ## Current state / ## Desired outcome / ## Notes), cross-linking #138 / #139 and the newtarr/Cleanuparr ' +
          'race issues if relevant. Only open a follow-up if there is genuinely deferred work (do not create empty noise).',
        'If a soak is recommended, either open a soak-confirmation follow-up issue OR note it clearly in the #139 closing comment.',
        'Do NOT commit secrets or plaintext Secret YAML. If a git/doc commit is needed (e.g. codifying a blocklist line), branch ' +
          '+ PR per the merge policy (rebase onto origin/main, push --force-with-lease, wait for validate, gh pr merge --merge) ' +
          '— but only if approved.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['issueClosed', 'closingCommentUrl', 'followUpIssueUrl', 'gitAction', 'summary'],
      properties: {
        issueClosed: { type: 'boolean' },
        closingCommentUrl: { type: 'string' },
        followUpIssueUrl: { type: 'string' },
        gitAction: { type: 'string' },
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
    namespace: 'servarr',
    seriesId: 40,
    episode: 'S04E07',
    hash: '828ea9eb36f00f821772d4d431dddf12ea6bd0c2',
    cleanuparrUrl: 'https://cleanuparr.epaflix.com',
    sonarrUrl: 'https://sonarr.epaflix.com',
    qbittorrentUrl: 'https://qbittorrent.epaflix.com',
    issueNumber: 139,
    repo: 'SpyrosPsarras/epaflix',
    ...inputs,
  };

  ctx.log('info', `S04E07 triage: seriesId ${cfg.seriesId} ${cfg.episode} hash ${cfg.hash} (issue #${cfg.issueNumber})`);

  // PHASE 1 — triage (read-only)
  const triage = await ctx.task(triageTask, {
    repoRoot: cfg.repoRoot, namespace: cfg.namespace, seriesId: cfg.seriesId, episode: cfg.episode,
    hash: cfg.hash, cleanuparrUrl: cfg.cleanuparrUrl, sonarrUrl: cfg.sonarrUrl, qbittorrentUrl: cfg.qbittorrentUrl,
  });
  ctx.log('info', `Triage: sonarrStillWants=${triage.sonarrStillWants}; recommendedAction=${triage.recommendedAction}`);

  // PHASE 2 — adversarial verify of recommended action (read-only)
  const va = await ctx.task(verifyActionTask, {
    namespace: cfg.namespace, hash: cfg.hash, episode: cfg.episode, triage,
  });
  ctx.log('info', `Action verify confirmed=${va.confirmed}: finalAction=${va.finalAction}`);

  // If no action is needed, skip the deploy gate — go straight to wrap-up (still gated).
  const actionIsLive = va.finalAction !== 'no-op';

  let applied = null;
  let verify = null;

  if (actionIsLive) {
    // GATE 1 (deploy / live-change) — mandatory before any mutation.
    const remGate = await ctx.breakpoint({
      question:
        'Approve applying the LIVE remediation for the stalled S04E07 torrent (#' + cfg.issueNumber + ')?\n\n' +
        'Sonarr still wants the episode: ' + triage.sonarrStillWants + '\n' +
        'Chosen action' + (va.confirmed ? ' (confirmed)' : ' (UNCONFIRMED — verifier refuted the first recommendation)') + ': ' +
        va.finalAction + '\n' +
        'Re-arm path closed by these steps: ' + va.reArmClosedBySteps + '\n\n' +
        'Exact steps:\n' + JSON.stringify(va.finalSteps, null, 2) + '\n\n' +
        'Risks: ' + JSON.stringify(va.risks) + '\n\n' +
        'This mutates live qbittorrent / Sonarr state (removes a download, clears a queue item, possibly unmonitors/blocklists). Apply now?',
      options: ['Approve remediation', 'Request changes', 'Abort'],
      expert: 'owner',
      tags: ['deploy', 'servarr', 'approval-gate'],
    });
    if (!remGate.approved || (remGate.response || '').toLowerCase().includes('abort')) {
      ctx.log('warn', 'Remediation not approved — stopping after read-only triage.');
      return {
        success: false, sonarrStillWants: triage.sonarrStillWants, recommendedAction: va.finalAction,
        remediationApplied: false, fixed: false, reArmRiskCleared: false, issueClosed: false,
        reason: 'remediation-not-approved', triage, verify: va, feedback: remGate.response || remGate.feedback || '',
      };
    }

    // PHASE 3 — apply approved remediation (live)
    applied = await ctx.task(remediateTask, {
      namespace: cfg.namespace, hash: cfg.hash, episode: cfg.episode, seriesId: cfg.seriesId,
      finalAction: va.finalAction, approvedSteps: va.finalSteps, ownerFeedback: remGate.response || '',
    });
    ctx.log('info', `Remediation: torrentRemoved=${applied.torrentRemoved}; queueCleared=${applied.queueCleared}; reArmClosed=${applied.reArmClosed}`);

    // PHASE 4 — verify the fix (read-only)
    verify = await ctx.task(verifyFixTask, {
      namespace: cfg.namespace, hash: cfg.hash, episode: cfg.episode, seriesId: cfg.seriesId,
      finalAction: va.finalAction, approvedSteps: va.finalSteps,
    });
    if (!verify.fixed) {
      const recover = await ctx.breakpoint({
        question:
          'Post-remediation verification did NOT confirm S04E07 is resolved.\n' +
          'Evidence: ' + JSON.stringify(verify.evidence) + '\n' +
          'Summary: ' + verify.summary + '\n\n' +
          'How to proceed?',
        options: ['Re-verify (transient)', 'Continue anyway (accept state)', 'Stop here'],
        expert: 'owner',
        tags: ['verification-gate'],
      });
      const r = (recover.response || '').toLowerCase();
      if (recover.approved && r.includes('re-verify')) {
        verify = await ctx.task(verifyFixTask, {
          namespace: cfg.namespace, hash: cfg.hash, episode: cfg.episode, seriesId: cfg.seriesId,
          finalAction: va.finalAction, approvedSteps: va.finalSteps, attempt: 2,
        });
      } else if (!recover.approved || r.includes('stop')) {
        return {
          success: false, sonarrStillWants: triage.sonarrStillWants, recommendedAction: va.finalAction,
          remediationApplied: true, fixed: false, reArmRiskCleared: false, issueClosed: false,
          reason: 'verification-stop', applied, verify,
        };
      }
    }
  } else {
    ctx.log('info', 'Triage concluded no live action is needed (no-op).');
  }

  // GATE 2 (outward-facing) — approve closing #139 + follow-up issue + any doc/git mutation.
  const wrapGate = await ctx.breakpoint({
    question:
      'Triage complete. Action: ' + va.finalAction +
      (actionIsLive ? ('; fixed=' + (verify ? verify.fixed : false) + (verify && verify.needsSoak ? ', soak recommended' : '')) : ' (no live change)') + '.\n\n' +
      'Per repo policy, post a closing comment on issue #' + cfg.issueNumber + ' and close it if resolved, and open a gh ' +
      'follow-up for any deferred item (soak confirmation / durable capture of any PVC-only change). Approve closing #' +
      cfg.issueNumber + ' + follow-ups?',
    options: ['Approve close + follow-up', 'Comment only (do not close)', 'Skip'],
    expert: 'owner',
    tags: ['outward-facing', 'approval-gate'],
  });

  let wrap = null;
  const wr = (wrapGate.response || '').toLowerCase();
  if (wrapGate.approved && !wr.includes('skip')) {
    wrap = await ctx.task(wrapupTask, {
      repoRoot: cfg.repoRoot, repo: cfg.repo, issueNumber: cfg.issueNumber, namespace: cfg.namespace,
      hash: cfg.hash, episode: cfg.episode, seriesId: cfg.seriesId,
      action: va.finalAction, triage, verify, applied,
      closeIssue: !wr.includes('comment only'),
      needsSoak: verify ? verify.needsSoak : false,
    });
    ctx.log('info', `Wrap-up: issueClosed=${wrap.issueClosed}; followUp=${wrap.followUpIssueUrl}; git=${wrap.gitAction}`);
  } else {
    ctx.log('warn', 'Wrap-up skipped by owner.');
  }

  return {
    success: true,
    sonarrStillWants: triage.sonarrStillWants,
    recommendedAction: va.finalAction,
    remediationApplied: actionIsLive,
    fixed: actionIsLive ? (verify ? verify.fixed : false) : true,
    reArmRiskCleared: actionIsLive ? (verify ? verify.reArmRiskCleared : false) : true,
    needsSoak: verify ? verify.needsSoak : false,
    issueClosed: wrap ? wrap.issueClosed : false,
    followUpIssueUrl: wrap ? wrap.followUpIssueUrl : null,
    summary: (verify && verify.summary) || triage.summary,
  };
}
