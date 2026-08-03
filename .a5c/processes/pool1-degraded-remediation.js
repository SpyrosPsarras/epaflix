/**
 * @process specializations/devops-sre-platform/pool1-degraded-remediation
 * @description Investigate + (safely) remediate TrueNAS `pool1` reported DEGRADED in issue #124.
 *   pool1 is a NON-REDUNDANT 2-disk HDD stripe (no mirror/raidz) on the TrueNAS workstation
 *   (192.168.10.200); a vdev logged 332 WRITE errors (ZFS-8000-9P) during the #57/PR#122
 *   migration. It now also hosts the canonical SOPS cluster age-key backup on the encrypted
 *   dataset `pool1/encrypted-backups`, so the degraded+non-redundant state directly undermines
 *   the #57 "secure long-term home" goal (though workstation copy + in-cluster KSOPS key are
 *   independent, so no lockout risk).
 *
 *   Goal = answer the owner's question ("can you fix it?") with evidence: diagnose the failing
 *   member (SMART, error class, dmesg/zpool events, scrub history), decide what is *software-
 *   fixable now* vs what needs *hardware* (disk replacement / add a mirror), apply only the safe
 *   software remediation (`zpool clear pool1` + on-demand scrub) under a mandatory gate, verify,
 *   then report findings + outcome on issue #124 and open follow-ups per repo policy.
 *
 *   Live-change risk: `zpool clear` and a scrub are writes/IO against the pool that holds the
 *   master-key backup — gated by a mandatory breakpoint. Issue/git mutation gated separately.
 *   Disk replacement and adding redundancy are HARDWARE actions, explicitly OUT OF SCOPE for
 *   autonomous execution (require physical disks) — captured as recommendations / follow-ups.
 *
 * @inputs { repoRoot, truenasHost, pool, issueNumber, issueUrl, repo }
 * @outputs { success, fixable, rootCause, remediationApplied, poolStateAfter, issueUrl, followUps, summary }
 *
 * @agent general-purpose (ssh/zpool/smartctl/midclt/gh/git executor + SMART classification + verification)
 * @skill systematic-debugging superpowers:systematic-debugging
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';

// PHASE 1 — investigate (READ-ONLY): gather all evidence about the degraded pool + member disks.
const investigateTask = defineTask('investigate', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Gather pool1 ZFS health + member-disk SMART evidence (read-only)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Storage/ZFS SRE on the Epaflix TrueNAS SCALE workstation',
      task:
        'Build the full evidence picture for the DEGRADED `' + args.pool + '` pool reported in issue #' +
        args.issueNumber + '. DO NOT change anything — strictly read-only diagnostics.',
      context: { ...args },
      instructions: [
        'Apply systematic-debugging discipline: collect evidence first, no changes.',
        'SSH access: `ssh truenas_admin@' + args.truenasHost + '` (passwordless key). For privileged calls prefix ' +
          '`echo "<TRUENAS_PASSWORD>" | sudo -S <cmd>`; read truenas_admin_username/password from ' +
          args.repoRoot + '/.github/instructions/secrets.yml (git-ignored).',
        '1) POOL STATE: `sudo -S zpool status -v ' + args.pool + '` (full, with the -v errors section) and ' +
          '`sudo -S zpool status -x`. Capture vdev layout, the exact GUID(s) flagged, READ/WRITE/CKSUM error counts, ' +
          'the ZFS-8000-9P notice, and whether `errors:` reports any known data errors. Confirm it is a non-redundant stripe.',
        '2) EVENT/HISTORY: `sudo -S zpool events -v | tail -n 200` (look for the WRITE-error/probe/IO events and timestamps) ' +
          'and `sudo -S zpool history ' + args.pool + ' | tail -n 40`. Note last scrub date/result from zpool status.',
        '3) MAP GUID -> PHYSICAL DISK: resolve each stripe member GUID to its /dev/ device and serial. Use ' +
          '`sudo -S zpool status -g ' + args.pool + '`, `ls -l /dev/disk/by-partuuid` / `by-id`, and ' +
          '`sudo -S midclt call disk.query | jq` (or `lsblk -o NAME,SERIAL,MODEL,SIZE`) to tie the flagged vdev GUID ' +
          'to a specific physical HDD (device, model, serial).',
        '4) SMART per member disk: for BOTH stripe HDDs run `sudo -S smartctl -a /dev/<dev>` (and `-x` if useful). ' +
          'Extract: overall SMART health PASSED/FAILED, Reallocated_Sector_Ct, Current_Pending_Sector, ' +
          'Offline_Uncorrectable, Reported_Uncorrect, UDMA_CRC_Error_Count (cable/controller signal), Power_On_Hours, ' +
          'temperature, and any self-test log errors. UDMA_CRC errors point at cabling/controller (transient); ' +
          'reallocated/pending/uncorrectable point at a dying platter (hardware replace).',
        '5) CONTEXT: `dmesg -T | grep -Ei "ata|scsi|i/o error|medium error|reset" | tail -n 60` for kernel-level disk ' +
          'errors, and confirm `' + args.pool + '/encrypted-backups` exists + is healthy (`sudo -S zfs list -r ' +
          args.pool + '`, `sudo -S zfs get encryption,keystatus ' + args.pool + '/encrypted-backups`).',
        'Classify the root cause from the evidence: (a) transient cabling/controller (CRC errors, zero reallocated) — ' +
          'clearable + monitor; (b) genuine disk degradation (reallocated/pending/uncorrectable rising, SMART FAILED) — ' +
          'needs hardware replacement; (c) ambiguous. State which.',
        'Save raw command output under tasks/' + taskCtx.effectId + '/ . Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['poolState', 'errorCounts', 'flaggedVdev', 'smart', 'rootCauseClass', 'dataErrors', 'evidence', 'summary'],
      properties: {
        poolState: { type: 'string', description: 'ONLINE|DEGRADED|FAULTED etc.' },
        isStripe: { type: 'boolean' },
        errorCounts: { type: 'object' },
        flaggedVdev: { type: 'object', description: 'guid, device, model, serial' },
        lastScrub: { type: 'string' },
        smart: { type: 'array', items: { type: 'object' }, description: 'per-disk SMART summary' },
        dataErrors: { type: 'string', description: 'errors: line from zpool status' },
        rootCauseClass: { type: 'string', enum: ['transient-cabling-controller', 'disk-degradation', 'ambiguous'] },
        evidence: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 2 — assess / fixability verdict (READ-ONLY reasoning over the evidence).
const assessTask = defineTask('assess', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Adversarially assess fixability + decide safe remediation vs hardware follow-ups',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Senior storage reliability engineer reviewing a degraded non-redundant ZFS pool',
      task:
        'From the read-only evidence, answer the owner question "can you fix it?" precisely, and produce an exact ' +
        'safe-remediation plan plus the hardware/redundancy recommendations. Do NOT apply anything.',
      context: { ...args },
      instructions: [
        'Be adversarial: a clean `errors: No known data errors` + a clean recent scrub does NOT mean the disk is healthy.',
        'Decide fixability honestly and split it: SOFTWARE-FIXABLE-NOW (clearable error state + verify) vs ' +
          'HARDWARE-REQUIRED (replace the failing HDD; and/or add a mirror to make the pool redundant). The latter ' +
          'cannot be done autonomously (needs a physical disk) — those are recommendations/follow-ups, not actions.',
        'If rootCauseClass=transient-cabling-controller (UDMA_CRC errors, zero reallocated/pending): `zpool clear ' +
          args.pool + '` then an on-demand `zpool scrub ' + args.pool + '` is the correct safe remediation; recommend ' +
          'reseating the SATA/power cable. If rootCauseClass=disk-degradation: clearing only resets counters and the ' +
          'errors WILL return — recommend disk replacement BEFORE relying on pool1/encrypted-backups, and warn that a ' +
          'scrub on a dying single-stripe disk adds load (still safe for data since no redundancy to resilver).',
        'Always note the standing risk: pool1 is a non-redundant stripe holding the master age-key backup; any single ' +
          'disk loss = total pool loss. Independent copies (workstation ~/.config/sops/age + in-cluster KSOPS) mean no ' +
          'lockout, but the #57 secure-home goal is not met until pool1 is ONLINE and ideally redundant.',
        'Produce: fixable (boolean — is there a safe software fix that improves the situation now), ' +
          'a precise ordered safeRemediation command list (the exact zpool clear / scrub commands, read-only re-checks), ' +
          'and followUps = array of {title, rationale, kind} for the hardware items (disk replace, add mirror).',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['fixable', 'verdict', 'rootCause', 'safeRemediation', 'expectedOutcome', 'followUps', 'risks', 'summary'],
      properties: {
        fixable: { type: 'boolean' },
        verdict: { type: 'string', description: 'plain-language can-we-fix-it answer' },
        rootCause: { type: 'string' },
        safeRemediation: { type: 'array', items: { type: 'object', properties: { step: { type: 'string' }, cmd: { type: 'string' }, why: { type: 'string' } } } },
        expectedOutcome: { type: 'string' },
        followUps: { type: 'array', items: { type: 'object', properties: { title: { type: 'string' }, rationale: { type: 'string' }, kind: { type: 'string' } } } },
        risks: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 3 — remediate (LIVE): apply the approved safe software remediation only.
const remediateTask = defineTask('remediate', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Apply approved safe remediation (zpool clear + on-demand scrub) on pool1',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Storage/ZFS SRE executing an approved, scoped remediation on TrueNAS',
      task: 'Execute ONLY the approved safeRemediation command list on `' + args.pool + '`. Nothing else. No disk ops.',
      context: { ...args },
      instructions: [
        'SSH `ssh truenas_admin@' + args.truenasHost + '`; privileged via `echo "<TRUENAS_PASSWORD>" | sudo -S <cmd>` ' +
          '(creds from ' + args.repoRoot + '/.github/instructions/secrets.yml).',
        'Run the approved commands IN ORDER exactly as listed in approvedRemediation. Typically: ' +
          '`sudo -S zpool clear ' + args.pool + '`, then capture `sudo -S zpool status -v ' + args.pool + '`, then ' +
          'if the plan includes it `sudo -S zpool scrub ' + args.pool + '` and capture initial `zpool status` ' +
          '(a scrub runs async — do NOT block waiting for completion; just record that it started + progress %).',
        'NEVER run `zpool replace`, `zpool offline/online`, `zpool detach`, partition or disk commands — those are ' +
          'hardware actions out of scope. If a command in the list looks destructive beyond clear/scrub, STOP and report.',
        'After clearing, re-read `sudo -S zpool status -v ' + args.pool + '` and report the new state + whether any ' +
          'WRITE/READ/CKSUM errors re-appeared immediately (a fast return = confirms a dying disk).',
        'Save raw output under tasks/' + taskCtx.effectId + '/ . Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['commandsRun', 'poolStateAfter', 'errorsReappeared', 'scrubStarted', 'summary'],
      properties: {
        commandsRun: { type: 'array', items: { type: 'string' } },
        poolStateAfter: { type: 'string' },
        errorsReappeared: { type: 'boolean' },
        scrubStarted: { type: 'boolean' },
        scrubProgress: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 4 — verify (READ-ONLY): confirm the remediation outcome.
const verifyTask = defineTask('verify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify pool1 state after remediation (read-only)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Storage SRE verifying a remediation outcome',
      task: 'Confirm the state of `' + args.pool + '` after remediation and judge whether the situation improved.',
      context: { ...args },
      instructions: [
        'SSH read-only. `sudo -S zpool status -v ' + args.pool + '` and `sudo -S zpool status -x`.',
        'Determine: is the pool ONLINE now (clear succeeded) or still DEGRADED? Did error counts stay at 0 or did they ' +
          'climb again right after clear (=> disk truly failing, software fix not durable)? If a scrub was started, note ' +
          'its progress (it may still be running — that is expected and fine).',
        'Set fixedNow=true ONLY if pool reports ONLINE with no immediately-returning errors. If errors returned, ' +
          'fixedNow=false and durable=false with hardware replacement required. needsSoak=true if a scrub is in flight ' +
          'or the verdict depends on watching for error recurrence over time.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['poolState', 'fixedNow', 'durable', 'needsSoak', 'evidence', 'summary'],
      properties: {
        poolState: { type: 'string' },
        fixedNow: { type: 'boolean' },
        durable: { type: 'boolean' },
        needsSoak: { type: 'boolean' },
        evidence: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 5 — report (OUTWARD): post findings + outcome to issue #124 and open follow-ups.
const reportTask = defineTask('report', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Report findings + outcome on issue #124 and open hardware follow-ups',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE closing the loop on a storage incident per Epaflix repo policy',
      task: 'Post the investigation findings + remediation outcome to issue #' + args.issueNumber +
        ' and open any agreed hardware/redundancy follow-up issues. NEVER print secrets/credentials.',
      context: { ...args },
      instructions: [
        'Use `gh` against repo ' + args.repo + '. NEVER include passwords, tokens, age keys, or disk-serial-derived ' +
          'secrets in any comment — disk model/serial for replacement is fine, credentials are not.',
        'Add a comment on issue #' + args.issueNumber + ' summarizing: root cause class, SMART findings (key counters), ' +
          'what was applied (zpool clear / scrub), the verified pool state after, and the honest fixability answer ' +
          '(software-fixed-now vs hardware-required). Keep it in the repo enhancement-issue tone.',
        'For each approved follow-up in followUps (e.g. replace failing HDD, add a mirror so the master-key backup ' +
          'lives on a redundant pool), open a `gh issue` on ' + args.repo + ' using the repo shape ' +
          '(## Finding / ## Current state / ## Desired outcome / ## Notes) and cross-link to #' + args.issueNumber +
          ' and #57. Do NOT duplicate a follow-up that issue #' + args.issueNumber + ' already fully covers — if the ' +
          'existing issue already tracks it, just note that in the comment instead of opening a dupe.',
        'If pool is now ONLINE and durable, consider whether issue #' + args.issueNumber + ' can be closed or should ' +
          'stay open pending redundancy — recommend, do not unilaterally close unless clearly resolved.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['commentUrl', 'followUpIssues', 'issueDisposition', 'summary'],
      properties: {
        commentUrl: { type: 'string' },
        followUpIssues: { type: 'array', items: { type: 'string' } },
        issueDisposition: { type: 'string', description: 'kept-open|closed|recommend-close' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

export async function process(inputs, ctx) {
  const cfg = {
    repoRoot: '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
    truenasHost: '192.168.10.200',
    pool: 'pool1',
    issueNumber: 124,
    issueUrl: 'https://github.com/SpyrosPsarras/epaflix/issues/124',
    repo: 'SpyrosPsarras/epaflix',
    ...inputs,
  };

  ctx.log('info', `pool1-degraded-remediation: pool=${cfg.pool} host=${cfg.truenasHost} issue=#${cfg.issueNumber}`);

  // PHASE 1 — investigate (read-only)
  const inv = await ctx.task(investigateTask, {
    repoRoot: cfg.repoRoot, truenasHost: cfg.truenasHost, pool: cfg.pool, issueNumber: cfg.issueNumber,
  });
  ctx.log('info', `Investigated: state=${inv.poolState}; rootCauseClass=${inv.rootCauseClass}`);

  // PHASE 2 — assess fixability (read-only)
  const assess = await ctx.task(assessTask, {
    pool: cfg.pool, issueNumber: cfg.issueNumber, investigation: inv,
  });
  ctx.log('info', `Assessed: fixable=${assess.fixable}; followUps=${(assess.followUps || []).length}`);

  // GATE 1 (live storage write to the pool holding the master-key backup) — mandatory before any mutation.
  const gate1 = await ctx.breakpoint({
    question:
      'Approve the LIVE safe remediation on `' + cfg.pool + '` (the non-redundant pool that now holds the SOPS ' +
      'master age-key backup)?\n\n' +
      'Pool state: ' + inv.poolState + '   Root cause: ' + assess.rootCause + '\n' +
      'Fixable in software now? ' + assess.fixable + ' — ' + assess.verdict + '\n\n' +
      'Proposed ordered remediation:\n' + JSON.stringify(assess.safeRemediation, null, 2) + '\n\n' +
      'Expected outcome: ' + assess.expectedOutcome + '\n' +
      'Risks: ' + JSON.stringify(assess.risks) + '\n' +
      'Hardware follow-ups (NOT executed here): ' + JSON.stringify((assess.followUps || []).map(f => f.title)) + '\n\n' +
      'This runs `zpool clear`' + ' (+ optional scrub) against the pool. Apply now?',
    options: ['Approve remediation', 'Report findings only (no live change)', 'Abort'],
    expert: 'owner',
    tags: ['deploy', 'storage', 'approval-gate'],
  });

  const r1 = (gate1.response || '').toLowerCase();
  if (!gate1.approved || r1.includes('abort')) {
    ctx.log('warn', 'Remediation not approved and report declined — stopping after read-only diagnosis.');
    return {
      success: true, fixable: assess.fixable, rootCause: assess.rootCause, remediationApplied: false,
      poolStateAfter: inv.poolState, fixedNow: false, reason: 'aborted-after-diagnosis',
      investigation: inv, assessment: assess, summary: assess.summary,
    };
  }

  const reportOnly = r1.includes('report findings only') || r1.includes('no live change');

  let remediate = null;
  let verify = null;
  if (!reportOnly) {
    // PHASE 3 — remediate (live, approved scope only)
    remediate = await ctx.task(remediateTask, {
      repoRoot: cfg.repoRoot, truenasHost: cfg.truenasHost, pool: cfg.pool,
      approvedRemediation: assess.safeRemediation, ownerFeedback: gate1.response || '',
    });
    ctx.log('info', `Remediated: stateAfter=${remediate.poolStateAfter}; errorsReappeared=${remediate.errorsReappeared}`);

    // PHASE 4 — verify (read-only)
    verify = await ctx.task(verifyTask, {
      truenasHost: cfg.truenasHost, pool: cfg.pool, remediation: remediate,
    });
    ctx.log('info', `Verified: state=${verify.poolState}; fixedNow=${verify.fixedNow}; durable=${verify.durable}`);
  } else {
    ctx.log('info', 'Owner chose report-only — skipping live remediation.');
  }

  // GATE 2 (outward-facing: issue comment + follow-up issues) — approve reporting.
  const gate2 = await ctx.breakpoint({
    question:
      (reportOnly ? 'Report-only path.' :
        'Remediation applied. Pool state after = ' + (verify ? verify.poolState : '?') +
        ', fixedNow=' + (verify ? verify.fixedNow : '?') + (verify && verify.needsSoak ? ', soak/scrub in flight' : '') + '.') +
      '\n\nPost findings + outcome as a comment on issue #' + cfg.issueNumber + ' and open hardware follow-up issues ' +
      '(' + JSON.stringify((assess.followUps || []).map(f => f.title)) + ') per repo policy? Approve outward reporting?',
    options: ['Approve report + follow-ups', 'Comment only (no new issues)', 'Skip reporting'],
    expert: 'owner',
    tags: ['outward-facing', 'destructive-git', 'approval-gate'],
  });

  const r2 = (gate2.response || '').toLowerCase();
  let report = null;
  if (gate2.approved && !r2.includes('skip')) {
    const commentOnly = r2.includes('comment only');
    report = await ctx.task(reportTask, {
      repoRoot: cfg.repoRoot, repo: cfg.repo, issueNumber: cfg.issueNumber, issueUrl: cfg.issueUrl,
      investigation: inv, assessment: assess, remediation: remediate, verification: verify,
      followUps: commentOnly ? [] : (assess.followUps || []), commentOnly,
    });
    ctx.log('info', `Reported: comment=${report.commentUrl}; followUps=${(report.followUpIssues || []).length}`);
  } else {
    ctx.log('warn', 'Reporting skipped by owner.');
  }

  return {
    success: true,
    fixable: assess.fixable,
    rootCause: assess.rootCause,
    remediationApplied: !!remediate,
    poolStateAfter: verify ? verify.poolState : inv.poolState,
    fixedNow: verify ? verify.fixedNow : false,
    durable: verify ? verify.durable : false,
    needsSoak: verify ? verify.needsSoak : false,
    issueUrl: report ? report.commentUrl : cfg.issueUrl,
    followUps: report ? report.followUpIssues : (assess.followUps || []).map(f => f.title),
    summary: (verify && verify.summary) || assess.summary,
  };
}
