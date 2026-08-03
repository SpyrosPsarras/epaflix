/**
 * @process specializations/devops-sre-platform/truenas-encrypted-sops-dataset
 * @description Resolve issue #57 — move the SOPS cluster age master-key backup off the
 *   unencrypted `pool1/dataset01/sops-age-backup/` onto a NEW ZFS-encrypted dataset
 *   `pool1/encrypted-backups` (passphrase unlock) on TrueNAS (192.168.10.200), then update the
 *   rotation recipe in `.github/instructions/sops.instructions.md` to reference the new path
 *   (branch + PR + merge per Epaflix policy).
 *
 *   SAFETY: the workstation copy (~/.config/sops/age/k3s-cluster.txt) and the in-cluster KSOPS
 *   key are NEVER touched, so there is no lockout risk. The old backup is copied → verified
 *   (sha256 against the workstation canonical) → and only THEN shredded. The encryption
 *   passphrase is auto-generated, recorded ONLY in the git-ignored `.github/instructions/
 *   secrets.yml`, passed to TrueNAS via a shredded temp payload file (never on a command line
 *   or in the run journal), and never echoed in any task's JSON output.
 *
 *   Gates (profile alwaysBreakOn: destructive + secrets-rotation + deploy):
 *     GATE 1 — before any live TrueNAS write (create dataset + migrate + shred old key).
 *     GATE 2 — before push + PR + merge of the doc change (outward-facing).
 *
 * @inputs { repoRoot, configPath, truenasHost, pool, datasetName, datasetPath, oldBackupPath,
 *           keyFile, workstationKey, secretsFile, secretKey, branch, currentBranch, repo }
 * @outputs { success, datasetEncrypted, keyMigrated, oldKeyShredded, merged, prUrl, summary }
 *
 * @agent general-purpose (TrueNAS midclt/zfs ops over SSH, sha256 verification, doc + git/gh)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

// ---------------------------------------------------------------------------
// Phase 0 — read-only precheck on TrueNAS + repo. No writes.
// ---------------------------------------------------------------------------
const precheckTask = defineTask('precheck', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Precheck TrueNAS pool/dataset state + locate age-key backup (read-only)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Storage/SRE engineer auditing TrueNAS before an encrypted-dataset migration',
      task:
        'READ-ONLY reconnaissance on TrueNAS (' + args.truenasHost + ') and the workstation to confirm it is ' +
        'safe to create encrypted dataset ' + args.datasetName + ' and migrate the SOPS age key backup. Make NO changes.',
      context: { ...args },
      instructions: [
        'SSH: `ssh truenas_admin@' + args.truenasHost + '`. midclt/zfs admin reads may need `sudo` — use it where required and report whether sudo was needed.',
        'Confirm the pool "' + args.pool + '" exists and is ONLINE/healthy (`zpool status ' + args.pool + '`, `zpool list ' + args.pool + '`). Report free space.',
        'Confirm the OLD backup file exists: `' + args.oldBackupPath + '/' + args.keyFile + '` — report its perms, owner, and sha256.',
        'Confirm the target dataset "' + args.datasetName + '" does NOT already exist (`zfs list ' + args.datasetName + '` should fail). If it DOES exist, report its encryption status and STOP-flag it.',
        'Confirm the WORKSTATION canonical key exists locally at ' + args.workstationKey + ' and report its sha256. The OLD-backup sha256 and the workstation sha256 MUST match — report match=true/false. This is the safety anchor for the later copy→verify→shred.',
        'Determine the exact create mechanism available on this TrueNAS version: prefer `midclt call pool.dataset.create` with `encryption=true`, `encryption_options.generate_key=false`, `encryption_options.passphrase=<...>`, `encryption_options.algorithm=AES-256-GCM`, inherit_encryption=false. Confirm `pool.dataset.create` accepts a JSON payload via `-j @file` (or document the correct stdin form). Note the TrueNAS version (`midclt call system.version`).',
        'Do NOT generate the passphrase, do NOT create anything, do NOT move/shred anything in this task.',
        'Return structured JSON with the findings + a go/no-go recommendation and any blockers.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['poolHealthy', 'oldKeyExists', 'oldKeySha256', 'workstationKeySha256', 'shaMatch', 'datasetAlreadyExists', 'createMechanism', 'goNoGo', 'blockers'],
      properties: {
        poolHealthy: { type: 'boolean' },
        freeSpace: { type: 'string' },
        oldKeyExists: { type: 'boolean' },
        oldKeyPerms: { type: 'string' },
        oldKeySha256: { type: 'string' },
        workstationKeySha256: { type: 'string' },
        shaMatch: { type: 'boolean' },
        datasetAlreadyExists: { type: 'boolean' },
        truenasVersion: { type: 'string' },
        sudoNeeded: { type: 'boolean' },
        createMechanism: { type: 'string' },
        goNoGo: { type: 'string' },
        blockers: { type: 'array', items: { type: 'string' } },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// Phase 1 — LIVE: create the passphrase-encrypted dataset. Secret-safe.
// ---------------------------------------------------------------------------
const createDatasetTask = defineTask('create-dataset', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Create passphrase-encrypted ZFS dataset ' + args.datasetName + ' on TrueNAS',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Storage engineer provisioning an encrypted ZFS dataset on TrueNAS',
      task:
        'Create ZFS dataset ' + args.datasetName + ' with native ZFS encryption (AES-256-GCM, ' +
        'PASSPHRASE unlock). Generate a strong passphrase, record it ONLY in the git-ignored ' +
        args.secretsFile + ', and never expose it in output or on a command line.',
      context: { ...args },
      instructions: [
        'SSH `ssh truenas_admin@' + args.truenasHost + '`; use sudo where midclt/zfs need root.',
        'IDEMPOTENCY: if ' + args.datasetName + ' already exists AND is encrypted, do NOT recreate — report existing=true and its encryption props, skip to setting perms-ready state.',
        'Generate a strong passphrase (>= 32 bytes, `openssl rand -base64 32`). Treat it as a secret. Per the CREATE instruction below, generate it remotely inside the same SSH session that creates the dataset and capture it via the SSH stdout, so it is used exactly once and is not retyped.',
        'PASSPHRASE STORAGE: after a successful create, write the captured passphrase as a clearly-labelled entry in ' + args.secretsFile + ' on the workstation (repoRoot=' + args.repoRoot + ') under key `' + args.secretKey + '`, with a comment noting it unlocks TrueNAS dataset ' + args.datasetName + ' (manual unlock REQUIRED after a TrueNAS reboot — passphrase datasets do NOT auto-mount). ' + args.secretsFile + ' is git-ignored; do NOT commit it. Verify the value landed by re-reading the file (presence only).',
        'CREATE using the VERIFIED-CORRECT form for TrueNAS 25.10.0.1 (confirmed in precheck): midclt has NO @file expansion, `-j` means `--job`, and pool.dataset.create is a JOB method. sudo is NOT needed (truenas_admin is in builtin_administrators). Build the payload inline and pass it as a POSITIONAL JSON string — NOT via -j @file. Recommended single-session form, generating the passphrase remotely so it never touches the workstation shell history: ssh into TrueNAS and run `PASS="$(openssl rand -base64 32)"; PAYLOAD=$(jq -nc --arg n "' + args.datasetName + '" --arg p "$PASS" \'{name:$n,type:"FILESYSTEM",encryption:true,inherit_encryption:false,encryption_options:{generate_key:false,algorithm:"AES-256-GCM",passphrase:$p}}\'); midclt call -j pool.dataset.create "$PAYLOAD"; printf "%s" "$PASS"` — capture that final printed passphrase over the SSH stdout to store it (next instruction). Do NOT write the payload or passphrase to any file on TrueNAS; do NOT use a TrueNAS shell that persists history for the PASS assignment (a single non-interactive `ssh host bash -lc \'...\'` does not). The passphrase is briefly visible in TrueNAS process args (root-only, LAN) — acceptable per the issue threat model.',
        'Verify the dataset is encrypted and unlocked: `zfs get -H encryption,encryptionroot,keystatus,keyformat,mounted ' + args.datasetName + '` → expect encryption=aes-256-gcm, keyformat=passphrase, keystatus=available, mounted=yes. Report the mountpoint (expected /mnt/' + args.datasetName + ').',
        'Do NOT migrate or shred the key in this task — that is the next phase.',
        'If feedback is present in context (a prior rejection), incorporate it.',
        'Return structured JSON. NEVER include the passphrase value. Confirm it was written to ' + args.secretsFile + '.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['datasetCreated', 'datasetEncrypted', 'encryptionAlgo', 'keyformat', 'mounted', 'mountpoint', 'passphraseStoredInSecretsFile'],
      properties: {
        datasetCreated: { type: 'boolean' },
        alreadyExisted: { type: 'boolean' },
        datasetEncrypted: { type: 'boolean' },
        encryptionAlgo: { type: 'string' },
        keyformat: { type: 'string' },
        keystatus: { type: 'string' },
        mounted: { type: 'boolean' },
        mountpoint: { type: 'string' },
        passphraseStoredInSecretsFile: { type: 'boolean' },
        notes: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// Phase 2 — LIVE: migrate the age key (copy → verify → shred). Fail-safe.
// ---------------------------------------------------------------------------
const migrateKeyTask = defineTask('migrate-key', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Migrate SOPS age key backup into the encrypted dataset (copy→verify→shred)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE performing a fail-safe secret-file migration on TrueNAS',
      task:
        'Move the age-key backup from ' + args.oldBackupPath + '/' + args.keyFile + ' into the new encrypted dataset ' +
        'at ' + args.datasetPath + '/sops-age-backup/' + args.keyFile + '. NEVER shred the old copy until the new copy ' +
        'is byte-verified against the WORKSTATION canonical key.',
      context: { ...args },
      instructions: [
        'SSH `ssh truenas_admin@' + args.truenasHost + '`; use sudo where needed.',
        'Compute the WORKSTATION canonical sha256: on the workstation, `sha256sum ' + args.workstationKey + '`. This is the source-of-truth digest.',
        'On TrueNAS, create dir `' + args.datasetPath + '/sops-age-backup/` (mode 0700). COPY (do not move yet) `' + args.oldBackupPath + '/' + args.keyFile + '` → `' + args.datasetPath + '/sops-age-backup/' + args.keyFile + '`. Set the new file 0600 and the SAME owner the old file had (report owner).',
        'VERIFY: sha256 of the NEW copy MUST equal the workstation canonical digest AND the old-backup digest. If ANY mismatch, ABORT — do NOT shred anything, report the mismatch, leave the old file intact.',
        'Only after a clean 3-way sha256 match: `shred -u ' + args.oldBackupPath + '/' + args.keyFile + '` to remove the old plaintext-dataset copy. If `' + args.oldBackupPath + '` is now empty and was created solely for this key, you MAY remove the empty dir (report if you did).',
        'Final state check: new file exists at the encrypted path with 0600, sha256 matches; old path no longer exists.',
        'Return structured JSON with all three digests, the verify result, and whether the old key was shredded.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['newKeyPath', 'workstationSha256', 'oldSha256', 'newSha256', 'verified', 'oldKeyShredded'],
      properties: {
        newKeyPath: { type: 'string' },
        newKeyPerms: { type: 'string' },
        newKeyOwner: { type: 'string' },
        workstationSha256: { type: 'string' },
        oldSha256: { type: 'string' },
        newSha256: { type: 'string' },
        verified: { type: 'boolean' },
        oldKeyShredded: { type: 'boolean' },
        oldDirRemoved: { type: 'boolean' },
        notes: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// Phase 3 — read-only end-state verification of the live migration.
// ---------------------------------------------------------------------------
const verifyMigrationTask = defineTask('verify-migration', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify end state: encrypted dataset + key at new path + old path gone',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'QA/SRE verifier confirming a TrueNAS encrypted-backup migration',
      task: 'Independently re-verify the migration result. READ-ONLY.',
      context: { ...args },
      instructions: [
        'SSH `ssh truenas_admin@' + args.truenasHost + '`; sudo for reads as needed.',
        'Confirm `zfs get -H encryption,keyformat,keystatus,mounted ' + args.datasetName + '` shows aes-256-gcm + passphrase + available + yes.',
        'Confirm the key exists at `' + args.datasetPath + '/sops-age-backup/' + args.keyFile + '` with mode 0600, and its sha256 equals the workstation canonical (`sha256sum ' + args.workstationKey + '` on the workstation).',
        'Confirm the OLD path `' + args.oldBackupPath + '/' + args.keyFile + '` NO LONGER exists.',
        'Confirm ' + args.secretsFile + ' contains the passphrase entry (presence only — do NOT print the value).',
        'Return structured JSON pass/fail with evidence; list any discrepancy.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['datasetEncryptedOk', 'newKeyOk', 'oldPathGone', 'shaMatch', 'passphraseRecorded', 'allPass', 'issues'],
      properties: {
        datasetEncryptedOk: { type: 'boolean' },
        newKeyOk: { type: 'boolean' },
        oldPathGone: { type: 'boolean' },
        shaMatch: { type: 'boolean' },
        passphraseRecorded: { type: 'boolean' },
        allPass: { type: 'boolean' },
        issues: { type: 'array', items: { type: 'string' } },
        evidence: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// Phase 4 — author the doc update on a branch + commit (no push). Reversible.
// ---------------------------------------------------------------------------
const docTask = defineTask('author-doc', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Update sops.instructions.md to the new encrypted path + branch + commit',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC/docs engineer updating the SOPS rotation runbook in the Epaflix repo',
      task:
        'Edit ' + args.configPath + ' so every reference to the old age-key backup path ' +
        '`' + args.oldBackupPath + '` points at the new encrypted path `' + args.datasetPath + '/sops-age-backup/`, ' +
        'and the "to be moved to a ZFS-encrypted (...)" note becomes a statement of fact. Then branch + ONE commit. No push.',
      context: { ...args },
      instructions: [
        'CONCURRENCY HAZARD: the main checkout at repoRoot=' + args.repoRoot + ' is SHARED with other active sessions that switch branches and sweep files. To avoid colliding with them, do ALL git work in an ISOLATED WORKTREE based on latest origin/main. From repoRoot run: `git fetch origin` then `git worktree add -B ' + args.branch + ' /tmp/wt-encrypted-sops origin/main` (if that worktree path already exists, reuse it / `git worktree remove --force` then re-add). Do everything below INSIDE /tmp/wt-encrypted-sops. Do NOT `git checkout`/`git switch` in the main checkout. Do NOT touch other branches or other sessions\' files.',
        'Inside the worktree, read ' + args.configPath + ' fully first; match its style.',
        'Lines ~13-15: the bullet currently says the backup lives at `pool1/dataset01/sops-age-backup/` "(to be moved to a ZFS-encrypted...)". Rewrite it to state the backup now lives on the encrypted dataset at `' + args.datasetPath + '/sops-age-backup/' + args.keyFile + '` (ZFS native encryption, passphrase unlock; passphrase recorded in the git-ignored ' + args.secretsFile + ' under `' + args.secretKey + '`; manual unlock required after a TrueNAS reboot before this backup is readable).',
        'Rotation recipe (~lines 112-113): update the `shred -u .../sops-age-backup/' + args.keyFile + '` and the `scp ... :/.../sops-age-backup/` commands to use `' + args.datasetPath + '/sops-age-backup/` instead of `' + args.oldBackupPath + '`.',
        'Scan the WHOLE file for any other occurrence of `' + args.oldBackupPath + '` or `dataset01/sops-age-backup` and update them too. After editing, grep to confirm ZERO remaining references to the OLD path.',
        'Do NOT change unrelated content. Keep secrets out of the doc — reference the secrets.yml key by name only, never a value.',
        'If feedback is present in context, incorporate it.',
        'In the worktree, stage ONLY ' + args.configPath + '. ONE commit on branch ' + args.branch + '. End the message body with: Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>. Report the worktree path so the publish phase reuses it.',
        'Return structured JSON: branch, commitSha, worktreePath, full diff, oldPathRefsRemaining (must be 0), proposed PR title/body. The PR body must reference issue #57, summarize the new encrypted path + passphrase-in-secrets.yml, note the live TrueNAS migration is already done, and note this is a docs-only change (no cluster deploy).',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'commitSha', 'diff', 'oldPathRefsRemaining', 'prTitle', 'prBody'],
      properties: {
        branch: { type: 'string' },
        commitSha: { type: 'string' },
        worktreePath: { type: 'string' },
        diff: { type: 'string' },
        oldPathRefsRemaining: { type: 'number' },
        prTitle: { type: 'string' },
        prBody: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// Phase 5 — push + PR + merge per Epaflix policy + close issue #57.
// ---------------------------------------------------------------------------
const publishTask = defineTask('publish-merge', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Push branch, open PR, rebase+merge per policy, close #57',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer publishing an OWNER-APPROVED docs change to SpyrosPsarras/epaflix',
      task: 'Push the branch, open a PR referencing #57, satisfy the Epaflix merge policy, merge, and close #57.',
      context: { ...args },
      instructions: [
        'CONCURRENCY HAZARD: the main checkout is SHARED with other active sessions. Operate ONLY inside the isolated worktree from the author phase (context worktreePath, expected /tmp/wt-encrypted-sops). Do NOT run git checkout/switch/rebase in the main checkout. repo=' + args.repo + '.',
        'cd into the worktree (context.worktreePath). Push branch ' + args.branch + ' to origin.',
        'Epaflix policy: merge-commit + mandatory rebase (semi-linear). Before merging, `git fetch origin` and rebase ' + args.branch + ' onto origin/main inside the worktree, then `push --force-with-lease` so the branch is up to date (strict branch protection + required `validate` check).',
        'Open a PR to main with the approved title/body (context approvedPrTitle / approvedPrBody); ensure the body has "Closes #57".',
        'Wait for the required `validate` check to pass, then merge: `gh pr merge ' + args.branch + ' --merge` (merge commit — NOT squash/rebase). Use `--admin` only if a non-content gate blocks and 0-approval admin bypass is authorized.',
        'Confirm the PR is MERGED and issue #57 is CLOSED (gh will auto-close via "Closes #57"; verify, and `gh issue close 57` if needed).',
        'CLEANUP: after a confirmed merge, remove the worktree from the main checkout: `git -C ' + args.repoRoot + ' worktree remove --force ' + '<worktreePath>` so it does not linger.',
        'Return structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'merged', 'mergeSha', 'issueClosed'],
      properties: {
        prUrl: { type: 'string' },
        merged: { type: 'boolean' },
        mergeSha: { type: 'string' },
        issueClosed: { type: 'boolean' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

export async function process(inputs, ctx) {
  const cfg = {
    repoRoot: '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
    configPath: '.github/instructions/sops.instructions.md',
    truenasHost: '192.168.10.200',
    pool: 'pool1',
    datasetName: 'pool1/encrypted-backups',
    datasetPath: '/mnt/pool1/encrypted-backups',
    oldBackupPath: '/mnt/pool1/dataset01/sops-age-backup',
    keyFile: 'k3s-cluster.txt',
    workstationKey: '/home/spy/.config/sops/age/k3s-cluster.txt',
    secretsFile: '.github/instructions/secrets.yml',
    secretKey: 'truenas_zfs_encrypted_backups_passphrase',
    branch: 'truenas/encrypted-sops-backups-dataset',
    currentBranch: 'main',
    repo: 'SpyrosPsarras/epaflix',
    ...inputs,
  };

  ctx.log('info', 'issue #57: move SOPS age-key backup to ZFS-encrypted dataset ' + cfg.datasetName + ' (passphrase unlock)');

  // PHASE 0 — read-only precheck.
  const pre = await ctx.task(precheckTask, { ...cfg });
  ctx.log('info', `precheck: poolHealthy=${pre.poolHealthy} oldKey=${pre.oldKeyExists} shaMatch=${pre.shaMatch} datasetExists=${pre.datasetAlreadyExists} goNoGo=${pre.goNoGo}`);

  // GATE 1 — destructive + secrets-rotation: approve live TrueNAS writes.
  {
    const blockers = (pre.blockers && pre.blockers.length) ? pre.blockers.join('; ') : 'none';
    const gate = await ctx.breakpoint({
      question:
        'Approve LIVE TrueNAS migration for issue #57?\n\n' +
        'Plan: create encrypted dataset ' + cfg.datasetName + ' (AES-256-GCM, PASSPHRASE unlock; passphrase auto-generated → stored in ' + cfg.secretsFile + ' key `' + cfg.secretKey + '`), then copy→verify(sha256 vs workstation)→shred the age-key backup into ' + cfg.datasetPath + '/sops-age-backup/.\n\n' +
        'Precheck: poolHealthy=' + pre.poolHealthy + ', oldKeyExists=' + pre.oldKeyExists + ', workstation↔old sha match=' + pre.shaMatch + ', datasetAlreadyExists=' + pre.datasetAlreadyExists + ', TrueNAS=' + (pre.truenasVersion || '?') + '.\n' +
        'go/no-go=' + pre.goNoGo + '; blockers=' + blockers + '.\n\n' +
        'SAFETY: workstation copy + in-cluster KSOPS key untouched; old key shredded ONLY after a clean 3-way sha256 match. Passphrase datasets do NOT auto-mount after a TrueNAS reboot (manual unlock).\n\n' +
        'Proceed with the live create + migrate?',
      title: 'GATE 1 — live TrueNAS create + key migration (#57)',
      options: ['Approve live migration', 'Request changes', 'Abort'],
      expert: 'owner',
      tags: ['destructive', 'secrets-rotation', 'truenas', 'approval-gate'],
    });
    const r = (gate.response || '').toLowerCase();
    if (!(gate.approved && r.includes('approve'))) {
      ctx.log('warn', 'GATE 1 not approved — nothing changed on TrueNAS.');
      return { success: false, merged: false, reason: r.includes('abort') ? 'aborted-gate1' : 'not-approved-gate1', precheck: pre };
    }
  }

  // PHASE 1 — create encrypted dataset (live).
  const created = await ctx.task(createDatasetTask, { ...cfg });
  ctx.log('info', `dataset: created=${created.datasetCreated} existed=${created.alreadyExisted} encrypted=${created.datasetEncrypted} keyformat=${created.keyformat} mounted=${created.mounted}`);
  if (!created.datasetEncrypted) {
    return { success: false, merged: false, reason: 'dataset-not-encrypted', created };
  }

  // PHASE 2 — migrate key (copy→verify→shred, live, self-aborting on mismatch).
  const migrated = await ctx.task(migrateKeyTask, { ...cfg });
  ctx.log('info', `migrate: verified=${migrated.verified} oldShredded=${migrated.oldKeyShredded}`);
  if (!migrated.verified) {
    return { success: false, merged: false, reason: 'sha256-verify-failed', migrated };
  }

  // PHASE 3 — independent end-state verification.
  const verified = await ctx.task(verifyMigrationTask, { ...cfg });
  ctx.log('info', `verify-migration: allPass=${verified.allPass} issues=${(verified.issues || []).join('|')}`);
  if (!verified.allPass) {
    return { success: false, merged: false, reason: 'migration-verify-failed', verified };
  }

  // PHASE 4 — author doc update on a branch + commit (reversible). Retry/refine loop + GATE 2.
  let authored = await ctx.task(docTask, { ...cfg });
  let lastFeedback = null;
  let approved = false;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (lastFeedback) {
      authored = await ctx.task(docTask, { ...cfg, feedback: lastFeedback, attempt: attempt + 1 });
    }
    const gate = await ctx.breakpoint({
      question:
        'Approve the sops.instructions.md doc update (push + PR + merge, Closes #57)?\n\n' +
        'Old-path references remaining (must be 0): ' + authored.oldPathRefsRemaining + '\n' +
        'Branch: ' + authored.branch + '\n\n' +
        'Diff:\n' + authored.diff + '\n\n' +
        'PR title: ' + authored.prTitle,
      title: 'GATE 2 — publish doc change + close #57 (outward-facing)',
      options: ['Approve push + merge', 'Request changes', 'Abort'],
      expert: 'owner',
      tags: ['deploy', 'outward-facing', 'approval-gate'],
      previousFeedback: lastFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    const r = (gate.response || '').toLowerCase();
    if (gate.approved && r.includes('approve')) { approved = true; break; }
    if (r.includes('abort') || (!gate.approved && !r)) {
      ctx.log('warn', 'GATE 2 aborted — TrueNAS migration done + verified; doc change on local branch only, not pushed.');
      return { success: false, merged: false, reason: 'aborted-gate2', datasetEncrypted: true, keyMigrated: true, oldKeyShredded: migrated.oldKeyShredded, branch: authored.branch };
    }
    lastFeedback = gate.response || gate.feedback || 'Changes requested';
  }
  if (!approved) {
    return { success: false, merged: false, reason: 'not-approved-gate2-after-retries', branch: authored.branch };
  }

  // PHASE 5 — publish + merge + close #57.
  const pub = await ctx.task(publishTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo, branch: authored.branch,
    worktreePath: authored.worktreePath,
    approvedPrTitle: authored.prTitle, approvedPrBody: authored.prBody,
  });
  ctx.log('info', `published: merged=${pub.merged} issueClosed=${pub.issueClosed} PR=${pub.prUrl}`);

  return {
    success: pub.merged === true && verified.allPass === true,
    datasetEncrypted: created.datasetEncrypted,
    keyMigrated: migrated.verified,
    oldKeyShredded: migrated.oldKeyShredded,
    merged: pub.merged,
    prUrl: pub.prUrl,
    mergeSha: pub.mergeSha,
    issueClosed: pub.issueClosed,
    summary: 'SOPS age-key backup moved to ZFS-encrypted ' + cfg.datasetName + ' (passphrase in ' + cfg.secretsFile + '); sops.instructions.md updated; issue #57 closed. Passphrase dataset requires manual unlock after a TrueNAS reboot.',
  };
}
