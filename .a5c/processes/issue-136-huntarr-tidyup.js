/**
 * @process specializations/devops-sre-platform/issue-136-huntarr-tidyup
 * @description Post-migration tidy-up for the huntarr -> newtarr migration (#131). After a
 *   successful soak (newtarr v1.0.0 stable on ArgoCD), remove the benign on-disk remnants left
 *   behind:
 *     1) worker-61 safety tarball `huntarr-config-backup-20260531-pre-delete.tgz` (~32MB) under
 *        /var/lib/rancher/k3s/storage/ (rollback net, no longer needed once soak is good).
 *     2) worker-65 ORPHAN local-path dir `pvc-47b294c2..._servarr_huntarr-config` — a pre-existing
 *        OLD-cluster PVC backing dir with NO live PVC bound (NOT the PVC deleted in #131's run).
 *     3) stale `HUNTARR_*` env vars on the newtarr pod (injected from the now-deleted legacy
 *        huntarr Service) — inert, self-clear on next pod restart. VERIFICATION ONLY, no forced
 *        restart (owner decision: tags + env are inert, leave-on-restart).
 *
 *   Live-change risk: deletes files on cluster worker nodes over SSH. Gated by a mandatory
 *   destructive breakpoint that only fires AFTER read-only soak+safety verification proves
 *   newtarr is stable and the targets are truly orphaned/safe. Issue close-out gated separately.
 *
 *   No repo/manifest changes are expected (pure runtime/on-disk cleanup) -> no PR; close-out is a
 *   gh issue comment + close, plus a gh follow-up issue for anything that surfaces, per repo rules.
 *
 * @inputs { repoRoot, repo, namespace, issueNumber, worker61, worker65, storagePath, tarballName }
 * @outputs { success, soakStable, deleted, envCleared, issueClosed, summary }
 *
 * @agent general-purpose (kubectl/ssh/gh executor + verification)
 * @skill systematic-debugging superpowers:systematic-debugging
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';

// PHASE 1 — soak + safety verification (read-only)
const verifySafetyTask = defineTask('verify-safety', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify newtarr soak is good AND the two delete targets are truly orphaned/safe (read-only)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Kubernetes/Servarr SRE on the Epaflix k3s cluster doing pre-delete due diligence',
      task:
        'Prove (a) the huntarr->newtarr migration soak is GOOD so the rollback tarball is no longer needed, and ' +
        '(b) each on-disk delete target is exactly what issue #' + args.issueNumber + ' describes and is SAFE to delete. ' +
        'Read-only — change nothing.',
      context: { ...args },
      instructions: [
        'Apply systematic-debugging discipline: gather evidence first, assert nothing without proof.',
        'SOAK HEALTH: confirm the ArgoCD `servarr` Application is Synced + Healthy (`kubectl -n argocd get application servarr -o jsonpath`), ' +
          'the newtarr pod is 1/1 Running (`kubectl -n ' + args.namespace + ' get pods -l app=newtarr -o wide`), and its JSON config is intact ' +
          '(newtarr v1.0.0 keeps config under /config; `kubectl -n ' + args.namespace + ' exec deploy/newtarr -- ls -la /config` and spot-check the ' +
          'config JSON has its Sonarr/Sonarr2/Radarr instances + hunt settings). Conclude soakStable=true only if all green.',
        'TARBALL TARGET (worker-61, ' + args.worker61 + '): `ssh ubuntu@' + args.worker61 + " 'ls -la " + args.storagePath + args.tarballName + "'" + '` — ' +
          'confirm the file exists, capture its exact size and mtime. Confirm it is the pre-delete safety backup from #131.',
        'ORPHAN DIR TARGET (worker-65, ' + args.worker65 + '): `ssh ubuntu@' + args.worker65 + " 'ls -d " + args.storagePath + "*huntarr-config* 2>/dev/null; du -sh " + args.storagePath + "*huntarr-config* 2>/dev/null'" + '` — ' +
          'identify the exact dir name (expected to match `pvc-47b294c2..._servarr_huntarr-config`). CRITICAL: prove NO live PVC/PV is bound to it: ' +
          '`kubectl get pv,pvc -A | grep -i huntarr` must return NOTHING, and the pvc-<uuid> in the dir name must NOT correspond to any existing PV ' +
          '(`kubectl get pv | grep <uuid>`). This is the pre-existing OLD-cluster orphan, NOT a live volume. Default to NOT-safe if you cannot prove it is orphaned.',
        'STALE ENV: check the newtarr pod env for HUNTARR_* vars (`kubectl -n ' + args.namespace + ' exec deploy/newtarr -- env | grep -i ^HUNTARR_ || true`) ' +
          'and note the pod age/restart count. If the pod already restarted after the legacy huntarr Service was deleted, these will already be GONE — report which.',
        'Return ONLY the structured JSON result. Save raw captures under tasks/' + taskCtx.effectId + '/ if useful.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['soakStable', 'tarball', 'orphanDir', 'staleEnv', 'safeToDelete', 'risks', 'summary'],
      properties: {
        soakStable: { type: 'boolean' },
        tarball: { type: 'object', required: ['exists', 'path'], properties: { exists: { type: 'boolean' }, path: { type: 'string' }, size: { type: 'string' }, mtime: { type: 'string' } } },
        orphanDir: { type: 'object', required: ['exists', 'path', 'noLivePvc'], properties: { exists: { type: 'boolean' }, path: { type: 'string' }, size: { type: 'string' }, noLivePvc: { type: 'boolean' } } },
        staleEnv: { type: 'object', required: ['present'], properties: { present: { type: 'boolean' }, vars: { type: 'array', items: { type: 'string' } }, podAge: { type: 'string' }, willClearOnRestart: { type: 'boolean' } } },
        safeToDelete: { type: 'boolean' },
        risks: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 2 — apply deletions (live, destructive: rm files on worker nodes over SSH)
const deleteTask = defineTask('delete-remnants', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Delete the worker-61 safety tarball + worker-65 orphan huntarr-config dir (live, approved)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE applying an APPROVED destructive cleanup on the live Epaflix cluster',
      task: 'Delete EXACTLY the two approved targets over SSH. No scope creep. Re-verify each path immediately before removing it.',
      context: { ...args },
      instructions: [
        'Use the EXACT paths confirmed in the verification step (tarballPath, orphanDirPath). If a path differs from what was approved, SKIP it and report — do not improvise.',
        'TARBALL: `ssh ubuntu@' + args.worker61 + " 'ls -la <tarballPath>'" + '` to confirm it still exists, then `ssh ubuntu@' + args.worker61 + " 'rm -f <tarballPath> && ls -la <tarballPath> 2>&1 || echo DELETED'" + '`.',
        'ORPHAN DIR: re-confirm NO live PVC is bound (`kubectl get pv,pvc -A | grep -i huntarr` returns nothing) RIGHT BEFORE deleting; then ' +
          '`ssh ubuntu@' + args.worker65 + " 'rm -rf <orphanDirPath> && ls -d <orphanDirPath> 2>&1 || echo DELETED'" + '`. The dir name MUST contain `huntarr-config` and live under ' + args.storagePath + ' — never rm anything outside it.',
        'Do NOT touch the newtarr pod (no restart) — the stale HUNTARR_* env is inert and clears on its own next restart per owner decision.',
        'Capture before/after evidence for each rm. If either rm fails or a target is no longer the expected thing, STOP that target, keep the other intact, and report applied-vs-not.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['tarballDeleted', 'orphanDirDeleted', 'skipped', 'evidence', 'summary'],
      properties: {
        tarballDeleted: { type: 'boolean' },
        orphanDirDeleted: { type: 'boolean' },
        skipped: { type: 'array', items: { type: 'string' } },
        evidence: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 3 — verify cleanup (read-only)
const verifyDoneTask = defineTask('verify-done', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify tarball + orphan dir are gone and confirm stale HUNTARR_* env state (read-only)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE verifying the post-migration tidy-up landed',
      task: 'Confirm the cleanup took effect and the cluster is still healthy. Read-only.',
      context: { ...args },
      instructions: [
        'Confirm the worker-61 tarball is GONE: `ssh ubuntu@' + args.worker61 + " 'ls -la <tarballPath> 2>&1 || echo GONE'" + '`.',
        'Confirm the worker-65 orphan dir is GONE: `ssh ubuntu@' + args.worker65 + " 'ls -d <orphanDirPath> 2>&1 || echo GONE'" + '`.',
        'Confirm newtarr is still 1/1 Running and ArgoCD `servarr` still Synced/Healthy (cleanup must not have disturbed it).',
        'Re-check HUNTARR_* env on the newtarr pod. Report envCleared=true if absent; if still present, state they are inert and will clear on the next ' +
          'restart (no action — per owner decision). This is verification only.',
        'Set done=true only if BOTH targets are gone (or were already gone) AND newtarr is healthy.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['done', 'tarballGone', 'orphanDirGone', 'envCleared', 'newtarrHealthy', 'evidence', 'summary'],
      properties: {
        done: { type: 'boolean' },
        tarballGone: { type: 'boolean' },
        orphanDirGone: { type: 'boolean' },
        envCleared: { type: 'boolean' },
        newtarrHealthy: { type: 'boolean' },
        evidence: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 4 — close out issue #136 + any follow-ups (gh, outward-facing)
const closeoutTask = defineTask('closeout', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Close issue #' + args.issueNumber + ' with a summary + open any follow-up issues (per repo policy)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE closing the loop on the Epaflix repo per its Critical Rules',
      task: 'Record the outcome on issue #' + args.issueNumber + ', close it, and open a gh follow-up for anything still deferred.',
      context: { ...args },
      instructions: [
        'Post a concise closing comment on ' + args.repo + ' issue #' + args.issueNumber + ' (`gh issue comment`) summarizing the three Desired-outcome ' +
          'items: tarball deleted (worker-61), orphan dir removed (worker-65), and the HUNTARR_* env outcome (cleared / will-clear-on-restart). Include the ' +
          'key evidence (sizes, paths, pre/post). This is an ISSUE work-item (NOT a PR test plan), so a closing comment + close is correct.',
        'Close the issue: `gh issue close ' + args.issueNumber + ' --repo ' + args.repo + '`.',
        'Per repo Critical Rules, open a `gh issue` (enhancement shape: ## Finding / ## Current state / ## Desired outcome / ## Notes) for ANY item that ' +
          'surfaced and is still deferred (e.g. if the orphan dir could NOT be safely deleted, or env did not clear and a deliberate restart is wanted). ' +
          'If nothing is deferred, do NOT open a noise issue — just record that none was needed. Cross-link #131/#136 where relevant.',
        'No repo file changes are expected for this cleanup, so NO branch/PR. If you somehow find a doc that must change, branch off origin/main, commit only ' +
          'that change with a conventional message ending `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`, open a PR, rebase, ' +
          'push --force-with-lease, wait for `validate`, then `gh pr merge <n> --merge`. Never commit secrets/.history/.a5c.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['issueClosed', 'commentUrl', 'followUpIssues', 'summary'],
      properties: {
        issueClosed: { type: 'boolean' },
        commentUrl: { type: 'string' },
        followUpIssues: { type: 'array', items: { type: 'string' } },
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
    repo: 'SpyrosPsarras/epaflix',
    namespace: 'servarr',
    issueNumber: 136,
    worker61: '192.168.10.61',
    worker65: '192.168.10.65',
    storagePath: '/var/lib/rancher/k3s/storage/',
    tarballName: 'huntarr-config-backup-20260531-pre-delete.tgz',
    ...inputs,
  };

  ctx.log('info', `Issue #${cfg.issueNumber} huntarr tidy-up: tarball on ${cfg.worker61}, orphan dir on ${cfg.worker65}`);

  // PHASE 1 — soak + safety verification (read-only)
  const v = await ctx.task(verifySafetyTask, {
    repo: cfg.repo, namespace: cfg.namespace, issueNumber: cfg.issueNumber,
    worker61: cfg.worker61, worker65: cfg.worker65, storagePath: cfg.storagePath, tarballName: cfg.tarballName,
  });
  ctx.log('info', `Soak stable=${v.soakStable}; safeToDelete=${v.safeToDelete}; tarball=${v.tarball && v.tarball.exists}; orphanDir noLivePvc=${v.orphanDir && v.orphanDir.noLivePvc}`);

  // GATE 1 (destructive) — mandatory before any rm on the worker nodes.
  const gate1 = await ctx.breakpoint({
    question:
      'Approve deleting the huntarr migration remnants on the live cluster?\n\n' +
      'Soak stable (newtarr Synced/Healthy, config intact): ' + v.soakStable + '\n\n' +
      'TARBALL (worker-61 ' + cfg.worker61 + '):\n' + JSON.stringify(v.tarball, null, 2) + '\n\n' +
      'ORPHAN DIR (worker-65 ' + cfg.worker65 + ', must have NO live PVC):\n' + JSON.stringify(v.orphanDir, null, 2) + '\n\n' +
      'Stale HUNTARR_* env (verification-only, no restart): ' + JSON.stringify(v.staleEnv) + '\n\n' +
      'Risks: ' + JSON.stringify(v.risks) + '\n\n' +
      'This RM-deletes the tarball on worker-61 and rm -rf the orphan dir on worker-65. The newtarr pod is NOT touched. Proceed?',
    options: ['Approve deletions', 'Tarball only', 'Orphan dir only', 'Abort'],
    expert: 'owner',
    tags: ['destructive', 'deploy', 'servarr', 'approval-gate'],
  });
  const resp = (gate1.response || '').toLowerCase();
  if (!gate1.approved || resp.includes('abort')) {
    ctx.log('warn', 'Deletions not approved — stopping after read-only verification.');
    return { success: false, soakStable: v.soakStable, deleted: false, envCleared: false, issueClosed: false, reason: 'not-approved', verification: v, feedback: gate1.response || gate1.feedback || '' };
  }
  const doTarball = !resp.includes('orphan dir only');
  const doOrphan = !resp.includes('tarball only');

  // PHASE 2 — apply approved deletions (live)
  const del = await ctx.task(deleteTask, {
    worker61: cfg.worker61, worker65: cfg.worker65, storagePath: cfg.storagePath,
    tarballPath: (v.tarball && v.tarball.path) || (cfg.storagePath + cfg.tarballName),
    orphanDirPath: (v.orphanDir && v.orphanDir.path) || '',
    doTarball, doOrphan, ownerFeedback: gate1.response || '',
  });
  ctx.log('info', `Deleted tarball=${del.tarballDeleted}; orphanDir=${del.orphanDirDeleted}; skipped=${JSON.stringify(del.skipped)}`);

  // PHASE 3 — verify cleanup (read-only)
  let verify = await ctx.task(verifyDoneTask, {
    namespace: cfg.namespace, worker61: cfg.worker61, worker65: cfg.worker65,
    tarballPath: (v.tarball && v.tarball.path) || (cfg.storagePath + cfg.tarballName),
    orphanDirPath: (v.orphanDir && v.orphanDir.path) || '',
  });
  if (!verify.done) {
    const recover = await ctx.breakpoint({
      question: 'Post-cleanup verification did NOT confirm done.\nEvidence: ' + JSON.stringify(verify.evidence) + '\nSummary: ' + verify.summary + '\n\nHow to proceed?',
      options: ['Re-verify (transient)', 'Continue anyway', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (recover.response || '').toLowerCase();
    if (recover.approved && r.includes('re-verify')) {
      verify = await ctx.task(verifyDoneTask, {
        namespace: cfg.namespace, worker61: cfg.worker61, worker65: cfg.worker65,
        tarballPath: (v.tarball && v.tarball.path) || (cfg.storagePath + cfg.tarballName),
        orphanDirPath: (v.orphanDir && v.orphanDir.path) || '', attempt: 2,
      });
    } else if (!recover.approved || r.includes('stop')) {
      return { success: false, soakStable: v.soakStable, deleted: del.tarballDeleted || del.orphanDirDeleted, envCleared: verify.envCleared, issueClosed: false, reason: 'verification-stop', deletion: del, verify };
    }
  }

  // GATE 2 (outward-facing) — approve closing issue #136 + opening any follow-up.
  const gate2 = await ctx.breakpoint({
    question:
      'Cleanup verified (done=' + verify.done + ', tarballGone=' + verify.tarballGone + ', orphanDirGone=' + verify.orphanDirGone + ', envCleared=' + verify.envCleared + ').\n\n' +
      'Close issue #' + cfg.issueNumber + ' with a summary comment, and open a gh follow-up for anything still deferred (only if needed)?',
    options: ['Approve close-out', 'Skip close-out'],
    expert: 'owner',
    tags: ['outward-facing', 'approval-gate'],
  });

  let closeout = null;
  if (gate2.approved && !(gate2.response || '').toLowerCase().includes('skip')) {
    closeout = await ctx.task(closeoutTask, {
      repo: cfg.repo, repoRoot: cfg.repoRoot, issueNumber: cfg.issueNumber,
      deletion: del, verify, soakStable: v.soakStable, staleEnv: v.staleEnv,
    });
    ctx.log('info', `Close-out: issueClosed=${closeout.issueClosed}; followUps=${JSON.stringify(closeout.followUpIssues)}`);
  } else {
    ctx.log('warn', 'Close-out skipped by owner.');
  }

  return {
    success: true,
    soakStable: v.soakStable,
    deleted: del.tarballDeleted || del.orphanDirDeleted,
    tarballDeleted: del.tarballDeleted,
    orphanDirDeleted: del.orphanDirDeleted,
    envCleared: verify.envCleared,
    done: verify.done,
    issueClosed: closeout ? closeout.issueClosed : false,
    followUpIssues: closeout ? closeout.followUpIssues : [],
    summary: verify.summary,
  };
}
