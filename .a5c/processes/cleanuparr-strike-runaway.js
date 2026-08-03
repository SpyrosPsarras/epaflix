/**
 * @process specializations/devops-sre-platform/cleanuparr-strike-runaway
 * @description Incident investigation + mitigation for a Cleanuparr "Download keeps coming
 *   back after deletion" runaway: a single release (sonarr seriesId 40 / episodeId 3143 / S04E13, hash
 *   66a4dc...) has accumulated strikeCount 248 across 76 "Action Required" events despite the
 *   user believing all mitigations are in place. Diagnose the real root cause (Cleanuparr
 *   queue/download-cleaner + blocklist config vs *arr "remove + redownload/blocklist"
 *   settings vs an indexer/qbt loop), adversarially verify it, then apply an approved live
 *   mitigation (Cleanuparr config + *arr blocklist + remove the stuck item), verify the
 *   strike count stops climbing, and persist a follow-up issue + doc note per repo policy.
 *
 *   Live-change risk: applying mitigation mutates running Cleanuparr / *arr config and
 *   removes a live download — gated by a mandatory deploy breakpoint. Any git/issue
 *   mutation is gated by a separate destructive-git/outward-facing breakpoint.
 *
 * @inputs { repoRoot, namespace, itemName, hash, strikeCount, eventCount, cleanuparrUrl }
 * @outputs { success, rootCauseConfirmed, mitigationApplied, fixed, issueUrl, summary }
 *
 * @agent general-purpose (kubectl/exec/curl/git/gh executor + classification/verification)
 * @skill systematic-debugging superpowers:systematic-debugging
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';

// ---------------------------------------------------------------------------
// PHASE 1 — diagnose (read-only)
// ---------------------------------------------------------------------------
const diagnoseTask = defineTask('diagnose', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Diagnose Cleanuparr strike runaway (read-only, root-cause)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr/Kubernetes SRE debugging a Cleanuparr cleanup loop on the Epaflix k3s cluster',
      task:
        'Find the REAL root cause of why download "' + args.itemName + '" (hash ' + args.hash + ') ' +
        'keeps coming back after Cleanuparr deletes it, reaching strikeCount ' + args.strikeCount +
        ' across ' + args.eventCount + ' "Action Required" events. DO NOT change anything — read-only.',
      context: { ...args },
      instructions: [
        'Apply systematic-debugging discipline: gather evidence before forming a hypothesis; do not guess.',
        'Cleanuparr is v2+ (JSON config, not SQLite). Read its live config from the config PVC: ' +
          '`kubectl -n ' + args.namespace + ' exec deploy/cleanuparr -- sh -c "ls -la /config && cat /config/*.json"` ' +
          '(look for queue_cleaner / download_cleaner / content_blocker / blacklist/whitelist, strike thresholds, ' +
          'and the arr instance bindings). If config is under a subdir, find it (find /config -name "*.json").',
        'Capture the EXACT Cleanuparr settings that govern re-grab: (1) is the Queue Cleaner / Download Cleaner enabled and ' +
          'does it BLOCKLIST the release when removing (vs just removing from client)? (2) the strike threshold / max strikes; ' +
          '(3) whether "remove from arr" / "trigger search after removal" is on; (4) the blocklist/blocklist-path patterns and ' +
          'whether they actually match this release name.',
        'Identify which *arr owns this item (S04E13 -> likely sonarr or sonarr2 anime). For that arr, capture the ' +
          'download-client "Remove" + "Redownload"/blocklist behaviour and whether the failed/stalled release is being ' +
          'blocklisted on removal. Use: `kubectl -n ' + args.namespace + ' exec deploy/<arr> -- sh -c "..."` against its ' +
          'config.xml/api, or query the arr API with its API key (read the key from config.xml). Check the arr Blocklist for ' +
          'this release/hash and the History for the add->grab->import-fail->remove->re-grab cycle.',
        'Check qbittorrent for the hash ' + args.hash + ': is the torrent still present/stalled/erroring, and is it being ' +
          're-added repeatedly? (qbt api or `kubectl -n ' + args.namespace + ' exec deploy/qbittorrent -- ...`).',
        'Cross-reference the known Sonarr2/Cleanuparr race (Huntarr->Newtarr hunt re-grabs + Cleanuparr pattern list too ' +
          'narrow). Newtarr was just migrated (108m old) — check whether Newtarr is re-hunting/re-grabbing this release on a ' +
          'schedule, defeating the deletion.',
        'Form a single, evidence-backed root cause: WHY does deletion not stick? (e.g. arr re-grabs because removal does not ' +
          'blocklist; OR Cleanuparr removes from client but never tells the arr; OR Newtarr re-hunts; OR blocklist pattern ' +
          'does not match). List the concrete config gaps that the user THINKS are fixed but are not.',
        'Produce concrete mitigationCandidates with EXACT settings/values/steps (Cleanuparr config keys + *arr settings + the ' +
          'one-off cleanup of this specific item/hash). Do not apply them.',
        'Return ONLY the structured JSON result, not a plan. Save raw captures under tasks/' + taskCtx.effectId + '/ if useful.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['owningArr', 'cleanuparrConfig', 'arrRemoveSettings', 'itemState', 'rootCause', 'configGaps', 'mitigationCandidates', 'summary'],
      properties: {
        owningArr: { type: 'string' },
        cleanuparrConfig: { type: 'object' },
        arrRemoveSettings: { type: 'object' },
        itemState: { type: 'object' },
        rootCause: { type: 'string' },
        configGaps: { type: 'array', items: { type: 'string' } },
        mitigationCandidates: { type: 'array', items: { type: 'object' } },
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
// PHASE 2 — adversarially verify the root cause (read-only)
// ---------------------------------------------------------------------------
const verifyRootCauseTask = defineTask('verify-root-cause', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Adversarially verify the diagnosed root cause',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Skeptical SRE reviewer trying to REFUTE a proposed root cause',
      task:
        'Try to refute the diagnosed root cause for the Cleanuparr strike runaway. Default to refuted=true if the evidence ' +
        'is not airtight. Confirm only if the evidence directly supports it. Read-only.',
      context: { ...args },
      instructions: [
        'Re-check the strongest counter-explanations: (a) is it actually a DIFFERENT arr re-grabbing? (b) is the blocklist ' +
          'silently failing because removal happens at the client level only? (c) is Newtarr/an indexer the re-grab source ' +
          'rather than the arr import loop? (d) is strikeCount climbing because the item is genuinely stuck and never reaches ' +
          'the removal threshold?',
        'Verify the claimed config gaps against the live config you re-read — do not trust the prior task blindly. Re-read the ' +
          'Cleanuparr JSON and the owning arr settings.',
        'If the root cause holds, sharpen the recommendedMitigation into an exact, ordered, minimal set of changes (Cleanuparr ' +
          'config keys + values, *arr setting toggles, the one-off cleanup of hash ' + args.hash + '). If refuted, give the ' +
          'corrected root cause and corrected mitigation.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['confirmed', 'finalRootCause', 'recommendedMitigation', 'risks', 'summary'],
      properties: {
        confirmed: { type: 'boolean' },
        finalRootCause: { type: 'string' },
        recommendedMitigation: { type: 'array', items: { type: 'object' } },
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
// PHASE 3 — apply approved mitigation (live change)
// ---------------------------------------------------------------------------
const applyMitigationTask = defineTask('apply-mitigation', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Apply approved mitigation (Cleanuparr + *arr config + cleanup stuck item)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr SRE applying an APPROVED mitigation on the live cluster',
      task:
        'Apply EXACTLY the approved mitigation steps and clean up the stuck item (hash ' + args.hash + '). ' +
        'Do only what was approved — no scope creep.',
      context: { ...args },
      instructions: [
        'Apply each approved step in order. For Cleanuparr v2 config changes, prefer the UI/API; if editing the JSON on the ' +
          'PVC directly, take a backup copy first (cp the json to *.bak inside /config) and restart the deployment so it ' +
          'reloads: `kubectl -n ' + args.namespace + ' rollout restart deploy/cleanuparr` then wait for rollout.',
        'For *arr changes (enable blocklist-on-removal / "Remove" + "Redownload" handling), apply via the arr API or settings; ' +
          'add this release/hash to the arr Blocklist so it is not re-grabbed.',
        'One-off cleanup of the offending item: remove the torrent (with data if it is junk) from qbittorrent and ensure the ' +
          'owning arr blocklists it. If Newtarr is re-hunting this title, exclude/pause that hunt for it.',
        'Take a before/after snapshot of the relevant config so the change is auditable. Capture command outputs.',
        'If a step fails, STOP, do not improvise destructive actions, and report what was applied vs not.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['appliedSteps', 'itemCleaned', 'backupTaken', 'residualRisk', 'summary'],
      properties: {
        appliedSteps: { type: 'array', items: { type: 'object' } },
        itemCleaned: { type: 'boolean' },
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
// PHASE 4 — verify the fix (read-only)
// ---------------------------------------------------------------------------
const verifyFixTask = defineTask('verify-fix', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify the strike runaway is stopped',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE verifying the mitigation actually stopped the loop',
      task:
        'Confirm the offending release no longer comes back and the strike loop is broken. Read-only.',
      context: { ...args },
      instructions: [
        'Confirm hash ' + args.hash + ' is gone from qbittorrent and is on the owning arr Blocklist (re-grab blocked).',
        'Confirm the applied Cleanuparr/arr config now matches the approved target (re-read live config).',
        'Check Cleanuparr recent events/logs to confirm no NEW "Action Required" / strike for this hash is being generated ' +
          'after the change (the strike counter should no longer increment). Note: pre-existing 76 events are historical.',
        'Set fixed=true ONLY if both re-grab is blocked AND config matches target. List any residual concern (e.g. needs a ' +
          'soak window to be certain).',
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
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// PHASE 5 — persist follow-up (doc note + gh issue), gated
// ---------------------------------------------------------------------------
const wrapupTask = defineTask('wrapup', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Persist follow-up: doc note + gh issue (per repo policy)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE closing the loop on the Epaflix repo per its Critical Rules',
      task:
        'Open a gh follow-up issue for any deferred item (e.g. soak window to confirm the strike loop stays dead, or durable ' +
        'manifest/doc capture of the config that lives only on the PVC) and note the incident + fix in the servarr docs. ' +
        'Only perform the approved actions.',
      context: { ...args },
      instructions: [
        'Per CLAUDE.md: open a `gh issue` on SpyrosPsarras/epaflix for the follow-up using the enhancement shape ' +
          '(## Finding / ## Current state / ## Desired outcome / ## Notes), cross-linking the Sonarr2/Cleanuparr race issues if relevant.',
        'If durable config now lives only on the Cleanuparr PVC (not in git manifests), record that gap in the issue so it is ' +
          'not lost on a PVC rebuild. Add a short note to 2-k3s/08.servarr/RECOVERY-newtarr-cleanuparr.md or the cleanuparr README.',
        'Do NOT commit secrets or plaintext Secret YAML. If a git commit is needed, branch + PR per the merge policy (rebase ' +
          'onto origin/main, push --force-with-lease, wait for validate, gh pr merge --merge) — but only if approved.',
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
    itemName: 'sonarr-seriesId-40-episodeId-3143-S04E13',
    hash: '66a4dc6201cb149ff70eed12b9902317cb82ed87',
    strikeCount: 248,
    eventCount: 76,
    cleanuparrUrl: 'https://cleanuparr.epaflix.com',
    repo: 'SpyrosPsarras/epaflix',
    ...inputs,
  };

  ctx.log('info', `Cleanuparr strike runaway: ${cfg.itemName} (hash ${cfg.hash}) strikeCount=${cfg.strikeCount}`);

  // PHASE 1 — diagnose (read-only)
  const diag = await ctx.task(diagnoseTask, {
    repoRoot: cfg.repoRoot, namespace: cfg.namespace, itemName: cfg.itemName,
    hash: cfg.hash, strikeCount: cfg.strikeCount, eventCount: cfg.eventCount, cleanuparrUrl: cfg.cleanuparrUrl,
  });
  ctx.log('info', `Diagnosis: owningArr=${diag.owningArr}; rootCause=${diag.rootCause}`);

  // PHASE 2 — adversarial verify of root cause (read-only)
  const rc = await ctx.task(verifyRootCauseTask, {
    namespace: cfg.namespace, hash: cfg.hash, diagnosis: diag,
  });
  ctx.log('info', `Root cause confirmed=${rc.confirmed}: ${rc.finalRootCause}`);

  // GATE 1 (deploy / live-change) — mandatory before any mutation.
  const mitGate = await ctx.breakpoint({
    question:
      'Approve applying the LIVE mitigation for the Cleanuparr strike runaway?\n\n' +
      'Owning arr: ' + diag.owningArr + '\n' +
      'Root cause' + (rc.confirmed ? ' (confirmed)' : ' (UNCONFIRMED — verifier refuted the first hypothesis)') + ':\n  ' +
      rc.finalRootCause + '\n\n' +
      'Config gaps the user believed were fixed:\n  ' + JSON.stringify(diag.configGaps, null, 0) + '\n\n' +
      'Proposed mitigation (exact steps):\n' +
      JSON.stringify(rc.recommendedMitigation, null, 2) + '\n\n' +
      'Risks: ' + JSON.stringify(rc.risks) + '\n\n' +
      'This mutates live Cleanuparr/*arr config and removes the stuck download. Apply now?',
    options: ['Approve mitigation', 'Request changes', 'Abort'],
    expert: 'owner',
    tags: ['deploy', 'servarr', 'approval-gate'],
  });
  if (!mitGate.approved || (mitGate.response || '').toLowerCase().includes('abort')) {
    ctx.log('warn', 'Mitigation not approved — stopping after read-only diagnosis.');
    return {
      success: false, rootCauseConfirmed: rc.confirmed, mitigationApplied: false, fixed: false,
      reason: 'mitigation-not-approved', rootCause: rc.finalRootCause, diagnosis: diag, verify: rc,
      feedback: mitGate.response || mitGate.feedback || '',
    };
  }

  // PHASE 3 — apply approved mitigation (live)
  const applied = await ctx.task(applyMitigationTask, {
    namespace: cfg.namespace, hash: cfg.hash, owningArr: diag.owningArr,
    approvedMitigation: rc.recommendedMitigation, ownerFeedback: mitGate.response || '',
  });
  ctx.log('info', `Mitigation applied: itemCleaned=${applied.itemCleaned}; steps=${(applied.appliedSteps || []).length}`);

  // PHASE 4 — verify the fix (read-only)
  let verify = await ctx.task(verifyFixTask, {
    namespace: cfg.namespace, hash: cfg.hash, owningArr: diag.owningArr,
    approvedMitigation: rc.recommendedMitigation, cleanuparrUrl: cfg.cleanuparrUrl,
  });
  if (!verify.fixed) {
    const recover = await ctx.breakpoint({
      question:
        'Post-mitigation verification did NOT confirm the loop is broken.\n' +
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
        namespace: cfg.namespace, hash: cfg.hash, owningArr: diag.owningArr,
        approvedMitigation: rc.recommendedMitigation, cleanuparrUrl: cfg.cleanuparrUrl, attempt: 2,
      });
    } else if (!recover.approved || r.includes('stop')) {
      return {
        success: false, rootCauseConfirmed: rc.confirmed, mitigationApplied: true, fixed: false,
        reason: 'verification-stop', applied, verify,
      };
    }
  }

  // GATE 2 (destructive-git / outward-facing) — approve follow-up issue + any doc/git mutation.
  const wrapGate = await ctx.breakpoint({
    question:
      'Mitigation applied and verified (fixed=' + verify.fixed + (verify.needsSoak ? ', soak recommended' : '') + ').\n\n' +
      'Per repo policy, open a gh follow-up issue (soak confirmation + durable capture of PVC-only config) and add a short ' +
      'doc note. Approve creating the issue + doc note (and a branch/PR if a manifest change is needed)?',
    options: ['Approve follow-up', 'Skip follow-up'],
    expert: 'owner',
    tags: ['destructive-git', 'outward-facing', 'approval-gate'],
  });

  let wrap = null;
  if (wrapGate.approved && !(wrapGate.response || '').toLowerCase().includes('skip')) {
    wrap = await ctx.task(wrapupTask, {
      repoRoot: cfg.repoRoot, repo: cfg.repo, namespace: cfg.namespace,
      hash: cfg.hash, itemName: cfg.itemName, rootCause: rc.finalRootCause,
      mitigation: rc.recommendedMitigation, verify, needsSoak: verify.needsSoak,
    });
    ctx.log('info', `Follow-up: issue=${wrap.issueUrl}; git=${wrap.gitAction}`);
  } else {
    ctx.log('warn', 'Follow-up skipped by owner.');
  }

  return {
    success: true,
    rootCauseConfirmed: rc.confirmed,
    rootCause: rc.finalRootCause,
    mitigationApplied: true,
    fixed: verify.fixed,
    needsSoak: verify.needsSoak,
    issueUrl: wrap ? wrap.issueUrl : null,
    summary: verify.summary,
  };
}
