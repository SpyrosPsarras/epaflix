/**
 * @process specializations/devops-sre-platform/deliver-issue-138-cleanuparr-blocklist
 * @description Deliver GitHub issue #138 — "Cleanuparr: durable capture of Sonarr custom
 *   blocklist + soak-confirm seriesId 40 S04E13 fix". Three deliverables:
 *     (a) SOAK-CONFIRM the 2026-05-31 fix: verify (read-only) that seriesId 40 / episodeId 3143
 *         (S04E13, hash 66a4dc...) stayed dead across >= 1 full newtarr hunt interval — no new
 *         grab, no new strike, no new Action Required event since the fix.
 *     (b) DURABILITY: make the Sonarr custom blocklist reproducible on a PVC rebuild by codifying
 *         /config/custom-blocklist-sonarr.txt (+ the content_blocker sonarr_blocklist_path pointer)
 *         into git, following the EXACT #137/PR#178 newtarr-config SOPS-seed model
 *         (SOPS *.enc.yaml Secret reconciled by the existing ksops generator + a non-clobbering
 *         seed-config initContainer). Concrete instance of umbrella issue #180.
 *     (c) DECISION: evaluate whether to re-enable Cleanuparr per-arr failed_import striking for
 *         Sonarr (currently failed_import_max_strikes = -1). Owner decides at the plan gate.
 *
 *   Live-change risk: part (b) creates a SOPS-encrypted Secret (secrets gate) and the manifest
 *   change deploys via ArgoCD selfHeal on merge (deploy gate). Both are gated by mandatory
 *   breakpoints, calibrated to the project profile (low breakpointTolerance; alwaysBreakOn
 *   secrets-rotation, deploy, architecture-change, destructive-git).
 *
 * @inputs { repoRoot, repo, issue, namespace, seriesId, episodeId, episode, hash, fixDate,
 *           blocklistFile, blocklistPathKey, failedImportStrikesArr, cleanuparrConfigPvc }
 * @outputs { success, soakConfirmed, durabilityCodified, partCDecision, prUrl, issueClosed, summary }
 *
 * @agent general-purpose specializations/devops-sre-platform/agents/platform-engineer/AGENT.md
 * @skill systematic-debugging superpowers:systematic-debugging
 * @skill verification-before-completion superpowers:verification-before-completion
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

// ===========================================================================
// PHASE 1 — Recon (read-only): capture live state + the live blocklist file
// ===========================================================================
const reconTask = defineTask('recon', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Recon: live Cleanuparr/Sonarr state + capture live blocklist (read-only)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr/Kubernetes SRE on the Epaflix k3s cluster gathering read-only ground truth',
      task:
        'Gather the live state needed to deliver issue #' + args.issue + ' parts (a) soak-confirm, ' +
        '(b) blocklist durability, (c) failed_import striking decision. DO NOT change anything — read-only. ' +
        'Critically, CAPTURE the live custom blocklist file content verbatim so it can be codified later.',
      context: { ...args },
      instructions: [
        'Apply systematic-debugging discipline: collect evidence, do not assume the issue body is still accurate.',
        'Determine cluster access first: try `kubectl -n ' + args.namespace + ' get pods` locally; if no kubeconfig, ' +
          'ssh to a master (ssh ubuntu@192.168.10.51) and use kubectl there. Record which access path works.',
        'CLEANUPARR CONFIG (v2+, JSON not SQLite): `kubectl -n ' + args.namespace + ' exec deploy/cleanuparr -- sh -c ' +
          '"find /config -maxdepth 3 -type f | head -50 && echo --- && cat /config/*.json 2>/dev/null"`. ' +
          'Find: content_blocker config, the sonarr_blocklist_path value (issue says it points at ' +
          '"' + args.blocklistFile + '"), and the per-arr failed_import_max_strikes for Sonarr (issue says -1).',
        'CAPTURE THE BLOCKLIST FILE VERBATIM: `kubectl -n ' + args.namespace + ' exec deploy/cleanuparr -- cat ' +
          args.blocklistFile + '` and SAVE it byte-for-byte to tasks/' + taskCtx.effectId + '/custom-blocklist-sonarr.txt. ' +
          'Record its line count and sha256. This file is the artifact that part (b) will codify — it must be captured exactly.',
        'Note HOW the sonarr_blocklist_path pointer is stored (a JSON config key vs a UI/DB setting) — this determines ' +
          'whether codifying the .txt file alone is sufficient, or whether the pointer also needs codifying/documenting.',
        'SOAK EVIDENCE (part a): the fix landed ' + args.fixDate + '. Query Sonarr (read its API key from ' +
          '`kubectl -n ' + args.namespace + ' exec deploy/sonarr -- cat /config/config.xml`) for seriesId ' + args.seriesId +
          ' / episodeId ' + args.episodeId + ' (' + args.episode + '): current monitored flag, hasFile, and History/Blocklist ' +
          'entries DATED AFTER ' + args.fixDate + ' (any grab/import/failed/redownload). Also check qbittorrent for hash ' +
          args.hash + ' (still absent?). Also check Cleanuparr manual_events / Action Required count (issue says reset to 0).',
        'Confirm the current newtarr hunt cadence (memory: seasons_packs/3600 = hourly) so we can assert >= 1 interval elapsed ' +
          'between ' + args.fixDate + ' and today.',
        'Inspect the repo for the codify template: read 2-k3s/08.servarr/newtarr/newtarr.yaml (seed-config initContainer), ' +
          '2-k3s/08.servarr/_shared/secrets/ksops-generator.yaml, and 2-k3s/08.servarr/cleanuparr/cleanuparr.yaml so the plan ' +
          'can mirror the #137/PR#178 pattern exactly.',
        'Return ONLY structured JSON. Include the captured-file path, lineCount, sha256, blocklistPathValue, ' +
          'failedImportStrikesValue, soakEvidence (lists of post-fix events, ideally empty), and accessPath.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['blocklistCaptured', 'soakEvidence', 'failedImportStrikesValue'],
      properties: {
        accessPath: { type: 'string' },
        blocklistCaptured: { type: 'boolean' },
        capturedFilePath: { type: 'string' },
        lineCount: { type: 'number' },
        sha256: { type: 'string' },
        blocklistPathValue: { type: 'string' },
        blocklistPathStorage: { type: 'string' },
        failedImportStrikesValue: { type: 'string' },
        soakEvidence: { type: 'object' },
        huntCadence: { type: 'string' },
        notes: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ===========================================================================
// PHASE 2 — Soak-confirm (read-only verification of part a)
// ===========================================================================
const soakConfirmTask = defineTask('soak-confirm', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Soak-confirm (part a): assert S04E13 stayed dead since the fix',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE adversarially verifying that a closed incident did not recur',
      task:
        'Decide PASS/FAIL for issue #' + args.issue + ' part (a): has seriesId ' + args.seriesId + ' / episodeId ' +
        args.episodeId + ' (' + args.episode + ') stayed dead across >= 1 full newtarr hunt interval since ' + args.fixDate +
        '? PASS only if there is NO new grab, NO new strike, and NO new Action Required event after the fix.',
      context: { ...args, recon: args.recon },
      instructions: [
        'Use the recon soakEvidence as the basis but VERIFY it yourself — re-query Sonarr History/Blocklist and Cleanuparr ' +
          'events for anything dated after ' + args.fixDate + ' if the recon evidence is thin or ambiguous.',
        'Be adversarial: default to FAIL if evidence is missing or inconclusive. A genuine PASS needs positive evidence that ' +
          'the relevant windows were actually checked and are clean.',
        'Confirm at least one full hunt interval elapsed (fixDate ' + args.fixDate + ' vs today) so the soak is meaningful.',
        'Return ONLY JSON: { soakPass, intervalsElapsed, postFixEvents: [...], reasoning }.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['soakPass', 'reasoning'],
      properties: {
        soakPass: { type: 'boolean' },
        intervalsElapsed: { type: 'number' },
        postFixEvents: { type: 'array' },
        reasoning: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ===========================================================================
// PHASE 3 — Plan the durability codification (part b) + part (c) recommendation
// ===========================================================================
const planTask = defineTask('plan', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Plan durability codify (part b) + failed_import decision recommendation (part c)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'GitOps platform engineer designing a minimal, reversible IaC change',
      task:
        'Produce an EXACT implementation plan to codify the Cleanuparr Sonarr custom blocklist durably (part b), mirroring ' +
        'the #137/PR#178 newtarr-config SOPS-seed pattern, AND a clear recommendation for part (c) (re-enable Sonarr ' +
        'failed_import striking, currently ' + (args.recon && args.recon.failedImportStrikesValue) + ').',
      context: { ...args, recon: args.recon, feedback: args.feedback, attempt: args.attempt },
      instructions: [
        'Mirror the newtarr seed model precisely (read newtarr.yaml + ksops-generator.yaml first if not already):',
        '  1. Create 2-k3s/08.servarr/_shared/secrets/cleanuparr-config-seed.enc.yaml — a kind: Secret whose stringData ' +
          '(or data) carries the captured custom-blocklist-sonarr.txt content under a sane key (e.g. custom-blocklist-sonarr.txt). ' +
          'It MUST be SOPS-encrypted per .sops.yaml (.enc.yaml suffix, encrypted_regex data|stringData) so the pre-commit hook ' +
          'and CI accept it. Do NOT commit plaintext kind: Secret.',
        '  2. Add that file to the files: list in _shared/secrets/ksops-generator.yaml so ArgoCD/KSOPS reconciles it.',
        '  3. Add a non-clobbering seed-config initContainer to cleanuparr/cleanuparr.yaml (busybox, runAsUser 0) that copies ' +
          'each /seed/* file into /config/<name> ONLY IF it does not already exist (cp + skip-if-exists, then chown 568:568), ' +
          'and a `seed` volume projecting the cleanuparr-config-seed Secret. Exactly like newtarr.yaml. This restores the ' +
          'blocklist .txt onto a freshly-provisioned local-path PVC without ever clobbering live config.',
        '  4. Decide & document the sonarr_blocklist_path POINTER durability: if it is a JSON config key, consider whether the ' +
          'seed can also restore/repoint it; if it is a DB/UI setting Cleanuparr rewrites at runtime, DOCUMENT the one manual ' +
          'repoint step in RECOVERY-newtarr-cleanuparr.md (the .txt is restored automatically; the pointer is set once). Pick ' +
          'the simplest correct approach and state it explicitly.',
        '  5. Update the kustomization.yaml header comment block to list cleanuparr-config-seed alongside newtarr-config-seed ' +
          '(provenance), and add a short note in RECOVERY-newtarr-cleanuparr.md explaining the new durable seed.',
        'STATIC-SNAPSHOT caveat: the blocklist is a frozen snapshot of upstream flmorg/cleanuperr (will not auto-track upstream). ' +
          'Note this and propose tracking it as a follow-up under umbrella issue #180 (do NOT expand scope now).',
        'Part (c) RECOMMENDATION: weigh re-enabling Sonarr failed_import striking vs the newtarr re-arm race (memory ' +
          'project_sonarr2_huntarr_race / #135). Give a clear recommend (keep -1 / re-enable with value N) + rationale, and ' +
          'note this is the OWNER decision at the gate. If deferred, it becomes a follow-up issue.',
        'Respect the Epaflix merge policy in the plan (branch + PR, rebase onto origin/main, force-with-lease, wait validate, ' +
          'gh pr merge --merge) and the media-title scrub rule (refer to the series only as seriesId ' + args.seriesId + ').',
        'If feedback is present, revise the plan to address it.',
        'Write the plan to tasks/' + taskCtx.effectId + '/PLAN.md AND return structured JSON summarizing files-to-change, ' +
          'the pointer-durability decision, the part-c recommendation, and risks.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['filesToChange', 'pointerDurabilityApproach', 'partCRecommendation'],
      properties: {
        planPath: { type: 'string' },
        filesToChange: { type: 'array', items: { type: 'string' } },
        pointerDurabilityApproach: { type: 'string' },
        partCRecommendation: { type: 'string' },
        risks: { type: 'array' },
        followUps: { type: 'array' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

const planCheckTask = defineTask('plan-check', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Adversarially review the codify plan against the #137 template',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Skeptical GitOps reviewer',
      task: 'Find gaps in the plan that would make it fail SOPS/pre-commit/CI, break ArgoCD reconcile, clobber live config, ' +
        'or miss the sonarr_blocklist_path pointer. Default to NOT-approved if any real gap exists.',
      context: { ...args },
      instructions: [
        'Verify the plan: (1) uses .enc.yaml + SOPS so the pre-commit guard and CI pass; (2) wires the seed into ' +
          'ksops-generator.yaml files: list; (3) the initContainer is genuinely non-clobbering (skip-if-exists) and chowns 568; ' +
          '(4) addresses the pointer durability explicitly; (5) does not expand scope beyond issue #138.',
        'Return ONLY JSON: { approved: boolean, blockingIssues: [...], suggestions: [...] }.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['approved'],
      properties: { approved: { type: 'boolean' }, blockingIssues: { type: 'array' }, suggestions: { type: 'array' } },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ===========================================================================
// PHASE 4 — Implement the codification (manifests + SOPS seed)
// ===========================================================================
const implementTask = defineTask('implement', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Implement codify: SOPS seed + initContainer + ksops wiring + docs',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'GitOps platform engineer applying an approved IaC change to the repo working tree',
      task: 'Implement the approved plan in ' + args.repoRoot + ' on a NEW branch off origin/main. Do NOT commit/push yet — ' +
        'leave the diff staged-ready for review at the next breakpoint.',
      context: { ...args, plan: args.plan, recon: args.recon, partCDecision: args.partCDecision },
      instructions: [
        'Create a feature branch off the latest origin/main (e.g. servarr-cleanuparr-blocklist-codify-138). Do not work on main.',
        'Create 2-k3s/08.servarr/_shared/secrets/cleanuparr-config-seed.enc.yaml carrying the captured blocklist content ' +
          '(from the recon capturedFilePath — use it verbatim, do NOT re-fetch a different snapshot) and ENCRYPT it with SOPS: ' +
          '`sops --encrypt --in-place 2-k3s/08.servarr/_shared/secrets/cleanuparr-config-seed.enc.yaml` (creation rule in ' +
          '.sops.yaml handles the age recipient). Verify the result has no plaintext stringData/data. Follow ' +
          '.github/instructions/sops.instructions.md.',
        'Add the new file to the files: list in 2-k3s/08.servarr/_shared/secrets/ksops-generator.yaml.',
        'Add the non-clobbering seed-config initContainer + seed Secret volume to 2-k3s/08.servarr/cleanuparr/cleanuparr.yaml, ' +
          'modeled exactly on newtarr.yaml. The init must mkdir as needed, cp skip-if-exists, chown -R 568:568 /config.',
        'Apply the pointer-durability approach from the plan (JSON repoint via seed if applicable, else a documented one-time ' +
          'manual step in RECOVERY-newtarr-cleanuparr.md).',
        'Update 2-k3s/08.servarr/kustomization.yaml header comments (list cleanuparr-config-seed) and ' +
          'RECOVERY-newtarr-cleanuparr.md with the durable-seed note. Refer to the series only as seriesId ' + args.seriesId +
          ' — never a release/show title.',
        'If partCDecision says re-enable striking NOW, also state precisely how (but live Cleanuparr config changes are a ' +
          'separate deploy — capture the exact value; do NOT mutate live Cleanuparr here). If deferred, do nothing for part c here.',
        'Run the pre-commit hook check locally if available (.github/hooks/check-sops-encrypted.sh) to confirm no plaintext Secret.',
        'Return ONLY JSON: { branch, filesChanged: [...], sopsEncryptedOk, diffSummary }.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'filesChanged', 'sopsEncryptedOk'],
      properties: {
        branch: { type: 'string' },
        filesChanged: { type: 'array', items: { type: 'string' } },
        sopsEncryptedOk: { type: 'boolean' },
        diffSummary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// kustomize build validation (quality gate) — shell
const kustomizeBuildTask = defineTask('kustomize-build', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Validate: kustomize build (with helm) of 2-k3s/08.servarr',
  shell: {
    command:
      'cd ' + args.repoRoot + ' && (kustomize build --enable-helm 2-k3s/08.servarr >/tmp/servarr-build-138.yaml 2>/tmp/servarr-build-138.err ' +
      '&& echo \'{"buildOk": true, "lines": \'$(wc -l </tmp/servarr-build-138.yaml)\'}\' ' +
      '|| echo \'{"buildOk": false, "err": "see /tmp/servarr-build-138.err"}\')',
    parseOutput: 'json',
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/result.json`,
  },
  labels: ['validation', 'kustomize'],
}));

// ===========================================================================
// PHASE 5 — PR + merge (per Epaflix merge policy) — gated by deploy breakpoint
// ===========================================================================
const prTask = defineTask('pr', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Commit, push, open PR, rebase, wait validate, merge (Epaflix policy)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Release engineer following the Epaflix merge-commit + mandatory-rebase policy',
      task: 'Commit the approved diff on branch ' + (args.impl && args.impl.branch) + ', push, open a PR on ' + args.repo +
        ' that closes/advances issue #' + args.issue + ', then rebase onto origin/main, force-with-lease, wait for the ' +
        'required `validate` check, and merge with `gh pr merge --merge`.',
      context: { ...args, impl: args.impl, plan: args.plan },
      instructions: [
        'Commit with a clear conventional message (e.g. "feat(servarr): codify Cleanuparr Sonarr blocklist as SOPS seed (#138)"). ' +
          'Do NOT git add -f anything gitignored. Refer to the series only as seriesId ' + args.seriesId + '.',
        'PR body: summarize parts (a) soak result, (b) codify, (c) decision; cross-link #137, #180 (umbrella), #135, #131. ' +
          'Include a Test plan / Verification checklist (kustomize build, SOPS encrypted, ArgoCD reconcile, seed no-op on live PVC).',
        'Rebase the branch onto origin/main and `git push --force-with-lease`. Wait for the `validate` check to pass ' +
          '(`gh pr checks` / `gh run watch`); if it fails on the known unpinned-kustomize flake (#164), `gh run rerun --failed`.',
        'Merge with `gh pr merge <n> --merge` (semi-linear; produces the Merge-PR marker). Confirm merged.',
        'Return ONLY JSON: { prUrl, prNumber, merged, mergeCommit, validatePassed }.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'merged'],
      properties: {
        prUrl: { type: 'string' }, prNumber: { type: 'number' }, merged: { type: 'boolean' },
        mergeCommit: { type: 'string' }, validatePassed: { type: 'boolean' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ===========================================================================
// PHASE 6 — Post-merge verify + follow-ups + close issue
// ===========================================================================
const closeoutTask = defineTask('closeout', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Post-merge verify, open follow-ups, update + close issue #138',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE verifying a deploy and closing the loop per repo policy',
      task: 'Verify ArgoCD reconciled the servarr Application (Synced/Healthy) and that Cleanuparr came up with the blocklist ' +
        'present (seed no-op on the live PVC), then close out issue #' + args.issue + ' properly.',
      context: { ...args, pr: args.pr, soak: args.soak, plan: args.plan, partCDecision: args.partCDecision },
      instructions: [
        'Verify: ArgoCD servarr App Synced+Healthy after merge; the cleanuparr-config-seed Secret exists in the servarr ' +
          'namespace; the cleanuparr pod restarted cleanly and ' + args.blocklistFile + ' is still present and unchanged ' +
          '(seed must have been a no-op on the live PVC — confirm by sha256 vs the recon capture).',
        'Open gh issues on ' + args.repo + ' for deferred items (use the ## Finding / ## Current state / ## Desired outcome / ' +
          '## Notes shape, cross-link): (1) static-snapshot blocklist will not auto-track upstream flmorg/cleanuperr — link under ' +
          'umbrella #180; (2) if part (c) was DEFERRED, open an issue to decide failed_import striking later. Skip any that are ' +
          'already covered by an existing open issue (search first).',
        'Update issue #' + args.issue + ' DESCRIPTION (edit the body — NEVER add a new comment): tick the (a)/(b)/(c) desired ' +
          'outcomes with inline results; strike through anything no longer applicable with a note. Record the soak PASS/FAIL, ' +
          'the PR link, and the part-c decision.',
        'If all three desired outcomes are satisfied (soak PASS + durability merged + part-c decided/deferred-with-issue), close ' +
          'issue #' + args.issue + ' with a short summary comment referencing the PR. Otherwise leave it open and say why.',
        'Refer to the series only as seriesId ' + args.seriesId + '. Return ONLY JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['argoHealthy', 'issueClosed'],
      properties: {
        argoHealthy: { type: 'boolean' },
        seedNoOpConfirmed: { type: 'boolean' },
        followUpIssues: { type: 'array' },
        issueClosed: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ===========================================================================
// ORCHESTRATION
// ===========================================================================
export async function process(inputs, ctx) {
  const a = {
    repoRoot: inputs.repoRoot || '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
    repo: inputs.repo || 'SpyrosPsarras/epaflix',
    issue: inputs.issue || 138,
    namespace: inputs.namespace || 'servarr',
    seriesId: inputs.seriesId || 40,
    episodeId: inputs.episodeId || 3143,
    episode: inputs.episode || 'S04E13',
    hash: inputs.hash || '66a4dc6201cb149ff70eed12b9902317cb82ed87',
    fixDate: inputs.fixDate || '2026-05-31',
    blocklistFile: inputs.blocklistFile || '/config/custom-blocklist-sonarr.txt',
    failedImportStrikesArr: inputs.failedImportStrikesArr || 'sonarr',
    cleanuparrConfigPvc: inputs.cleanuparrConfigPvc || 'cleanuparr-config',
  };

  ctx.log('info', `Delivering issue #${a.issue}: Cleanuparr blocklist durability + soak-confirm`);

  // PHASE 1 — recon (read-only)
  const recon = await ctx.task(reconTask, a);

  // PHASE 2 — soak-confirm (part a)
  const soak = await ctx.task(soakConfirmTask, { ...a, recon });

  // PHASE 3 — plan (part b design + part c recommendation) with adversarial review loop
  let plan = await ctx.task(planTask, { ...a, recon });
  for (let i = 0; i < 2; i++) {
    const check = await ctx.task(planCheckTask, { ...a, plan, recon });
    if (check.approved) break;
    plan = await ctx.task(planTask, {
      ...a, recon,
      feedback: (check.blockingIssues || []).concat(check.suggestions || []).join('; '),
      attempt: i + 2,
    });
  }

  // BP1 — owner approves plan + soak interpretation + decides part (c) (architecture-change gate)
  let partCDecision = plan.partCRecommendation;
  let bp1Feedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (bp1Feedback) {
      plan = await ctx.task(planTask, { ...a, recon, feedback: bp1Feedback, attempt: attempt + 1 });
    }
    const bp1 = await ctx.breakpoint({
      question:
        `Issue #${a.issue} plan gate.\n` +
        `(a) SOAK: ${soak.soakPass ? 'PASS' : 'FAIL'} — ${soak.reasoning}\n` +
        `(b) CODIFY plan: change ${(plan.filesToChange || []).join(', ')}; pointer durability: ${plan.pointerDurabilityApproach}\n` +
        `(c) DECISION (failed_import striking for Sonarr, currently ${recon.failedImportStrikesValue}): ` +
        `recommendation = ${plan.partCRecommendation}.\n` +
        `Approve to implement the codify (SOPS seed + initContainer) and proceed? Also confirm/override the part (c) decision.`,
      title: `Plan + part(c) decision gate — issue #${a.issue}`,
      expert: 'owner',
      tags: ['approval-gate', 'architecture-change'],
      context: {
        runId: ctx.runId,
        files: [
          { path: plan.planPath || `tasks/plan/PLAN.md`, format: 'markdown', label: 'Codify plan' },
        ],
        soak, plan, partCRecommendation: plan.partCRecommendation,
      },
      previousFeedback: bp1Feedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    if (bp1.approved) {
      if (bp1.response) partCDecision = bp1.response;
      if (bp1.feedback) partCDecision = bp1.feedback;
      break;
    }
    bp1Feedback = bp1.response || bp1.feedback || 'Changes requested';
  }

  // PHASE 4 — implement + validate (build-fix loop)
  let impl = await ctx.task(implementTask, { ...a, plan, recon, partCDecision });
  let build = await ctx.task(kustomizeBuildTask, a);
  for (let i = 0; i < 2 && !(build && build.buildOk); i++) {
    impl = await ctx.task(implementTask, {
      ...a, plan, recon, partCDecision,
      feedback: `kustomize build failed: ${build && build.err}. Fix the manifests on branch ${impl.branch}.`,
    });
    build = await ctx.task(kustomizeBuildTask, a);
  }

  // BP2 — owner reviews exact diff; approves commit + push + PR + merge + DEPLOY (secrets + deploy gate)
  let bp2Feedback = null;
  let approvedToShip = false;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (bp2Feedback) {
      impl = await ctx.task(implementTask, { ...a, plan, recon, partCDecision, feedback: bp2Feedback });
      build = await ctx.task(kustomizeBuildTask, a);
    }
    const bp2 = await ctx.breakpoint({
      question:
        `Ship gate — issue #${a.issue}.\n` +
        `Branch: ${impl.branch}\nFiles: ${(impl.filesChanged || []).join(', ')}\n` +
        `SOPS encrypted OK: ${impl.sopsEncryptedOk}\nkustomize build: ${build && build.buildOk ? 'OK' : 'FAILED'}\n` +
        `${impl.diffSummary || ''}\n\n` +
        `Approving will: commit, push, open a PR, rebase onto origin/main, wait for validate, MERGE, and DEPLOY via ArgoCD ` +
        `selfHeal (creates a SOPS Secret + restarts cleanuparr). Approve?`,
      title: `Ship + deploy gate — issue #${a.issue}`,
      expert: 'owner',
      tags: ['approval-gate', 'deploy', 'secrets-rotation', 'destructive-git'],
      context: { runId: ctx.runId, impl, build },
      previousFeedback: bp2Feedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    if (bp2.approved) { approvedToShip = true; break; }
    bp2Feedback = bp2.response || bp2.feedback || 'Changes requested';
  }

  if (!approvedToShip) {
    return {
      success: false,
      reason: 'Ship gate not approved after retries',
      soakConfirmed: soak.soakPass, durabilityCodified: false, partCDecision,
      metadata: { processId: 'deliver-issue-138-cleanuparr-blocklist', runId: ctx.runId },
    };
  }

  // PHASE 5 — PR + merge
  const pr = await ctx.task(prTask, { ...a, impl, plan });

  // PHASE 6 — post-merge verify + follow-ups + close
  const closeout = await ctx.task(closeoutTask, { ...a, pr, soak, plan, partCDecision });

  ctx.log('info', `Issue #${a.issue} delivery complete. PR=${pr.prUrl} closed=${closeout.issueClosed}`);

  return {
    success: !!(pr.merged && closeout.argoHealthy),
    soakConfirmed: soak.soakPass,
    durabilityCodified: !!pr.merged,
    partCDecision,
    prUrl: pr.prUrl,
    issueClosed: !!closeout.issueClosed,
    followUpIssues: closeout.followUpIssues || [],
    summary: closeout.summary,
    metadata: { processId: 'deliver-issue-138-cleanuparr-blocklist', runId: ctx.runId },
  };
}

export default process;
