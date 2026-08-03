/**
 * @process specializations/devops-sre-platform/deliver-issue-182-cleanuparr-blocklist-drift
 * @description Deliver GitHub issue #182 — "Cleanuparr Sonarr blocklist snapshot won't
 *   auto-track upstream flmorg/cleanuperr (drift refresh)". The codified Sonarr custom
 *   blocklist (cleanuparr-blocklist-seed, #138/PR#181) is a FROZEN snapshot of upstream
 *   flmorg/cleanuperr; unlike the Radarr blocklist (which tracks the live upstream URL), it
 *   will never auto-track new upstream entries. This is fundamentally a DECISION issue:
 *     - option A: a periodic re-snapshot job (pull upstream, re-merge the local seriesId 40
 *       regex, re-encrypt the SOPS Secret) — mirrors the newtarr drift-refresh #179;
 *     - option B: repoint Cleanuparr's sonarr_blocklist_path back at the live upstream URL
 *       (like Radarr) and keep ONLY the local seriesId 40 regex as the codified overlay;
 *     - option C/hybrid: keep the snapshot but add a drift-DETECTOR that only alerts loudly
 *       when upstream diverges, leaving the re-snapshot manual (lowest maintenance).
 *   The owner picks the option at an architecture-change breakpoint; the process then
 *   implements the chosen option, validates, ships per the Epaflix merge policy, verifies the
 *   deploy, and closes the issue with follow-ups cross-linked to #179 / #180.
 *
 *   Live-change risk: option A re-encrypts a SOPS Secret (secrets gate) and may add a CronJob;
 *   option B requires a one-time live Cleanuparr config repoint (deploy gate) + shrinking the
 *   SOPS seed. All merges deploy via ArgoCD selfHeal. Gated by mandatory breakpoints calibrated
 *   to the project profile (low breakpointTolerance; alwaysBreakOn deploy + destructive-git;
 *   secrets-rotation + architecture-change treated as always-break here).
 *
 * @inputs { repoRoot, repo, issue, namespace, seriesId, blocklistFile, blocklistPathKey,
 *           seedSecret, seedFile, upstreamRepo, cleanuparrConfigPvc, relatedIssues }
 * @outputs { success, chosenOption, implemented, prUrl, issueClosed, followUpIssues, summary }
 *
 * @agent general-purpose specializations/devops-sre-platform/agents/platform-engineer/AGENT.md
 * @skill systematic-debugging superpowers:systematic-debugging
 * @skill verification-before-completion superpowers:verification-before-completion
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

// ===========================================================================
// PHASE 1 — Recon (read-only): capture the facts the A/B/C decision needs
// ===========================================================================
const reconTask = defineTask('recon', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Recon: live Cleanuparr blocklist config + upstream drift (read-only)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr/Kubernetes SRE on the Epaflix k3s cluster gathering read-only ground truth',
      task:
        'Gather the facts needed to DECIDE issue #' + args.issue + ' (how the Sonarr custom blocklist stays current with ' +
        'upstream ' + args.upstreamRepo + '). DO NOT change anything — read-only. The output drives an owner A/B/C decision.',
      context: { ...args },
      instructions: [
        'Apply systematic-debugging discipline: collect evidence; do not assume the issue body is still accurate.',
        'Cluster access: try `kubectl -n ' + args.namespace + ' get pods` locally; if no kubeconfig, ssh ubuntu@192.168.10.51 ' +
          'and use kubectl there. Record which access path works.',
        'CLEANUPARR CONFIG (v2+ stores config in JSON under /config, not the legacy SQLite): ' +
          '`kubectl -n ' + args.namespace + ' exec deploy/cleanuparr -- sh -c "find /config -maxdepth 3 -type f && echo --- && ' +
          'cat /config/*.json 2>/dev/null"`. Extract the content_blocker config for BOTH arrs:',
        '  - SONARR: the current `' + args.blocklistPathKey + '` value (issue says it points at the local file ' +
          args.blocklistFile + '). Confirm it is a LOCAL FILE path, not a URL.',
        '  - RADARR: its blocklist source — the issue claims Radarr still points at the LIVE UPSTREAM URL. Capture that EXACT ' +
          'URL verbatim (this is the precedent for option B and tells us the upstream base path / filename scheme).',
        'FEASIBILITY OF OPTION B — the single most important unknown: determine whether Cleanuparr content-blocker supports, ' +
          'for ONE arr, EITHER (a) a URL source AND a separate local overlay/regex simultaneously, OR (b) only ONE source per arr ' +
          '(URL XOR file). Check the Cleanuparr docs/UI/config schema. If only one source per arr is allowed, option B would DROP ' +
          'the local seriesId ' + args.seriesId + ' regex unless that regex is merged into a self-hosted list — note this explicitly, ' +
          'it materially changes option B feasibility.',
        'CAPTURE THE LIVE SONARR BLOCKLIST FILE verbatim: `kubectl -n ' + args.namespace + ' exec deploy/cleanuparr -- cat ' +
          args.blocklistFile + '` → save byte-for-byte to tasks/' + taskCtx.effectId + '/live-blocklist-sonarr.txt. Record line ' +
          'count + sha256. Identify which line(s) are the LOCAL seriesId ' + args.seriesId + ' regex (the overlay that must survive ' +
          'under every option) vs the upstream-derived entries.',
        'CAPTURE THE COMMITTED SNAPSHOT: decrypt the committed seed to compare — `sops -d ' + args.seedFile + '` ' +
          '(from repoRoot ' + args.repoRoot + '; the age key is the cluster recipient — if local decrypt is unavailable, note that ' +
          'and fall back to comparing the live file only). Extract the snapshot blocklist text, record line count + sha256.',
        'MEASURE DRIFT + CHURN: fetch the CURRENT upstream Sonarr blocklist from ' + args.upstreamRepo + ' (use the same base URL ' +
          'derived from the live Radarr URL, swapping the radarr filename for the sonarr one — e.g. the raw.githubusercontent.com ' +
          'permalink). Diff it against the committed snapshot: how many entries ADDED/REMOVED upstream since the snapshot. Also ' +
          'estimate upstream CHURN: `gh api repos/' + args.upstreamRepo + '/commits?path=<sonarr-blocklist-file>&per_page=30` (or ' +
          'web) to see how often that file actually changes (commits in the last ~6–12 months). Low churn favors B/C-manual; high ' +
          'churn favors automation.',
        'INVENTORY EXISTING DRIFT-REFRESH MACHINERY in the repo so we reuse, not reinvent: is there any existing CronJob pattern ' +
          'under 2-k3s/maintenance/ that opens a PR or writes back to git? Note that image-updater git write-back to main is BROKEN ' +
          '(#192 — required `validate` check rejects bot pushes) and Renovate is the working fallback — this constrains any ' +
          '"open a PR automatically" design for option A. Read 2-k3s/08.servarr/cleanuparr/cleanuparr.yaml, ' +
          '2-k3s/08.servarr/_shared/secrets/ksops-generator.yaml, and the #179 issue body for the analogous newtarr decision state.',
        'Return ONLY structured JSON: access path, sonarrBlocklistPathValue, sonarrSourceType (file|url), radarrUpstreamUrl, ' +
          'optionBSupportsUrlPlusOverlay (boolean|unknown), liveBlocklist {path,lines,sha256}, localRegexLines, ' +
          'snapshotBlocklist {lines,sha256,decryptable}, drift {addedUpstream, removedUpstream, samplesAdded}, ' +
          'upstreamChurn {commitsLast12mo, lastChangedDate}, existingWritebackMachinery, constraints.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['sonarrSourceType', 'liveBlocklist'],
      properties: {
        accessPath: { type: 'string' },
        sonarrBlocklistPathValue: { type: 'string' },
        sonarrSourceType: { type: 'string' },
        radarrUpstreamUrl: { type: 'string' },
        optionBSupportsUrlPlusOverlay: {},
        liveBlocklist: { type: 'object' },
        localRegexLines: { type: 'array' },
        snapshotBlocklist: { type: 'object' },
        drift: { type: 'object' },
        upstreamChurn: { type: 'object' },
        existingWritebackMachinery: { type: 'string' },
        constraints: { type: 'array' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ===========================================================================
// PHASE 2 — Design the options + recommend one (with adversarial review loop)
// ===========================================================================
const planTask = defineTask('plan', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Design options A/B/C, recommend one, write the exact implementation plan for the recommendation',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'GitOps platform engineer designing a minimal, reversible drift-management change',
      task:
        'From the recon facts, lay out the viable options to keep the Cleanuparr Sonarr blocklist current with upstream ' +
        args.upstreamRepo + ', RECOMMEND one with rationale (weighing upstream churn vs stale-snapshot risk vs maintenance cost ' +
        'and feasibility), and write an EXACT, reversible implementation plan for the recommended option.',
      context: { ...args, recon: args.recon, feedback: args.feedback, attempt: args.attempt },
      instructions: [
        'Frame the three candidate options precisely against the recon facts:',
        '  A) PERIODIC RE-SNAPSHOT: a CronJob (or scheduled review) that pulls the upstream Sonarr blocklist, re-merges the local ' +
          'seriesId ' + args.seriesId + ' regex, re-encrypts the cleanuparr-blocklist-seed SOPS Secret, and surfaces the change as ' +
          'a PR (NOT a direct push to main — that is blocked by #192). Mirrors the newtarr re-snapshot decision #179. Heaviest; ' +
          'note the #192 write-back constraint forces either a Renovate-style PR bot or a fail-loud-on-diff alert.',
        '  B) REPOINT TO UPSTREAM URL: change Cleanuparr sonarr_' + 'blocklist_path to the live upstream URL (exactly like Radarr ' +
          'already does), and keep ONLY the local seriesId ' + args.seriesId + ' regex as the codified overlay. Feasible ONLY IF ' +
          'recon.optionBSupportsUrlPlusOverlay is true (URL + local overlay can coexist for one arr). If Cleanuparr allows only ' +
          'one source per arr, B is NOT viable as-is — say so and either drop B or describe the self-hosted-merged-list variant.',
        '  C) DRIFT DETECTOR (hybrid, lowest maintenance): keep the codified snapshot, add a CronJob that only COMPARES upstream vs ' +
          'the committed snapshot and ALERTS LOUDLY (e.g. fires an Alertmanager alert / fails a job) when they diverge beyond a ' +
          'threshold, leaving the actual re-snapshot a manual runbook step. No secret churn, no bot-PR machinery.',
        'Account for the project context: low upstream churn + a single local regex overlay favors B or C; frequent upstream ' +
          'updates favor A. The committed snapshot already survives PVC rebuild (#138), so this issue is purely about FRESHNESS.',
        'RECOMMEND exactly one option (or a clearly-bounded hybrid). Then write the EXACT plan for the recommendation:',
        '  - For A: the CronJob manifest location (2-k3s/maintenance/), the re-snapshot+re-merge+re-encrypt script steps, how it ' +
          'opens a PR or alerts, the SOPS re-encryption flow (.enc.yaml, .sops.yaml age recipient), and the cadence.',
        '  - For B: the live repoint step (one-time UI/DB change — DOCUMENT, do not silently mutate live), shrinking ' +
          args.seedFile + ' to carry ONLY the local regex overlay (re-encrypt), the ksops-generator + initContainer adjustments, ' +
          'and the RECOVERY-newtarr-cleanuparr.md update.',
        '  - For C: the detector CronJob manifest + the alert wiring + the manual re-snapshot runbook section.',
        'Respect the Epaflix merge policy (branch + PR, rebase onto origin/main, force-with-lease, wait `validate`, ' +
          'gh pr merge --merge), the SOPS rule (.enc.yaml, no plaintext kind: Secret), and the media-title scrub rule (refer to ' +
          'the series only as seriesId ' + args.seriesId + ', never a show/release title). NOTE a concurrent branch ' +
          '(scrub-media-titles-servarr-docs) may also touch cleanuparr.yaml / RECOVERY-newtarr-cleanuparr.md — plan to rebase ' +
          'cleanly onto origin/main at ship time.',
        'If feedback is present, revise to address it.',
        'Write the plan to tasks/' + taskCtx.effectId + '/PLAN.md AND return structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['recommendedOption', 'optionsSummary', 'filesToChange'],
      properties: {
        planPath: { type: 'string' },
        recommendedOption: { type: 'string' },
        recommendationRationale: { type: 'string' },
        optionsSummary: { type: 'object' },
        optionBViable: {},
        filesToChange: { type: 'array', items: { type: 'string' } },
        liveStepsRequired: { type: 'array' },
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
  title: 'Adversarially review the options analysis + recommended plan',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Skeptical GitOps reviewer',
      task: 'Find gaps that would make the recommended option fail SOPS/pre-commit/CI, break ArgoCD reconcile, silently drop the ' +
        'local seriesId ' + args.seriesId + ' regex, rely on broken git write-back (#192), or mis-state Cleanuparr URL/overlay ' +
        'capability. Default to NOT-approved if any real gap exists.',
      context: { ...args },
      instructions: [
        'Verify: (1) the recommendation is justified by the recon churn/drift/feasibility facts, not hand-waved; (2) the local ' +
          'seriesId ' + args.seriesId + ' regex overlay is preserved under the recommended option; (3) any SOPS Secret stays ' +
          '.enc.yaml + encrypted (no plaintext kind: Secret); (4) option A does not assume a direct push to main (blocked by #192); ' +
          '(5) option B is only chosen if recon confirms URL + overlay can coexist; (6) scope stays within issue #' + args.issue + ' ' +
          '(no unrelated refactors); (7) live-config repoints are DOCUMENTED, not silently applied by the implement step.',
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
// PHASE 4 — Implement the chosen option (manifests / SOPS / docs)
// ===========================================================================
const implementTask = defineTask('implement', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Implement the owner-chosen drift-management option on a feature branch',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'GitOps platform engineer applying an approved IaC change to the repo working tree',
      task: 'Implement the OWNER-CHOSEN option (' + args.chosenOption + ') for issue #' + args.issue + ' in ' + args.repoRoot +
        ' on a NEW branch off the latest origin/main. Do NOT commit/push yet — leave the diff staged-ready for review at the ' +
        'next breakpoint. Do NOT mutate live Cleanuparr config; live repoints are documented for the operator.',
      context: { ...args, plan: args.plan, recon: args.recon, chosenOption: args.chosenOption, feedback: args.feedback },
      instructions: [
        'Create a feature branch off the latest origin/main (e.g. issue-' + args.issue + '-cleanuparr-blocklist-drift). Do not ' +
          'work on main. A concurrent branch (scrub-media-titles-servarr-docs) may touch the same files — branch from a fresh ' +
          '`git fetch origin` tip and keep the diff minimal so the later rebase is clean.',
        'Implement EXACTLY what the approved plan specifies for the chosen option:',
        '  - OPTION A (re-snapshot): add the CronJob + script under 2-k3s/maintenance/ (or the agreed location), wire any new SOPS ' +
          'Secret as .enc.yaml encrypted via `sops --encrypt --in-place` (creation rule in .sops.yaml), add it to ksops-generator ' +
          'files: list if reconciled by ArgoCD. The job must re-merge the local seriesId ' + args.seriesId + ' regex and surface ' +
          'changes as a PR or a loud alert — NEVER a direct push to main (#192).',
        '  - OPTION B (repoint URL): shrink ' + args.seedFile + ' to carry ONLY the local seriesId ' + args.seriesId + ' regex ' +
          'overlay and re-encrypt with SOPS in place; adjust the seed-config initContainer / ksops wiring as needed; DOCUMENT the ' +
          'one-time live sonarr_blocklist_path repoint (to the upstream URL) as an operator step in RECOVERY-newtarr-cleanuparr.md. ' +
          'Verify the .enc.yaml has no plaintext data/stringData.',
        '  - OPTION C (drift detector): add the detector CronJob manifest + alert wiring (match the existing observability/alerting ' +
          'pattern) and a manual re-snapshot runbook section in RECOVERY-newtarr-cleanuparr.md. No secret churn.',
        'Update 2-k3s/08.servarr/kustomization.yaml header comments and RECOVERY-newtarr-cleanuparr.md to reflect the new ' +
          'drift-management mechanism. Refer to the series only as seriesId ' + args.seriesId + ' — never a release/show title.',
        'If a SOPS file changed, run .github/hooks/check-sops-encrypted.sh locally if available to confirm no plaintext Secret.',
        'Return ONLY JSON: { branch, filesChanged: [...], sopsEncryptedOk, liveStepsDocumented, diffSummary }.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'filesChanged'],
      properties: {
        branch: { type: 'string' },
        filesChanged: { type: 'array', items: { type: 'string' } },
        sopsEncryptedOk: {},
        liveStepsDocumented: { type: 'array' },
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
  title: 'Validate: kustomize build (with helm) of the changed trees',
  shell: {
    command:
      'cd ' + args.repoRoot + ' && (kustomize build --enable-helm 2-k3s/08.servarr >/tmp/servarr-build-182.yaml 2>/tmp/servarr-build-182.err ' +
      '&& kustomize build --enable-helm 2-k3s/maintenance >/tmp/maint-build-182.yaml 2>/tmp/maint-build-182.err ' +
      '&& echo \'{"buildOk": true, "servarrLines": \'$(wc -l </tmp/servarr-build-182.yaml)\', "maintLines": \'$(wc -l </tmp/maint-build-182.yaml)\'}\' ' +
      '|| echo \'{"buildOk": false, "err": "see /tmp/servarr-build-182.err and /tmp/maint-build-182.err"}\')',
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
        ' that closes issue #' + args.issue + ', then rebase onto origin/main, force-with-lease, wait for the required ' +
        '`validate` check, and merge with `gh pr merge --merge`.',
      context: { ...args, impl: args.impl, plan: args.plan, chosenOption: args.chosenOption },
      instructions: [
        'Commit with a clear conventional message (e.g. "feat(cleanuparr): ' + (args.chosenOption || 'drift') +
          ' Sonarr blocklist upstream-drift handling (#' + args.issue + ')"). Do NOT git add -f anything gitignored. Refer to the ' +
          'series only as seriesId ' + args.seriesId + '.',
        'PR body: state the chosen option + rationale, the files changed, any one-time live operator step (the URL repoint, if ' +
          'option B), and cross-link #138 / PR#181 (the codified snapshot), #179 (analogous newtarr re-snapshot), #180 (umbrella). ' +
          'Include a Test plan / Verification checklist (kustomize build, SOPS encrypted if applicable, ArgoCD reconcile, ' +
          'local regex preserved, drift mechanism works).',
        'Rebase the branch onto origin/main and `git push --force-with-lease` (a concurrent scrub branch may have merged — resolve ' +
          'any cleanuparr.yaml / RECOVERY-newtarr-cleanuparr.md overlap by keeping both intents). Wait for the `validate` check ' +
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
  title: 'Post-merge verify, open follow-ups, update + close issue #182',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE verifying a deploy and closing the loop per repo policy',
      task: 'Verify ArgoCD reconciled the affected Application(s) (Synced/Healthy) and the chosen drift mechanism is in place, then ' +
        'close out issue #' + args.issue + ' properly.',
      context: { ...args, pr: args.pr, plan: args.plan, chosenOption: args.chosenOption, impl: args.impl },
      instructions: [
        'Verify per the chosen option: (A) the re-snapshot CronJob exists and its first run/dry-run behaves (or is scheduled); ' +
          '(B) the seed now carries only the regex overlay, the live sonarr_blocklist_path repoint operator step is documented, ' +
          'and Cleanuparr still has the local seriesId ' + args.seriesId + ' regex effective; (C) the drift-detector CronJob exists ' +
          'and alert wiring is valid. In all cases confirm ArgoCD servarr (and maintenance, if touched) App is Synced+Healthy and ' +
          'the local seriesId ' + args.seriesId + ' regex was NOT dropped.',
        'If a one-time live operator step is required (e.g. option B URL repoint), DO NOT silently perform it — surface it clearly ' +
          'in the issue/PR as a documented manual action for the owner (deploy gate already covered the GitOps change).',
        'Open gh issues on ' + args.repo + ' for any deferred items (## Finding / ## Current state / ## Desired outcome / ## Notes ' +
          'shape, cross-link #179 / #180). Skip any already covered by an existing open issue (search first).',
        'Update issue #' + args.issue + ' DESCRIPTION (edit the body — NEVER add a new comment): record the chosen option + ' +
          'rationale, tick the desired-outcome items, strike through what no longer applies with a note, link the PR.',
        'If the desired outcome is satisfied (a concrete, durable drift-management mechanism is chosen + implemented + deployed), ' +
          'close issue #' + args.issue + ' with a short summary comment referencing the PR. Otherwise leave it open and say why.',
        'Refer to the series only as seriesId ' + args.seriesId + '. Return ONLY JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['argoHealthy', 'issueClosed'],
      properties: {
        argoHealthy: { type: 'boolean' },
        regexPreserved: {},
        liveStepsForOwner: { type: 'array' },
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
    issue: inputs.issue || 182,
    namespace: inputs.namespace || 'servarr',
    seriesId: inputs.seriesId || 40,
    blocklistFile: inputs.blocklistFile || '/config/custom-blocklist-sonarr.txt',
    blocklistPathKey: inputs.blocklistPathKey || 'sonarr_blocklist_path',
    seedSecret: inputs.seedSecret || 'cleanuparr-blocklist-seed',
    seedFile: inputs.seedFile || '2-k3s/08.servarr/_shared/secrets/cleanuparr-blocklist-seed.enc.yaml',
    upstreamRepo: inputs.upstreamRepo || 'flmorg/cleanuperr',
    cleanuparrConfigPvc: inputs.cleanuparrConfigPvc || 'cleanuparr-config',
    relatedIssues: inputs.relatedIssues || { codified: 138, codifyPr: 181, newtarrReSnapshot: 179, umbrella: 180 },
  };

  ctx.log('info', `Delivering issue #${a.issue}: Cleanuparr Sonarr blocklist upstream-drift handling (decision + implement)`);

  // PHASE 1 — recon (read-only)
  const recon = await ctx.task(reconTask, a);

  // PHASE 2 — design options + recommend, with adversarial review loop
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

  // BP1 — owner DECIDES the option (the core of issue #182) — architecture-change gate
  let chosenOption = plan.recommendedOption;
  let bp1Feedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (bp1Feedback) {
      plan = await ctx.task(planTask, { ...a, recon, feedback: bp1Feedback, attempt: attempt + 1 });
      chosenOption = plan.recommendedOption;
    }
    const bp1 = await ctx.breakpoint({
      question:
        `Issue #${a.issue} DECISION gate — how should the Cleanuparr Sonarr blocklist stay current with upstream ${a.upstreamRepo}?\n` +
        `Recon: sonarr source = ${recon.sonarrSourceType}; URL+overlay supported = ${recon.optionBSupportsUrlPlusOverlay}; ` +
        `drift vs snapshot = +${recon.drift && recon.drift.addedUpstream}/-${recon.drift && recon.drift.removedUpstream}; ` +
        `upstream churn (12mo commits) = ${recon.upstreamChurn && recon.upstreamChurn.commitsLast12mo}.\n` +
        `Options: A) periodic re-snapshot job (mirrors #179); B) repoint sonarr_blocklist_path to upstream URL + keep local ` +
        `seriesId ${a.seriesId} regex overlay (only if URL+overlay supported); C) drift-detector that alerts loudly + manual ` +
        `re-snapshot (lowest maintenance).\n` +
        `RECOMMENDATION: ${plan.recommendedOption} — ${plan.recommendationRationale}\n` +
        `Reply with the option to implement (A / B / C, or describe a variant). Approve to proceed with the recommendation.`,
      title: `Drift-handling decision — issue #${a.issue}`,
      expert: 'owner',
      tags: ['approval-gate', 'architecture-change'],
      context: {
        runId: ctx.runId,
        files: [{ path: plan.planPath || `tasks/plan/PLAN.md`, format: 'markdown', label: 'Options + recommended plan' }],
        recon, plan,
      },
      previousFeedback: bp1Feedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    if (bp1.approved) {
      // Owner may override the recommendation via response/feedback text.
      const override = (bp1.response || bp1.feedback || '').trim();
      if (override && override.length <= 80) chosenOption = override;
      break;
    }
    bp1Feedback = bp1.response || bp1.feedback || 'Changes requested';
  }

  // PHASE 4 — implement chosen option + validate (build-fix loop)
  let impl = await ctx.task(implementTask, { ...a, plan, recon, chosenOption });
  let build = await ctx.task(kustomizeBuildTask, a);
  for (let i = 0; i < 2 && !(build && build.buildOk); i++) {
    impl = await ctx.task(implementTask, {
      ...a, plan, recon, chosenOption,
      feedback: `kustomize build failed: ${build && build.err}. Fix the manifests on branch ${impl.branch}.`,
    });
    build = await ctx.task(kustomizeBuildTask, a);
  }

  // BP2 — owner reviews exact diff; approves commit + push + PR + merge + DEPLOY (deploy + secrets gate)
  let bp2Feedback = null;
  let approvedToShip = false;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (bp2Feedback) {
      impl = await ctx.task(implementTask, { ...a, plan, recon, chosenOption, feedback: bp2Feedback });
      build = await ctx.task(kustomizeBuildTask, a);
    }
    const bp2 = await ctx.breakpoint({
      question:
        `Ship gate — issue #${a.issue} (option ${chosenOption}).\n` +
        `Branch: ${impl.branch}\nFiles: ${(impl.filesChanged || []).join(', ')}\n` +
        `SOPS encrypted OK: ${impl.sopsEncryptedOk}\nkustomize build: ${build && build.buildOk ? 'OK' : 'FAILED'}\n` +
        `One-time live operator steps: ${(impl.liveStepsDocumented || []).join('; ') || 'none'}\n` +
        `${impl.diffSummary || ''}\n\n` +
        `Approving will: commit, push, open a PR, rebase onto origin/main, wait validate, MERGE, and DEPLOY via ArgoCD selfHeal. Approve?`,
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
      chosenOption, implemented: false,
      metadata: { processId: 'deliver-issue-182-cleanuparr-blocklist-drift', runId: ctx.runId },
    };
  }

  // PHASE 5 — PR + merge
  const pr = await ctx.task(prTask, { ...a, impl, plan, chosenOption });

  // PHASE 6 — post-merge verify + follow-ups + close
  const closeout = await ctx.task(closeoutTask, { ...a, pr, plan, chosenOption, impl });

  ctx.log('info', `Issue #${a.issue} delivery complete. option=${chosenOption} PR=${pr.prUrl} closed=${closeout.issueClosed}`);

  return {
    success: !!(pr.merged && closeout.argoHealthy),
    chosenOption,
    implemented: !!pr.merged,
    prUrl: pr.prUrl,
    issueClosed: !!closeout.issueClosed,
    liveStepsForOwner: closeout.liveStepsForOwner || [],
    followUpIssues: closeout.followUpIssues || [],
    summary: closeout.summary,
    metadata: { processId: 'deliver-issue-182-cleanuparr-blocklist-drift', runId: ctx.runId },
  };
}
