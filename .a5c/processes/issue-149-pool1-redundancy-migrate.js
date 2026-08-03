// SPDX-License-Identifier: ISC
/**
 * @process specializations/devops-sre-platform/backup-restore-automation
 * @description Resolve Epaflix issue #149: the SOPS master age-key backup currently lives on
 *   `pool1/encrypted-backups`, but `pool1` is a NON-REDUNDANT 2-disk stripe (sdb 10TB IronWolf +
 *   sde 14TB Exos) — either disk failing = total, unrecoverable pool loss. Issue #149 asks to make
 *   the master-key backup's home redundant BEFORE relying on it long-term.
 *
 *   DECISION (owner-confirmed): Option C — MOVE `pool1/encrypted-backups` to the already-redundant
 *   `apps` pool (3×240G Kingston SSD RAIDZ1, ONLINE, ~230G free). Rationale: there are NO spare disks
 *   on the box (Options A "add mirror" and B "rebuild pool1" both need new 10TB+14TB hardware that
 *   does not exist), and the payload is only ~236K (the encrypted age-key backup). Moving it onto the
 *   redundant SSD pool achieves #149's goal with no hardware and without touching the 14T media on
 *   pool1. The age key also still has two INDEPENDENT copies (workstation ~/.config/sops/age +
 *   in-cluster KSOPS), so this tertiary copy is low-risk to relocate.
 *
 *   PASSPHRASE: reuse the EXISTING passphrase from secrets.yml (NO rotation). The destination is a new
 *   TrueNAS-native encrypted dataset created with that same passphrase; the ~236K file is copied in.
 *   (Raw `zfs send -w` is avoided unless preflight proves the TrueNAS keystore tracks it for unlock.)
 *
 *   SAFETY: this touches the master-key backup, so it is fully gated. Flow is ADDITIVE-FIRST —
 *   create new + copy + verify the new copy is byte-identical AND decrypts BEFORE the old dataset is
 *   ever destroyed. Three owner gates: (1) destructive+secrets — approve the exact migration command
 *   plan before ANY mutation; (2) destructive — approve retiring (destroying) the old pool1 dataset
 *   only after the redundant copy is independently verified; (3) deploy/destructive-git — approve the
 *   docs PR merge + #149 close.
 *
 *   CONCURRENCY: another babysitter is active in the same working tree. All git work happens in an
 *   ISOLATED `git worktree` branched from origin/main — NEVER `git stash`/`git checkout`/branch-switch
 *   in the shared working tree (prior incident destroyed untracked files that way).
 *
 *   Flow: preflight (READ-ONLY: confirm src dataset size/encryption props, apps RAIDZ1 health+free,
 *   locate the backup writer + every reference to the pool1 path, confirm independent key copies,
 *   choose the TrueNAS-native method + emit exact additive command plan + rollback) → GATE1
 *   (secrets+destructive: approve plan) → execute-migration (ADDITIVE: create encrypted apps dataset
 *   w/ same passphrase, copy file, repoint references; DO NOT destroy old) → verify-redundancy
 *   (quality loop: file on RAIDZ1, checksum match, unlock+decrypt works, apps healthy, key
 *   end-to-end recoverable) → GATE2 (destructive: approve retiring old) → retire-old (destroy old
 *   snapshot+dataset on pool1) → document-and-pr (isolated worktree: docs + cross-link #124/#57 +
 *   PR w/ test plan, NO merge) → GATE3 (deploy/git: approve merge+close) → publish-merge-close
 *   (rebase + merge per Epaflix policy, close #149, tick PR test plan, open follow-ups).
 *
 * @inputs { repoRoot, truenasSsh, srcDataset, srcPath, dstPool, dstDataset, dstPath, snapName, secretsRef, issue, repo, branch, worktreeDir, relatedIssues }
 * @outputs { success, decision, migrated, redundancyConfirmed, oldRetired, merged, prUrl, issueState, followUpIssues }
 *
 * @agent general-purpose TrueNAS ssh/zfs/midclt executor + git/gh + verification
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

// ---------------------------------------------------------------------------
// Phase 1 — preflight (READ-ONLY): ground truth + exact additive migration plan.
// ---------------------------------------------------------------------------
const preflightTask = defineTask('preflight', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Preflight: live TrueNAS facts + exact additive migration plan (Option C)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'TrueNAS SCALE / ZFS storage engineer preparing a master-key-backup relocation (issue #149)',
      task:
        'READ-ONLY preflight for moving the SOPS master age-key backup off the non-redundant stripe ' +
        '`' + args.srcDataset + '` onto the redundant RAIDZ1 `' + args.dstPool + '` pool (Option C). ' +
        'Make NO changes. SSH: `' + args.truenasSsh + '` (use sudo where reads need it). Gather ground truth ' +
        'and emit an EXACT, copy-pasteable additive command plan + rollback for the owner to approve.',
      context: { ...args },
      instructions: [
        'Confirm source dataset `' + args.srcDataset + '`: `zfs get -o property,value used,referenced,encryption,keyformat,keylocation,keystatus,mounted,mountpoint ' + args.srcDataset + '`. Record size (expected ~236K) and encryption props.',
        'List the ACTUAL contents under ' + args.srcPath + ' (e.g. `sudo ls -la ' + args.srcPath + '` + `sudo find ' + args.srcPath + ' -type f -exec sha256sum {} +`). Identify exactly which files are the age-key backup. Record per-file sha256.',
        'Confirm destination pool `' + args.dstPool + '` is RAIDZ1 + ONLINE + healthy + has free space: `zpool status ' + args.dstPool + '` and `zpool list ' + args.dstPool + '`. Confirm `' + args.dstDataset + '` does NOT already exist.',
        'Determine the CLEANEST TrueNAS-native creation method for an ENCRYPTED dataset `' + args.dstDataset + '` reusing the SAME passphrase as the source (NO rotation), so the TrueNAS keystore/UI can unlock it. Prefer `midclt call pool.dataset.create` with encryption_options (passphrase) if that is how the source was made; fall back to `zfs create -o encryption=aes-256-gcm -o keyformat=passphrase`. Note: avoid raw `zfs send -w` unless you can prove TrueNAS will track the received dataset for unlock.',
        'LOCATE the backup writer + EVERY reference to the source path/dataset: grep the repo (' + args.repoRoot + ') for `pool1/encrypted-backups` and `encrypted-backups`; check TrueNAS for any cron job / script / replication task / Custom App that writes the age key there (`sudo crontab -l`, `midclt call cronjob.query`, `midclt call replication.query`). Determine whether the backup is placed MANUALLY (one-time, per #57) or by an automated job. List every reference that must be repointed to ' + args.dstPath + '.',
        'Confirm the two INDEPENDENT age-key copies exist as documented (workstation ~/.config/sops/age on this host, and in-cluster KSOPS secret) so the relocation is safe. Note their presence; do not exfiltrate key material.',
        'Produce `commandPlan`: the exact ordered shell commands for the ADDITIVE migration (create encrypted dataset, copy file(s) with `cp -a`/checksum, set keylocation=prompt for manual-unlock parity, repoint references) — and a separate `rollbackPlan`. Do NOT include the destroy-old step here (that is a later gated phase). NEVER print secret values; reference secrets.yml by placeholder.',
        'Summarize residual risk and the recommended method in `summary`.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['srcSizeHuman', 'srcEncryption', 'srcFiles', 'dstPoolHealthy', 'dstPoolRedundant', 'dstFreeHuman', 'dstExists', 'method', 'backupWriter', 'referencesToRepoint', 'independentCopiesConfirmed', 'commandPlan', 'rollbackPlan', 'risks', 'summary'],
      properties: {
        srcSizeHuman: { type: 'string' },
        srcEncryption: { type: 'string' },
        srcFiles: { type: 'array', items: { type: 'object' } },
        dstPoolHealthy: { type: 'boolean' },
        dstPoolRedundant: { type: 'boolean' },
        dstFreeHuman: { type: 'string' },
        dstExists: { type: 'boolean' },
        method: { type: 'string' },
        backupWriter: { type: 'string' },
        referencesToRepoint: { type: 'array', items: { type: 'string' } },
        independentCopiesConfirmed: { type: 'boolean' },
        commandPlan: { type: 'array', items: { type: 'string' } },
        rollbackPlan: { type: 'array', items: { type: 'string' } },
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
// Phase 3 — execute migration (ADDITIVE ONLY: create new + copy + repoint; no destroy).
// ---------------------------------------------------------------------------
const migrateTask = defineTask('execute-migration', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Execute ADDITIVE migration to redundant apps pool (no destroy)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'TrueNAS SCALE / ZFS storage engineer executing an owner-approved master-key-backup relocation',
      task:
        'Execute ONLY the approved ADDITIVE command plan to create `' + args.dstDataset + '` (encrypted, ' +
        'SAME passphrase from secrets.yml — NO rotation) on the redundant apps pool and copy the age-key ' +
        'backup file(s) into ' + args.dstPath + '. DO NOT destroy or modify the source `' + args.srcDataset + '`. ' +
        'SSH: `' + args.truenasSsh + '`.',
      context: { ...args },
      instructions: [
        'Run the approved commandPlan EXACTLY (provided in context.commandPlan). Use sudo as needed. NEVER echo secret values; read the passphrase from secrets.yml (' + args.secretsRef + ') without printing it.',
        'Create the encrypted destination dataset with the SAME passphrase as the source. Then copy the age-key file(s) (cp -a) from ' + args.srcPath + ' to ' + args.dstPath + '.',
        'Set the destination keylocation to prompt (manual unlock after reboot), matching the source behaviour.',
        'IMMEDIATE in-place verification: compare per-file sha256 (source vs destination) — they MUST match. Confirm the destination dataset is encrypted (aes-256-gcm), mounted, and that unlocking with the secrets.yml passphrase succeeds.',
        'Repoint the references listed in context.referencesToRepoint that live on TrueNAS (cron/script/app config) from the pool1 path to ' + args.dstPath + '. Repo/doc references are handled later in the docs phase — do NOT edit the git repo here.',
        'Leave the OLD dataset fully intact. Return the per-file checksum comparison, encryption confirmation, unlock-test result, and what was repointed.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['created', 'copied', 'checksumsMatch', 'encryptedConfirmed', 'unlockTested', 'repointed', 'oldUntouched', 'summary'],
      properties: {
        created: { type: 'boolean' },
        copied: { type: 'boolean' },
        checksumsMatch: { type: 'boolean' },
        encryptedConfirmed: { type: 'boolean' },
        unlockTested: { type: 'boolean' },
        repointed: { type: 'array', items: { type: 'string' } },
        oldUntouched: { type: 'boolean' },
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
// Phase 4 — verify redundancy (INDEPENDENT quality gate; loop/refine on failure).
// ---------------------------------------------------------------------------
const verifyTask = defineTask('verify-redundancy', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Independently verify the key backup is redundant + recoverable',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Storage/security verifier proving issue #149 is satisfied (master key on a redundant pool, recoverable)',
      task:
        'INDEPENDENTLY verify (fresh reads, do not trust the migration report) that the age-key backup now lives ' +
        'on the redundant `' + args.dstPool + '` pool and is fully recoverable. SSH: `' + args.truenasSsh + '`.',
      context: { ...args },
      instructions: [
        'Confirm `' + args.dstDataset + '` exists, is on `' + args.dstPool + '`, and that `' + args.dstPool + '` is RAIDZ1 + ONLINE (redundant). `zpool status ' + args.dstPool + '`.',
        'Re-compute sha256 of the destination file(s) and compare to the source — MUST match.',
        'Confirm encryption props on the destination (encryption=aes-256-gcm, keyformat=passphrase, keylocation=prompt).',
        'End-to-end recoverability test: confirm the destination dataset can be unlocked with the secrets.yml passphrase and the age key file can be decrypted/used (e.g. validate it is a well-formed age identity, WITHOUT printing the private key). If a non-destructive SOPS decrypt smoke-test is possible, do it.',
        'Confirm the OLD source dataset is still intact (not yet retired) so rollback remains possible.',
        'Set verified=true ONLY if redundancy + checksum + encryption + recoverability ALL pass. Otherwise verified=false with precise failingChecks.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['verified', 'onRedundantPool', 'checksumMatch', 'encryptionOk', 'recoverable', 'oldStillIntact', 'failingChecks', 'summary'],
      properties: {
        verified: { type: 'boolean' },
        onRedundantPool: { type: 'boolean' },
        checksumMatch: { type: 'boolean' },
        encryptionOk: { type: 'boolean' },
        recoverable: { type: 'boolean' },
        oldStillIntact: { type: 'boolean' },
        failingChecks: { type: 'array', items: { type: 'string' } },
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
// Phase 6 — retire old (DESTRUCTIVE: destroy old snapshot + dataset on pool1).
// ---------------------------------------------------------------------------
const retireTask = defineTask('retire-old', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Retire (destroy) the old pool1 dataset after verified redundancy',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'TrueNAS SCALE / ZFS storage engineer retiring a now-redundant copy of the master-key backup',
      task:
        'The redundant copy on `' + args.dstDataset + '` has been INDEPENDENTLY verified. Now destroy the old, ' +
        'non-redundant source `' + args.srcDataset + '` (and any migration snapshot) so the master-key backup lives ' +
        'ONLY on the redundant pool. SSH: `' + args.truenasSsh + '`.',
      context: { ...args },
      instructions: [
        'Final pre-destroy guard: re-confirm the destination file checksum still matches and the destination is on the redundant pool. Abort (do not destroy) if not.',
        'Destroy any migration snapshot if one was created, then destroy the source dataset `' + args.srcDataset + '` (TrueNAS-native: prefer `midclt call pool.dataset.delete` if the dataset was created that way; else `zfs destroy`). Use sudo as needed. NEVER print secret values.',
        'Confirm the source dataset/path is gone and the destination copy is intact.',
        'Return what was destroyed and the final state.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['oldDestroyed', 'destinationIntact', 'summary'],
      properties: {
        oldDestroyed: { type: 'boolean' },
        destinationIntact: { type: 'boolean' },
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
// Phase 7 — document + PR (ISOLATED git worktree; NO merge). Refine loop on gate rejection.
// ---------------------------------------------------------------------------
const docsTask = defineTask('document-and-pr', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Update docs + open PR in an isolated worktree (no merge)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC docs author + git/gh operator working safely alongside a concurrent babysitter',
      task:
        'Document the #149 outcome (master-key backup moved off non-redundant pool1 onto redundant apps RAIDZ1) ' +
        'and open a PR — WITHOUT merging. CRITICAL: do ALL git work in an ISOLATED worktree; never touch the shared tree.',
      context: { ...args },
      instructions: [
        'Create an isolated worktree from origin/main: `git -C ' + args.repoRoot + ' fetch origin && git -C ' + args.repoRoot + ' worktree add ' + args.worktreeDir + ' -b ' + args.branch + ' origin/main`. Do ALL edits/commits there. NEVER run git stash / git checkout / branch-switch in ' + args.repoRoot + ' (a concurrent babysitter shares it; prior incident destroyed untracked files that way).',
        'Update the TrueNAS docs to record the new home of the SOPS master age-key backup: it now lives on `' + args.dstDataset + '` (apps pool, 3×SSD RAIDZ1 redundant), unlocked manually with the secrets.yml passphrase after reboot; it NO LONGER lives on the non-redundant pool1 stripe. Update 0-truenas/README.md and any doc that referenced pool1/encrypted-backups (use the referencesToRepoint list). Cross-link #124 and #57.',
        'Do NOT commit any secret. Respect the SOPS pre-commit hook. If secrets.yml is referenced, use placeholders only.',
        'Commit on branch ' + args.branch + ' (do NOT push yet is wrong — push the branch so a PR can open, but do NOT merge). Push the branch and open a PR (base main) per Epaflix merge policy. The PR body MUST include a Test Plan with checkbox items for: destination dataset on apps RAIDZ1; checksum match; unlock+decrypt works; old pool1 dataset destroyed; no remaining doc/cron reference to pool1/encrypted-backups.',
        'Return branch, commitSha, changedFiles, a docs diff (<=4000 chars), and prUrl. Do NOT merge.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'commitSha', 'changedFiles', 'diff', 'prUrl', 'pushed'],
      properties: {
        branch: { type: 'string' },
        commitSha: { type: 'string' },
        changedFiles: { type: 'array', items: { type: 'string' } },
        diff: { type: 'string' },
        prUrl: { type: 'string' },
        pushed: { type: 'boolean' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// Phase 9 — publish: rebase + merge per policy, close #149, tick test plan, follow-ups.
// ---------------------------------------------------------------------------
const publishTask = defineTask('publish-merge-close', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Rebase + merge PR per Epaflix policy, close #149, open follow-ups',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Release operator applying the Epaflix merge policy + issue hygiene',
      task: 'Merge the #149 docs PR per the Epaflix merge policy, then close #149 and record follow-ups.',
      context: { ...args },
      instructions: [
        'Work in the isolated worktree ' + args.worktreeDir + ' (NOT the shared tree). Rebase branch ' + args.branch + ' onto origin/main and `git push --force-with-lease` (strict up-to-date + required `validate` check block stale branches — see feedback_epaflix_merge_policy).',
        'Wait for the required `validate` check to pass, then merge with `gh pr merge ' + args.prUrl + ' --merge` (merge commit — never squash/rebase-merge). If `validate` flakes (unpinned kustomize rate-limit, project_ci_kustomize_flake), `gh run rerun --failed` and re-wait.',
        'Confirm the PR is MERGED. Then close issue #' + args.issue + ' with a short comment summarizing: master-key backup moved to apps RAIDZ1 (redundant), pool1/encrypted-backups retired, key verified recoverable.',
        'Edit the PR body to TICK the Test Plan checkboxes that are satisfied (record outcomes inline; NEVER add a new PR comment for test-plan results).',
        'Open follow-up gh issues on ' + args.repo + ' for any deferred item, e.g.: (a) pool1 itself (the 14T MEDIA stripe) remains non-redundant — separate from the now-resolved key-backup risk — if the owner wants media redundancy tracked; (b) the new manual-unlock-after-reboot now applies to apps/encrypted-backups (update reboot runbook). Cross-link #124 and #57. Use the enhancement-issue shape.',
        'Clean up the worktree when done: `git -C ' + args.repoRoot + ' worktree remove ' + args.worktreeDir + '`.',
        'Return merged, prUrl, mergeSha, issueState, and followUpIssues.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['merged', 'prUrl', 'mergeSha', 'issueState', 'followUpIssues'],
      properties: {
        merged: { type: 'boolean' },
        prUrl: { type: 'string' },
        mergeSha: { type: 'string' },
        issueState: { type: 'string' },
        followUpIssues: { type: 'array', items: { type: 'string' } },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ═══════════════════════════════════════════════════════════════════════════
// MAIN PROCESS
// ═══════════════════════════════════════════════════════════════════════════
export async function process(inputs, ctx) {
  const cfg = {
    repoRoot: '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
    truenasSsh: 'ssh truenas_admin@192.168.10.200',
    srcDataset: 'pool1/encrypted-backups',
    srcPath: '/mnt/pool1/encrypted-backups',
    dstPool: 'apps',
    dstDataset: 'apps/encrypted-backups',
    dstPath: '/mnt/apps/encrypted-backups',
    snapName: 'migrate-149',
    secretsRef: '.github/instructions/secrets.yml',
    issue: '149',
    repo: 'SpyrosPsarras/epaflix',
    branch: 'issue-149-pool1-redundancy-migrate',
    worktreeDir: '/home/spy/Documents/Epaflix/k3s-swarm-proxmox-wt-149',
    relatedIssues: ['124', '57'],
    ...inputs,
  };

  ctx.log('info', '#149 master-key-backup redundancy (Option C: move to apps RAIDZ1) — preflight → GATE1(secrets+destructive) → additive migrate → verify → GATE2(destructive: retire old) → docs+PR → GATE3(deploy/git) → merge+close');

  // PHASE 1 — preflight (read-only).
  const pre = await ctx.task(preflightTask, {
    repoRoot: cfg.repoRoot, truenasSsh: cfg.truenasSsh, srcDataset: cfg.srcDataset, srcPath: cfg.srcPath,
    dstPool: cfg.dstPool, dstDataset: cfg.dstDataset, dstPath: cfg.dstPath, snapName: cfg.snapName,
    secretsRef: cfg.secretsRef, issue: cfg.issue,
  });
  ctx.log('info', `Preflight: srcSize=${pre.srcSizeHuman}; dstRedundant=${pre.dstPoolRedundant}/healthy=${pre.dstPoolHealthy}; free=${pre.dstFreeHuman}; method=${pre.method}; indepCopies=${pre.independentCopiesConfirmed}`);

  // Guard: destination must be redundant + healthy + free, and independent key copies must exist.
  if (!pre.dstPoolRedundant || !pre.dstPoolHealthy) {
    const proceed = await ctx.breakpoint({
      question:
        'Issue #' + cfg.issue + ': preflight could NOT confirm the destination `' + cfg.dstPool + '` pool is redundant+healthy ' +
        '(redundant=' + pre.dstPoolRedundant + ', healthy=' + pre.dstPoolHealthy + '). Moving the master-key backup onto a ' +
        'non-redundant/unhealthy pool defeats #149. Summary: ' + pre.summary + '\n\nHow to proceed?',
      options: ['Abort (do not touch anything)', 'Proceed anyway (I confirmed it is fine)'],
      expert: 'owner',
      tags: ['destructive', 'approval-gate'],
    });
    const pr = (proceed.response || '').toLowerCase();
    if (!proceed.approved || pr.includes('abort')) {
      return { success: false, decision: 'aborted-dst-not-redundant', migrated: false, oldRetired: false, merged: false, pre };
    }
  }

  // GATE 1 (secrets + destructive) — approve the EXACT additive migration plan before ANY mutation.
  const planText = (pre.commandPlan || []).map((c, i) => '  ' + (i + 1) + '. ' + c).join('\n') || '(no plan emitted)';
  const refs = (pre.referencesToRepoint || []).join(', ') || '(none)';
  let approvedPlan = false;
  let fb1 = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    const gate1 = await ctx.breakpoint({
      question:
        'Issue #' + cfg.issue + ' — Option C: move the SOPS master age-key backup off the NON-REDUNDANT pool1 stripe onto the ' +
        'REDUNDANT apps RAIDZ1 pool.\n\n' +
        'PREFLIGHT: src `' + cfg.srcDataset + '` size=' + pre.srcSizeHuman + ' enc=' + pre.srcEncryption + '; ' +
        'dst pool redundant=' + pre.dstPoolRedundant + ' healthy=' + pre.dstPoolHealthy + ' free=' + pre.dstFreeHuman + '; ' +
        'dst exists=' + pre.dstExists + '; independent key copies confirmed=' + pre.independentCopiesConfirmed + '\n' +
        'Backup writer: ' + pre.backupWriter + '\nReferences to repoint: ' + refs + '\n' +
        (pre.risks && pre.risks.length ? 'Risks: ' + JSON.stringify(pre.risks) + '\n' : '') +
        '\nMETHOD: ' + pre.method + ' (reuse EXISTING passphrase from secrets.yml — NO rotation)\n\n' +
        'THIS GATE AUTHORIZES the ADDITIVE migration only (create new encrypted dataset + copy ~236K file + repoint TrueNAS refs). ' +
        'The OLD dataset is NOT destroyed here — that is a separate gate after independent verification.\n\n' +
        '--- EXACT COMMAND PLAN ---\n' + planText + '\n\nProceed with the additive migration?',
      options: ['Approve (run additive migration)', 'Request changes', 'Abort'],
      expert: 'owner',
      tags: ['secrets', 'destructive', 'approval-gate'],
      previousFeedback: fb1 || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    const r = (gate1.response || '').toLowerCase();
    if (gate1.approved && r.includes('approve')) { approvedPlan = true; break; }
    if (!gate1.approved || r.includes('abort')) {
      return { success: false, decision: 'aborted-plan', migrated: false, oldRetired: false, merged: false, reason: 'not-approved', feedback: gate1.response || gate1.feedback || '', pre };
    }
    fb1 = gate1.response || gate1.feedback || 'Changes requested';
    // Re-run preflight to refine the plan against the feedback.
    const re = await ctx.task(preflightTask, {
      repoRoot: cfg.repoRoot, truenasSsh: cfg.truenasSsh, srcDataset: cfg.srcDataset, srcPath: cfg.srcPath,
      dstPool: cfg.dstPool, dstDataset: cfg.dstDataset, dstPath: cfg.dstPath, snapName: cfg.snapName,
      secretsRef: cfg.secretsRef, issue: cfg.issue, feedback: fb1, attempt: attempt + 1,
    });
    Object.assign(pre, re);
  }
  if (!approvedPlan) {
    return { success: false, decision: 'aborted-plan', migrated: false, oldRetired: false, merged: false, reason: 'not-approved-after-retries', pre };
  }

  // PHASE 3 — additive migration.
  const mig = await ctx.task(migrateTask, {
    truenasSsh: cfg.truenasSsh, srcDataset: cfg.srcDataset, srcPath: cfg.srcPath,
    dstDataset: cfg.dstDataset, dstPath: cfg.dstPath, secretsRef: cfg.secretsRef,
    commandPlan: pre.commandPlan, referencesToRepoint: pre.referencesToRepoint,
  });
  ctx.log('info', `Migration: created=${mig.created} copied=${mig.copied} checksumsMatch=${mig.checksumsMatch} unlockTested=${mig.unlockTested} oldUntouched=${mig.oldUntouched}`);
  if (!mig.created || !mig.copied || !mig.checksumsMatch || !mig.encryptedConfirmed) {
    return { success: false, decision: 'migration-failed', migrated: false, oldRetired: false, merged: false, mig };
  }

  // PHASE 4 — independent verification with quality loop.
  let ver = await ctx.task(verifyTask, {
    truenasSsh: cfg.truenasSsh, srcDataset: cfg.srcDataset, srcPath: cfg.srcPath,
    dstPool: cfg.dstPool, dstDataset: cfg.dstDataset, dstPath: cfg.dstPath, secretsRef: cfg.secretsRef,
  });
  ctx.log('info', `Verify: verified=${ver.verified} redundant=${ver.onRedundantPool} checksum=${ver.checksumMatch} enc=${ver.encryptionOk} recoverable=${ver.recoverable}`);
  if (!ver.verified) {
    const recover = await ctx.breakpoint({
      question:
        'Issue #' + cfg.issue + ': redundancy verification FAILED. failingChecks=' + JSON.stringify(ver.failingChecks) + '\n' +
        'The OLD pool1 dataset is still intact (' + ver.oldStillIntact + ') so nothing is lost. Summary: ' + ver.summary + '\n\nHow to proceed?',
      options: ['Re-verify', 'Stop here (do NOT retire old)'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const rr = (recover.response || '').toLowerCase();
    if (recover.approved && rr.includes('re-verify')) {
      ver = await ctx.task(verifyTask, {
        truenasSsh: cfg.truenasSsh, srcDataset: cfg.srcDataset, srcPath: cfg.srcPath,
        dstPool: cfg.dstPool, dstDataset: cfg.dstDataset, dstPath: cfg.dstPath, secretsRef: cfg.secretsRef,
      });
    }
    if (!ver.verified) {
      return { success: false, decision: 'verify-failed', migrated: true, redundancyConfirmed: false, oldRetired: false, merged: false, ver };
    }
  }

  // GATE 2 (destructive) — approve retiring the old pool1 dataset (only after verified redundancy).
  let approvedRetire = false;
  for (let attempt = 0; attempt < 3; attempt++) {
    const gate2 = await ctx.breakpoint({
      question:
        'Issue #' + cfg.issue + ': the key backup is now VERIFIED redundant on `' + cfg.dstDataset + '` (apps RAIDZ1): ' +
        'redundant=' + ver.onRedundantPool + ', checksumMatch=' + ver.checksumMatch + ', encryptionOk=' + ver.encryptionOk + ', recoverable=' + ver.recoverable + '.\n\n' +
        'Approve RETIRING (destroying) the old non-redundant source `' + cfg.srcDataset + '` (and migration snapshot)? ' +
        'After this, the master-key backup lives ONLY on the redundant pool. Two independent key copies still exist elsewhere.',
      options: ['Approve (destroy old dataset)', 'Keep old for now (stop)'],
      expert: 'owner',
      tags: ['destructive', 'approval-gate'],
    });
    const r = (gate2.response || '').toLowerCase();
    if (gate2.approved && r.includes('approve')) { approvedRetire = true; break; }
    if (!gate2.approved || r.includes('keep') || r.includes('stop')) {
      // Not destroying the old copy is a safe partial success; still document, but mark oldRetired=false.
      ctx.log('warn', 'Owner chose to keep the old dataset — skipping retire; documentation will note both copies exist.');
      approvedRetire = false; break;
    }
  }

  let retire = { oldDestroyed: false, destinationIntact: true, summary: 'old dataset kept by owner choice' };
  if (approvedRetire) {
    retire = await ctx.task(retireTask, {
      truenasSsh: cfg.truenasSsh, srcDataset: cfg.srcDataset, srcPath: cfg.srcPath,
      dstDataset: cfg.dstDataset, dstPath: cfg.dstPath, snapName: cfg.snapName,
    });
    ctx.log('info', `Retire: oldDestroyed=${retire.oldDestroyed} destinationIntact=${retire.destinationIntact}`);
    if (!retire.destinationIntact) {
      return { success: false, decision: 'retire-unsafe', migrated: true, redundancyConfirmed: true, oldRetired: false, merged: false, retire };
    }
  }

  // PHASE 7 — docs + PR (isolated worktree, no merge). Refine loop on GATE 3 rejection.
  let docs = await ctx.task(docsTask, {
    repoRoot: cfg.repoRoot, worktreeDir: cfg.worktreeDir, branch: cfg.branch, issue: cfg.issue,
    repo: cfg.repo, dstDataset: cfg.dstDataset, dstPath: cfg.dstPath, secretsRef: cfg.secretsRef,
    referencesToRepoint: pre.referencesToRepoint, relatedIssues: cfg.relatedIssues,
    oldRetired: retire.oldDestroyed,
  });
  ctx.log('info', `Docs: branch=${docs.branch} commit=${docs.commitSha} pr=${docs.prUrl} files=${JSON.stringify(docs.changedFiles)}`);

  // GATE 3 (deploy / destructive-git) — approve merge + close.
  let approvedMerge = false;
  let fb3 = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (fb3) {
      docs = await ctx.task(docsTask, {
        repoRoot: cfg.repoRoot, worktreeDir: cfg.worktreeDir, branch: cfg.branch, issue: cfg.issue,
        repo: cfg.repo, dstDataset: cfg.dstDataset, dstPath: cfg.dstPath, secretsRef: cfg.secretsRef,
        referencesToRepoint: pre.referencesToRepoint, relatedIssues: cfg.relatedIssues,
        oldRetired: retire.oldDestroyed, feedback: fb3, attempt: attempt + 1,
      });
    }
    const gate3 = await ctx.breakpoint({
      question:
        'Issue #' + cfg.issue + ' — docs PR is open (no merge yet): ' + docs.prUrl + '\n' +
        'Files: ' + JSON.stringify(docs.changedFiles) + '\n' +
        'Old dataset retired: ' + retire.oldDestroyed + '\n\n' +
        '--- docs diff ---\n' + (docs.diff || '(no diff)').slice(0, 4000) + '\n\n' +
        'Approve rebase + MERGE per Epaflix policy + close #' + cfg.issue + '?',
      options: ['Approve (merge + close)', 'Request changes', 'Abort (leave PR open)'],
      expert: 'owner',
      tags: ['deploy', 'destructive-git', 'approval-gate'],
      previousFeedback: fb3 || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    const r = (gate3.response || '').toLowerCase();
    if (gate3.approved && r.includes('approve')) { approvedMerge = true; break; }
    if (!gate3.approved || r.includes('abort')) {
      return { success: false, decision: 'pr-open-not-merged', migrated: true, redundancyConfirmed: true, oldRetired: retire.oldDestroyed, merged: false, prUrl: docs.prUrl, reason: 'merge-not-approved' };
    }
    fb3 = gate3.response || gate3.feedback || 'Changes requested';
  }
  if (!approvedMerge) {
    return { success: false, decision: 'pr-open-not-merged', migrated: true, redundancyConfirmed: true, oldRetired: retire.oldDestroyed, merged: false, prUrl: docs.prUrl, reason: 'merge-not-approved-after-retries' };
  }

  // PHASE 9 — merge + close + follow-ups.
  const pub = await ctx.task(publishTask, {
    repoRoot: cfg.repoRoot, worktreeDir: cfg.worktreeDir, branch: cfg.branch, prUrl: docs.prUrl,
    issue: cfg.issue, repo: cfg.repo, relatedIssues: cfg.relatedIssues,
  });
  ctx.log('info', `Publish: merged=${pub.merged} pr=${pub.prUrl} sha=${pub.mergeSha} issue=${pub.issueState} followUps=${JSON.stringify(pub.followUpIssues)}`);

  return {
    success: !!pub.merged,
    decision: 'completed',
    migrated: true,
    redundancyConfirmed: true,
    oldRetired: retire.oldDestroyed,
    merged: !!pub.merged,
    prUrl: pub.prUrl,
    issueState: pub.issueState,
    followUpIssues: pub.followUpIssues,
  };
}
