/**
 * @process specializations/devops-sre-platform/issue142-orphan-reaping-deliver
 * @description Deliver the OPEN forward-looking deliverables of issue #142 (the 2026-05-31
 *   orphan firefight + runbook were already shipped in PR #144). Remaining scope:
 *   (#2) DECIDE + implement a SAFE automated orphan-reaping mechanism — option (a) co-locate
 *        downloads+library on one ZFS dataset AND mount library roots into Cleanuparr so
 *        nlink>1 detection is real, OR option (b) a tag/category-scoped or seed-time/ratio
 *        guarded reaping rule that cannot touch the ~139 healthy nlink=1 seeders, OR an
 *        explicit documented deferral. The DownloadCleaner unlinked/orphan rule MUST stay OFF
 *        until (a) or (b) is in place — in the current separate-dataset / no-library-mount /
 *        copyUsingHardlinks=false topology it would delete ALL ~139 seeders.
 *   (#3) SOAK-CONFIRM the seriesId 272 re-search eventually grabbed a seeded release
 *        (was 0-seed "no healthy release available" on 2026-05-31 — separate from the orphan bug).
 *   (#4) DECIDE on bumping newtarr hunt_missing_items above 1 (note: #135 — hunt settings are
 *        GLOBAL and the owner restored them live; a bump can worsen the add-search race).
 *   Close the loop per repo policy: update issue #142 description with outcomes (tick / strike),
 *   open follow-ups, cross-link #138 (PVC-only Cleanuparr config durability).
 *
 *   Live-change risk: any Cleanuparr config / manifest change is GATED behind a deploy breakpoint;
 *   no torrent is deleted in this run. Git/PR/merge gated separately. Read-only investigation first.
 *
 * @inputs { repoRoot, namespace, repo, sonarrUrl, cleanuparrUrl, seriesId }
 * @outputs { success, chosenOption, implemented, soak272, newtarrDecision, prUrl, issueUpdated, summary }
 *
 * @agent general-purpose kubectl/qbt-api/curl/git/gh executor + design/verification
 * @skill systematic-debugging superpowers:systematic-debugging
 * @skill verification-before-completion superpowers:verification-before-completion
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

// PHASE 1 — investigate live state (read-only)
const investigateTask = defineTask('investigate', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Investigate live state: orphan recurrence, seriesId 272 soak, Cleanuparr/qbt config, newtarr hunt (read-only)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr/Kubernetes SRE on the Epaflix k3s cluster, delivering OPEN issue #142',
      task:
        'Gather the live evidence needed to deliver the three OPEN deliverables of issue #142. ' +
        'The 2026-05-31 firefight (orphan removal + runbook) is DONE (PR #144). DO NOT change anything — read-only.',
      context: { ...args },
      instructions: [
        'Apply systematic-debugging discipline: evidence first, no speculation.',
        'Read issue #142 body for the exact deliverables and the KEY SAFETY FINDING: in the current separate ZFS dataset ' +
          '(downloads vs tvshows/animes/movies) + no-library-mount + Sonarr2 copyUsingHardlinks=false topology, every imported seeder is ' +
          'nlink=1 and indistinguishable from a true orphan, so the DownloadCleaner unlinked/orphan rule would delete ALL ~139 seeders.',
        'A) ORPHAN RECURRENCE: via the qbt WebUI API (creds in deploy/cleanuparr cleanuparr.db download_clients table or /config/qBittorrent/qBittorrent.conf), ' +
          'list every torrent in stalledDL/metaDL/missingFiles with 0/near-0 seeds, and cross-match each hash against every arr /api/v3/queue (sonarr, sonarr2, radarr). ' +
          'Report whether any NEW manual-remove orphans have appeared since 2026-05-31. Count current healthy seeders too (sanity-check the ~139).',
        'B) seriesId 272 SOAK (#3): in Sonarr (main) at ' + args.sonarrUrl + ' (X-Api-Key from `kubectl -n ' + args.namespace +
          ' exec deploy/sonarr -- cat /config/config.xml`), inspect seriesId ' + args.seriesId + ': how many episodes are still missing+monitored vs now hasFile=true. ' +
          'Check /api/v3/queue + /api/v3/history for whether the post-2026-05-31 EpisodeSearch grabs are now SEEDED (progressing/imported) or still 0-seed. ' +
          'Classify: soakPassed (a seeded release was grabbed/imported), soakPending (still 0-seed/no healthy release), or soakNA.',
        'C) CLEANUPARR + qbt CONFIG (#2): Cleanuparr config = SQLite /config/cleanuparr.db in deploy/cleanuparr (no sqlite3 CLI — use python3 in-pod). ' +
          'Dump download_cleaner_configs / unlinked_configs / any orphan rule and CONFIRM the unlinked/orphan rule is still DISABLED. ' +
          'Record what guard knobs exist in this Cleanuparr version: seeding/ratio guards, seed-time minimums, per-category include/exclude. ' +
          'Enumerate qbt CATEGORIES and which arr uses which category/tag (so we can judge feasibility of a category-scoped option-b rule). ' +
          'Confirm from the manifest (2-k3s/08.servarr/cleanuparr/cleanuparr.yaml) and live pod mounts that ONLY servarr-media-downloads is mounted at /data and the ' +
          'library roots (tvshows/animes/movies) are NOT mounted.',
        'D) DATASET TOPOLOGY (#2 option a feasibility): via `ssh truenas_admin@192.168.10.200` (zfs list) confirm downloads and library are separate datasets ' +
          'under pool1/dataset01, and report what option (a) would require (co-locate or same-dataset + mount library roots into Cleanuparr).',
        'E) newtarr (#4): read hunt_missing_items current value (newtarr v1.0.0 JSON config under newtarr-config PVC /config sonarr.json + scheduling; #135 says GLOBAL, owner restored live). ' +
          'Note the known add-search race (#135) so a bump can be judged.',
        'Return ONLY the structured JSON result. Save raw captures under tasks/' + taskCtx.effectId + '/ if useful.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['orphanRecurrence', 'seriesId272', 'cleanuparrConfig', 'datasetTopology', 'newtarr', 'summary'],
      properties: {
        orphanRecurrence: { type: 'object' },
        seriesId272: { type: 'object' },
        cleanuparrConfig: { type: 'object' },
        datasetTopology: { type: 'object' },
        newtarr: { type: 'object' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 2 — design safe orphan-reaping options (authoring only, read-only on cluster)
const designTask = defineTask('design', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Design SAFE orphan-reaping options (a/b/defer) with concrete config + recommendation',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Senior Servarr/Storage SRE designing a SAFE automated orphan-reaping mechanism for issue #142 deliverable 2',
      task:
        'Using the live investigation findings, produce a concrete decision design for safe automated orphan reaping. ' +
        'Authoring only — do NOT change cluster state or git.',
      context: { ...args },
      instructions: [
        'Evaluate THREE candidate paths and give a clear recommendation:',
        ' OPTION A — co-locate downloads + library on ONE ZFS dataset (so hardlinks span and seeders become nlink>1) AND mount library roots ' +
          '(/tv,/animes,/movies) into the Cleanuparr deploy, then enable the unlinked/orphan rule. Detail the storage migration cost/risk (this is a ' +
          'large, destructive infra change touching media datasets) and the manifest mount change. Be honest that this is heavyweight.',
        ' OPTION B — a guarded reaping rule that CANNOT touch the ~139 nlink=1 library seeders: e.g. a seed-time / ratio / stalled-age guarded ' +
          'DownloadCleaner rule scoped to a download category, or a QueueCleaner-style rule that only targets genuinely abandoned 0-seed stalled ' +
          'downloads. Give the EXACT Cleanuparr knobs/values for the installed version (from findings.cleanuparrConfig). Prove why it cannot delete a ' +
          'healthy completed seeder (which is stalledUP/seeding, not stalledDL with 0 seeds).',
        ' OPTION C — explicit documented DEFERRAL: keep the unlinked rule OFF, rely on the operator runbook (PR #144) + the manual-remove discipline, ' +
          'and record exactly what trigger/condition would justify revisiting (a) or (b). Lowest risk.',
        'For the RECOMMENDED option, specify: (1) exact file/manifest/config edits, (2) the DURABILITY story — Cleanuparr config is SQLite on a ' +
          'PVC-only local-path (issue #138), so any DB-level rule is NOT in git; state what is codifiable (manifest mounts, seed files) vs what is a ' +
          'documented manual UI/DB step, and cross-link #138, (3) a rollback, (4) what to verify post-change.',
        'Also draft the #4 newtarr recommendation: should hunt_missing_items be bumped above 1? Account for #135 (GLOBAL setting, owner restored live, ' +
          'add-search race). Default to NO bump unless the seriesId 272 backlog evidence clearly justifies it; explain.',
        'Return ONLY the structured JSON result. Write the human-readable design to tasks/' + taskCtx.effectId + '/design.md.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['recommendedOption', 'optionA', 'optionB', 'optionC', 'recommendedChanges', 'durability', 'rollback', 'newtarrRecommendation', 'summary'],
      properties: {
        recommendedOption: { type: 'string' },
        optionA: { type: 'object' },
        optionB: { type: 'object' },
        optionC: { type: 'object' },
        recommendedChanges: { type: 'array', items: { type: 'object' } },
        durability: { type: 'string' },
        rollback: { type: 'string' },
        newtarrRecommendation: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 3 — adversarial verify of the recommended design (read-only)
const verifyDesignTask = defineTask('verify-design', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Adversarially verify the recommended orphan-reaping design cannot delete healthy seeders',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Skeptical SRE reviewer guarding the ~139 healthy nlink=1 library seeders',
      task:
        'Try to prove the recommended design is UNSAFE or wrong. Default to rejecting any rule that could delete a healthy seeder. ' +
        'Read-only; verify against the live findings.',
      context: { ...args },
      instructions: [
        'Construct concrete failure scenarios: could the recommended rule ever match a completed library seeder (nlink=1, stalledUP/seeding, healthy ratio)? ' +
          'Could it match an actively downloading wanted item? Could a category/tag be missing so the scope leaks? If ANY plausible path deletes wanted data, mark safe=false.',
        'If OPTION A: confirm the migration is correctly scoped and that mounting library roots actually makes nlink>1 true for future imports; flag the one-time backfill gap (existing seeders stay nlink=1 until re-imported).',
        'If OPTION B: re-derive the exact knob values are tight (0-seed AND stalledDL AND min stalled-age, category-scoped, seeding/ratio guard ON) and confirm they exist in the installed Cleanuparr version per findings.',
        'If OPTION C: confirm deferral is internally consistent and the runbook already covers the operator discipline.',
        'Confirm the durability claim (what is git-codifiable vs manual DB step) is accurate and #138 is cross-linked.',
        'Return ONLY the structured JSON result with safe (boolean), issues (array), and a sharpened finalChanges array.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['safe', 'issues', 'finalChanges', 'summary'],
      properties: {
        safe: { type: 'boolean' },
        issues: { type: 'array', items: { type: 'string' } },
        finalChanges: { type: 'array', items: { type: 'object' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 4 — implement approved changes in git (branch only; NO merge, NO live mutation)
const implementTask = defineTask('implement', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Author approved changes on a feature branch (manifests/docs/SOPS); no merge, no live mutation',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr IaC engineer implementing the OWNER-APPROVED orphan-reaping decision for issue #142',
      task:
        'Implement EXACTLY the approved option and changes on a NEW git branch off origin/main. Author files only — do NOT merge and do NOT ' +
        'change any live cluster/Cleanuparr state in this task (live changes happen later behind the deploy gate).',
      context: { ...args },
      instructions: [
        'Branch off origin/main with a descriptive name (e.g. issue-142-orphan-reaping).',
        'Apply the approved finalChanges: this always includes a RECOVERY-newtarr-cleanuparr.md / runbook update recording the chosen option and the ' +
          'safe-reaping decision; plus, if the approved option requires it, manifest edits (e.g. Cleanuparr library-root mounts for option A) and/or a ' +
          'codified seed/config artifact. Keep secrets encrypted — any kind: Secret YAML must be SOPS *.enc.yaml (the pre-commit hook refuses plaintext).',
        'If the approved option keeps a live-only Cleanuparr DB rule (option B PVC-only), DO NOT apply it live here — instead document the exact manual ' +
          'UI/DB step in the runbook and cross-link #138. The live application (if any) is gated separately.',
        'Conventional commit message; end the body with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. ' +
          'Do NOT stage secrets, .history/*, or .a5c/* files (all gitignored). Commit, but DO NOT push or open a PR yet.',
        'Return ONLY the structured JSON result with branch, filesChanged, commitSha, liveStepDeferred (string|null).',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'filesChanged', 'commitSha', 'liveStepDeferred', 'summary'],
      properties: {
        branch: { type: 'string' },
        filesChanged: { type: 'array', items: { type: 'string' } },
        commitSha: { type: 'string' },
        liveStepDeferred: { type: ['string', 'null'] },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 5 — validate the branch (shell: kustomize build + pre-commit hooks)
const validateTask = defineTask('validate', (args, taskCtx) => ({
  kind: 'shell',
  title: 'Validate branch: kustomize build servarr + SOPS pre-commit hook',
  shell: {
    command:
      'set -o pipefail; cd ' + args.repoRoot + ' && ' +
      'echo "=== git status ===" && git status --short && ' +
      'echo "=== kustomize build 08.servarr ===" && ' +
      '(kubectl kustomize 2-k3s/08.servarr >/tmp/issue142-kustomize.yaml && echo "kustomize OK ($(wc -l </tmp/issue142-kustomize.yaml) lines)" || echo "KUSTOMIZE_FAILED") && ' +
      'echo "=== SOPS plaintext-secret guard ===" && ' +
      '(test -x .github/hooks/check-sops-encrypted.sh && .github/hooks/check-sops-encrypted.sh || echo "hook-not-run") ',
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 6 — deliver: PR + merge + (optional approved live change) + soak-confirm + update issue #142 + follow-ups
const deliverTask = defineTask('deliver', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Push+PR+merge approved branch, apply approved live change, soak-confirm 272, update issue #142, open follow-ups',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Servarr SRE closing out issue #142 per the Epaflix repo policy',
      task: 'Land the approved branch via the repo merge policy, apply any owner-approved live change, soak-confirm seriesId 272, and update issue #142.',
      context: { ...args },
      instructions: [
        'Push the branch and open a PR. Body must end with the "🤖 Generated with [Claude Code](https://claude.com/claude-code)" line. ' +
          'Include a Test plan checklist (kustomize build, ArgoCD Synced/Healthy, unlinked rule still OFF unless option A landed, seriesId 272 soak).',
        'Follow the merge policy EXACTLY: rebase the branch onto origin/main, `git push --force-with-lease`, wait for the required `validate` check to pass ' +
          '(`gh pr checks` — if it flakes on the kustomize rate-limit, `gh run rerun --failed`), then `gh pr merge <n> --merge`. If validate/merge cannot ' +
          'complete cleanly, leave the PR open and report it (do not force).',
        'If the owner approved a LIVE Cleanuparr config change at the deploy gate (ownerApprovedLiveChange=' + String(args.ownerApprovedLiveChange) + '), ' +
          'apply EXACTLY that change in deploy/cleanuparr (backup cleanuparr.db first; rollout restart if needed; preserve seeding/ratio guard). Otherwise apply NO live change. ' +
          'NEVER enable the blanket unlinked/orphan rule unless option A (library roots mounted) actually landed.',
        'After merge, confirm ArgoCD reconciles the servarr Application to Synced/Healthy (argocd app get servarr, or kubectl rollouts) for the changed manifests.',
        'SOAK-CONFIRM seriesId ' + args.seriesId + ' (#3): re-check Sonarr now — has a SEEDED release been grabbed/imported, or is it still 0-seed? Record the outcome verbatim. ' +
          'If still 0-seed, mark that deliverable as a documented "no healthy release available" external condition (NOT the orphan bug) and strike it through with that note.',
        'UPDATE issue #142 DESCRIPTION (edit the body, do NOT add a new comment): tick the delivered Desired-outcome items, strike-through any N/A with a reason, ' +
          'and record the chosen orphan-reaping option + the seriesId 272 soak outcome + the newtarr #4 decision inline.',
        'Open gh follow-up issue(s) on ' + args.repo + ' (enhancement shape: ## Finding / ## Current state / ## Desired outcome / ## Notes) for any deferred work — ' +
          'e.g. the PVC-only Cleanuparr config durability (cross-link #138), a future option-A storage migration if deferred, or a 272-soak recheck if still pending. Cross-link #142.',
        'Do NOT close #142 yourself unless every Desired-outcome item is delivered or struck; otherwise leave it open with the updated checklist and report what remains.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'merged', 'liveChangeApplied', 'soak272', 'issueUpdated', 'followUps', 'remaining', 'summary'],
      properties: {
        prUrl: { type: 'string' },
        merged: { type: 'boolean' },
        liveChangeApplied: { type: 'boolean' },
        soak272: { type: 'string' },
        issueUpdated: { type: 'boolean' },
        followUps: { type: 'array', items: { type: 'string' } },
        remaining: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: { inputJsonPath: `tasks/${taskCtx.effectId}/input.json`, outputJsonPath: `tasks/${taskCtx.effectId}/output.json` },
}));

// PHASE 7 — final verification (read-only)
const finalVerifyTask = defineTask('final-verify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Final verification: PR landed/open, ArgoCD healthy, issue #142 updated, no seeder loss',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE verifying the issue #142 delivery actually took effect',
      task: 'Confirm the delivery is real and safe. Read-only.',
      context: { ...args },
      instructions: [
        'Confirm the PR state matches what deliver reported (merged or open-with-reason) via `gh pr view`.',
        'If a manifest change landed: confirm ArgoCD servarr Application is Synced/Healthy and the Cleanuparr pod is Ready.',
        'Confirm the DownloadCleaner unlinked/orphan rule is OFF unless option A landed (re-read cleanuparr.db) and that healthy seeder count is unchanged (no seeder was deleted).',
        'Confirm issue #142 description was edited (boxes ticked / struck, decisions recorded) and follow-ups were opened.',
        'Set delivered=true only if the approved scope is actually in place and verifiable. List anything still open.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['delivered', 'evidence', 'stillOpen', 'summary'],
      properties: {
        delivered: { type: 'boolean' },
        evidence: { type: 'array', items: { type: 'string' } },
        stillOpen: { type: 'array', items: { type: 'string' } },
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
    repo: 'SpyrosPsarras/epaflix',
    sonarrUrl: 'https://sonarr.epaflix.com',
    cleanuparrUrl: 'https://cleanuparr.epaflix.com',
    seriesId: 272,
    ...inputs,
  };

  ctx.log('info', `Deliver issue #142 forward deliverables (orphan reaping + seriesId ${cfg.seriesId} soak + newtarr).`);

  // PHASE 1 — investigate (read-only)
  const inv = await ctx.task(investigateTask, {
    repoRoot: cfg.repoRoot, namespace: cfg.namespace, sonarrUrl: cfg.sonarrUrl,
    cleanuparrUrl: cfg.cleanuparrUrl, seriesId: cfg.seriesId,
  });
  ctx.log('info', `Investigated: ${inv.summary}`);

  // PHASE 2 + 3 — design + adversarial verify, with a refine loop (max 3)
  let design = await ctx.task(designTask, { findings: inv });
  let vd = await ctx.task(verifyDesignTask, { findings: inv, design });
  for (let attempt = 1; attempt < 3 && !vd.safe; attempt++) {
    ctx.log('warn', `Design not safe (attempt ${attempt}): ${JSON.stringify(vd.issues)} — refining.`);
    design = await ctx.task(designTask, { findings: inv, previousDesign: design, reviewerIssues: vd.issues, attempt: attempt + 1 });
    vd = await ctx.task(verifyDesignTask, { findings: inv, design, attempt: attempt + 1 });
  }
  ctx.log('info', `Design recommended=${design.recommendedOption}; verifierSafe=${vd.safe}`);

  // GATE 1 (architecture decision) — owner picks the option + approves implementation scope.
  let lastFeedback = null;
  let gate1 = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (lastFeedback) {
      design = await ctx.task(designTask, { findings: inv, previousDesign: design, ownerFeedback: lastFeedback, attempt: attempt + 1 });
      vd = await ctx.task(verifyDesignTask, { findings: inv, design, attempt: attempt + 1 });
    }
    gate1 = await ctx.breakpoint({
      question:
        'Issue #142 — choose the SAFE orphan-reaping path (deliverable #2).\n\n' +
        'Recommended: ' + design.recommendedOption + ' (verifier safe=' + vd.safe + ')\n\n' +
        'A (heavyweight, destructive storage migration): ' + JSON.stringify(design.optionA) + '\n\n' +
        'B (guarded reaping rule, no library-mount): ' + JSON.stringify(design.optionB) + '\n\n' +
        'C (documented deferral, unlinked rule stays OFF): ' + JSON.stringify(design.optionC) + '\n\n' +
        'Durability: ' + design.durability + '\n' +
        'newtarr #4 recommendation: ' + design.newtarrRecommendation + '\n\n' +
        'seriesId ' + cfg.seriesId + ' soak status (from investigation): ' + JSON.stringify(inv.seriesId272) + '\n\n' +
        'Reply with which option to implement (A / B / C), or request changes. The unlinked rule will NOT be enabled unless option A lands.',
      title: 'Issue #142 orphan-reaping decision',
      options: ['Option A', 'Option B', 'Option C (defer)', 'Request changes', 'Abort'],
      context: {
        runId: ctx.runId,
        files: [{ path: `design-attempt-${attempt + 1}.json`, format: 'json', content: JSON.stringify({ design, verify: vd }, null, 2) }],
      },
      expert: 'owner',
      tags: ['architecture', 'approval-gate', 'servarr'],
      previousFeedback: lastFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    const r1 = (gate1.response || '').toLowerCase();
    if (gate1.approved && !r1.includes('request changes')) break;
    if (!gate1.approved && r1.includes('abort')) {
      ctx.log('warn', 'Decision aborted by owner — stopping after read-only design.');
      return { success: false, reason: 'decision-aborted', chosenOption: null, implemented: false, design, verify: vd, investigation: inv };
    }
    lastFeedback = gate1.response || gate1.feedback || 'Changes requested';
  }

  const resp1 = (gate1.response || '').toLowerCase();
  if (resp1.includes('abort')) {
    return { success: false, reason: 'decision-aborted', chosenOption: null, implemented: false, design, verify: vd, investigation: inv };
  }
  const chosenOption =
    resp1.includes('option a') ? 'A' :
    resp1.includes('option b') ? 'B' :
    resp1.includes('option c') || resp1.includes('defer') ? 'C' :
    (design.recommendedOption || 'C');
  ctx.log('info', `Owner chose option ${chosenOption}.`);

  // PHASE 4 — implement on a branch (no merge, no live mutation)
  const impl = await ctx.task(implementTask, {
    repoRoot: cfg.repoRoot, chosenOption, finalChanges: vd.finalChanges, design,
    ownerFeedback: gate1.response || '',
  });
  ctx.log('info', `Implemented branch=${impl.branch}; files=${(impl.filesChanged || []).length}; liveDeferred=${impl.liveStepDeferred}`);

  // PHASE 5 — validate (shell)
  const val = await ctx.task(validateTask, { repoRoot: cfg.repoRoot });
  ctx.log('info', 'Validation shell completed.');

  // GATE 2 (deploy / outward-facing git) — approve PR+merge and any approved live change.
  const gate2 = await ctx.breakpoint({
    question:
      'Issue #142 — approve DELIVERY of option ' + chosenOption + '?\n\n' +
      'Branch: ' + impl.branch + '\nFiles changed: ' + JSON.stringify(impl.filesChanged) + '\n' +
      'Deferred live step: ' + (impl.liveStepDeferred || 'none') + '\n\n' +
      'Validation output (kustomize build + SOPS guard):\n' + JSON.stringify((val && (val.stdout || val.output)) || val, null, 2).slice(0, 2000) + '\n\n' +
      'Approving will: push the branch, open a PR, rebase + force-with-lease, wait for `validate`, then `gh pr merge --merge` (ArgoCD then reconciles). ' +
      (impl.liveStepDeferred ? 'It will ALSO apply the deferred live Cleanuparr change shown above. ' : '') +
      'The DownloadCleaner unlinked/orphan rule will NOT be enabled unless option A landed. Proceed?',
    title: 'Issue #142 deploy gate (PR merge + optional live change)',
    options: ['Approve delivery', 'Approve PR only (no live change)', 'Request changes', 'Abort'],
    expert: 'owner',
    tags: ['deploy', 'destructive-git', 'outward-facing', 'approval-gate'],
  });
  const r2 = (gate2.response || '').toLowerCase();
  if (!gate2.approved || r2.includes('abort')) {
    ctx.log('warn', 'Delivery not approved — branch left local, nothing pushed.');
    return {
      success: false, reason: 'delivery-not-approved', chosenOption, implemented: true, prUrl: null,
      branch: impl.branch, design, verify: vd, investigation: inv, feedback: gate2.response || '',
    };
  }
  const ownerApprovedLiveChange = !!impl.liveStepDeferred && !r2.includes('pr only') && !r2.includes('no live');

  // PHASE 6 — deliver (push/PR/merge + optional live change + soak + update issue + follow-ups)
  const deliver = await ctx.task(deliverTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo, namespace: cfg.namespace, seriesId: cfg.seriesId,
    branch: impl.branch, chosenOption, design, ownerApprovedLiveChange, liveStepDeferred: impl.liveStepDeferred,
  });
  ctx.log('info', `Delivered: pr=${deliver.prUrl}; merged=${deliver.merged}; soak272=${deliver.soak272}; issueUpdated=${deliver.issueUpdated}`);

  // PHASE 7 — final verify (read-only)
  const fin = await ctx.task(finalVerifyTask, {
    namespace: cfg.namespace, repo: cfg.repo, seriesId: cfg.seriesId, chosenOption,
    prUrl: deliver.prUrl, expectMerged: deliver.merged, liveChangeApplied: deliver.liveChangeApplied,
  });
  ctx.log('info', `Final verify delivered=${fin.delivered}; stillOpen=${JSON.stringify(fin.stillOpen)}`);

  return {
    success: !!fin.delivered,
    chosenOption,
    implemented: true,
    prUrl: deliver.prUrl,
    merged: deliver.merged,
    liveChangeApplied: deliver.liveChangeApplied,
    soak272: deliver.soak272,
    newtarrDecision: design.newtarrRecommendation,
    issueUpdated: deliver.issueUpdated,
    followUps: deliver.followUps,
    stillOpen: fin.stillOpen,
    summary: fin.summary,
  };
}
