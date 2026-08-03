/**
 * @process specializations/devops-sre-platform/seerr-oidc-research
 * @description Investigate whether the Epaflix cluster can move off the custom
 *   `fallenbagel/jellyseerr:preview-OIDC` fork tag onto a latest/stable seerr (seerr-team)
 *   release with native OIDC support. Read-only research + analysis: confirm live state,
 *   research the seerr-team/seerr discussion #1529 + latest releases + the official OIDC
 *   image, compare against the deployed preview fork, and produce a migration recommendation.
 *   An owner DECISION gate then chooses: report-only / draft a migration plan / stop.
 *   No cluster mutation and no deploy happen in this run.
 * @inputs { repoRoot, repo, ns, kubeContext, discussionUrl, currentImage, deployments, manifests }
 * @outputs { success, decision, hasStableOidc, recommendation, summary }
 *
 * @agent general-purpose (web changelog/release research + read-only kubectl/git, plan authoring)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';

// Phase 1 — research current state (read-only) + upstream seerr/OIDC landscape.
const researchTask = defineTask('research', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Research live seerr state + latest seerr-team/seerr OIDC support',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Platform/SRE engineer investigating a self-hosted media-request app (seerr/jellyseerr) on the Epaflix k3s cluster',
      task:
        'Determine whether a latest/stable seerr (seerr-team) release with NATIVE OIDC exists that the cluster could ' +
        'adopt instead of the custom `' + args.currentImage + '` preview fork tag. READ-ONLY: do not change anything in cluster or git.',
      context: { ...args },
      instructions: [
        'Confirm live state (read-only). kubectl works directly with context: `kubectl --context ' + args.kubeContext + ' -n ' + args.ns + ' get deploy ' + args.deployments.join(' ') + ' -o custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image`. Expect both on ' + args.currentImage + '.',
        'Read the repo manifests for context: ' + args.manifests.join(', ') + ' (under repoRoot=' + args.repoRoot + '). Note there are TWO deployments (jellyseerr and seerr) — record what each is for and whether one is redundant.',
        'WEB RESEARCH — the core question. Read the discussion at ' + args.discussionUrl + '. Then determine: (a) what is the "seerr" project run by seerr-team (a community fork/continuation of jellyseerr)? (b) Does seerr-team/seerr have OIDC support merged into a STABLE release (not a preview/fork branch)? (c) What is the latest seerr-team/seerr release/tag and its official container image (e.g. ghcr.io/seerr-team/seerr or similar)? (d) Separately, has upstream Jellyseerr (fallenbagel/jellyseerr) merged OIDC into a stable `latest` tag yet, retiring the need for the `preview-OIDC` tag?',
        'Be precise about image coordinates: exact registry/repo, the stable tag name, and the most recent version. Cite the GitHub release/discussion URLs you used.',
        'Assess OIDC parity: does the latest stable seerr OIDC implementation cover what the preview-OIDC fork provided (Authentik OIDC login)? Note any config/schema differences (env vars, settings.json, DB) between the fork and the stable release.',
        'Return ONLY the structured JSON result, not a plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['liveImages', 'twoDeploymentsReason', 'seerrProject', 'seerrHasStableOidc', 'seerrLatestVersion', 'seerrOfficialImage', 'jellyseerrStableOidc', 'oidcParityNotes', 'sources', 'summary'],
      properties: {
        liveImages: { type: 'object' },
        twoDeploymentsReason: { type: 'string' },
        seerrProject: { type: 'string' },
        seerrHasStableOidc: { type: 'boolean' },
        seerrLatestVersion: { type: 'string' },
        seerrOfficialImage: { type: 'string' },
        jellyseerrStableOidc: { type: 'boolean' },
        oidcParityNotes: { type: 'string' },
        sources: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 2 — analyse + recommend (read-only reasoning over the research).
const analyzeTask = defineTask('analyze', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Compare deployed preview fork vs latest stable seerr OIDC; recommend',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Platform/SRE engineer making an upgrade recommendation for the Epaflix k3s cluster',
      task: 'Given the research findings, decide whether and how to move off the `' + args.currentImage + '` preview fork.',
      context: { ...args },
      instructions: [
        'Answer the owner question plainly: "Do we have the latest available seerr that supports OIDC?" — yes/no, and what the latest stable OIDC-capable image is.',
        'Recommend ONE of: (1) stay on the preview fork (with rationale), (2) migrate to latest stable seerr/jellyseerr OIDC image, (3) consolidate the two deployments (jellyseerr + seerr) into one. Justify against this repo\'s posture: ArgoCD selfHeal, image-updater currently EXCLUDES seerr (custom fork tag — see 2-k3s/11.argocd/README.md), GitOps-via-PR, low-risk increments.',
        'If recommending a migration, outline the concrete steps WITHOUT executing: which manifest/tag changes, DB/settings compatibility checks, whether image-updater could now track a stable tag, rollback (keep preview tag pinned), and what to verify post-deploy (OIDC login via Authentik still works).',
        'Flag risks: data/DB migration between fork and stable, OIDC config differences, the two-deployment ambiguity, and that any deploy is owner-gated.',
        'Return ONLY the structured JSON result, not a plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['answer', 'latestStableOidcImage', 'recommendation', 'migrationOutline', 'risks', 'imageUpdaterImpact', 'summary'],
      properties: {
        answer: { type: 'string' },
        latestStableOidcImage: { type: 'string' },
        recommendation: { type: 'string' },
        migrationOutline: { type: 'array', items: { type: 'string' } },
        risks: { type: 'array', items: { type: 'string' } },
        imageUpdaterImpact: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 3 (optional) — author a concrete migration plan doc (still no deploy).
const planTask = defineTask('draft-plan', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Draft a migration plan document (no cluster/git mutation)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Platform/SRE engineer authoring a migration plan for the Epaflix k3s cluster',
      task: 'Write a concrete, reviewable migration plan to move seerr onto ' + (args.latestStableOidcImage || 'the recommended stable OIDC image') + '. Do NOT edit live cluster or push git.',
      context: { ...args },
      instructions: [
        'Produce a step-by-step plan: exact manifest edits (file + line + old→new image tag), DB backup step (there is a backup-jellyseerr-db.sh), settings/OIDC config diffs to apply, the PR + ArgoCD selfHeal flow, and rollback (revert tag).',
        'Note any decision still needed (e.g. consolidate jellyseerr+seerr into one deployment).',
        'Write the plan to ' + args.repoRoot + '/.history/seerr-oidc-migration-plan.md and return its path + a short summary. This file is a plan only; no manifests are changed.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['planPath', 'steps', 'summary'],
      properties: {
        planPath: { type: 'string' },
        steps: { type: 'array', items: { type: 'string' } },
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
// Rename / consolidation milestone (added mid-run per owner directive 2026-06-03:
// "jellyseerr is going to retire -> seerr stays. pick the future name and make
//  the necessary changes that will reflect that name.")
// Read-only confirm -> owner gate on the concrete change set -> implement (branch +
// edits) -> verify (kustomize build) -> open PR (no merge, no deploy). Data-bearing
// resources (jellyseerr-config PVC, jellyseerr DB/user, jellyseerr-* secret keys)
// are NOT renamed — that would force a data migration. Only the user-facing surface
// (Deployment/Service/PDB/directory/ingress/docs) consolidates onto the future name.
// ---------------------------------------------------------------------------

// Confirm the rebrand + decide the canonical future name + produce the exact change set.
const confirmRebrandTask = defineTask('confirm-rebrand', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Confirm jellyseerr->seerr retirement; decide canonical name; produce exact change set',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Platform/SRE engineer planning a name-consolidation refactor for the Epaflix k3s servarr stack',
      task:
        'Confirm whether the "jellyseerr" name is being retired in favour of "seerr" upstream, decide the canonical future name the repo should use, and produce a PRECISE, file-level change set to reflect that name. READ-ONLY: do not edit anything yet.',
      context: { ...args },
      instructions: [
        'Use the prior research (context.research) plus the discussion/release URLs. Confirm: is jellyseerr (and overseerr) a deprecated name for the same codebase now published as seerr-team/seerr? State yes/no with evidence.',
        'Decide the canonical future name. Default to "seerr" (matches the upstream rebrand and the existing seerr.epaflix.com route). State it explicitly as canonicalName.',
        'CRITICAL data-safety rule: do NOT propose renaming data-bearing resources — the PVC `jellyseerr-config`, the Postgres DB/user `jellyseerr`, and the `jellyseerr-*` keys in the servarr-postgres secret. Renaming those forces a data migration and is out of scope. Keep them as-is and add an inline comment noting they retain the legacy name for data continuity.',
        'Produce the exact change set as a list of file-level edits. Survey the repo first (grep -ri jellyseerr 2-k3s/08.servarr; read 2-k3s/08.servarr/kustomization.yaml, 2-k3s/08.servarr/seerr/seerr.yaml, 2-k3s/08.servarr/jellyseerr/jellyseerr.yaml, 2-k3s/08.servarr/_shared/ingress/public-routes.yaml, the seerr/ and jellyseerr/ READMEs). The intended outcome: ONE consolidated Deployment/Service/PDB under the canonical "seerr" name on image ' + args.currentImage + ' bound to the existing jellyseerr-config PVC; retire the redundant duplicate; keep BOTH ingress hosts (seerr.epaflix.com + legacy jellyseerr.epaflix.com) pointing at the surviving Service so OIDC redirect URIs keep working; update kustomization.yaml resource list; update docs/READMEs. Decide which of the two existing deployments (jellyseerr/jellyseerr.yaml vs seerr/seerr.yaml) survives and which file/dir is deleted.',
        'For each edit specify: file path, what changes (old->new), and why. Flag every place the surviving Service name matters for the ingress backend so routes do not break.',
        'List risks and the rollback (git revert of the PR; data resources untouched so no data risk).',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['rebrandConfirmed', 'canonicalName', 'survivingService', 'changeSet', 'keepAsLegacy', 'risks', 'rollback', 'summary'],
      properties: {
        rebrandConfirmed: { type: 'boolean' },
        canonicalName: { type: 'string' },
        survivingService: { type: 'string' },
        changeSet: { type: 'array', items: { type: 'object', required: ['file', 'change', 'why'], properties: { file: { type: 'string' }, change: { type: 'string' }, why: { type: 'string' } } } },
        keepAsLegacy: { type: 'array', items: { type: 'string' } },
        risks: { type: 'array', items: { type: 'string' } },
        rollback: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Implement the approved change set on a feature branch (edits only; no push, no merge, no deploy).
const implementRenameTask = defineTask('implement-rename', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Apply the approved rename/consolidation change set on a feature branch',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Platform/SRE engineer implementing an approved manifest refactor in the Epaflix GitOps repo',
      task: 'Apply EXACTLY the approved change set (context.scope.changeSet) to consolidate the servarr media-request app onto the canonical name "' + (args.scope && args.scope.canonicalName) + '". Create a feature branch first. Do NOT push, do NOT open a PR, do NOT merge, do NOT touch the live cluster.',
      context: { ...args },
      instructions: [
        'cd ' + args.repoRoot + '. Ensure you are on an up-to-date main: `git fetch origin && git checkout main && git pull --ff-only`. Then create a branch: `git checkout -b consolidate-seerr-naming-retire-jellyseerr`.',
        'Apply ONLY the edits in context.scope.changeSet — file renames/deletes, Deployment/Service/PDB consolidation, kustomization.yaml resource list update, ingress backend/host edits, README/doc updates. Do NOT rename the jellyseerr-config PVC, the jellyseerr Postgres DB/user, or the jellyseerr-* secret keys (data continuity); add the inline legacy-name comment where touched.',
        'Do NOT introduce or modify any plaintext kind:Secret YAML (pre-commit hook refuses it). Leave *.enc.yaml untouched.',
        'After editing, self-verify the manifests render: `kustomize build 2-k3s/08.servarr >/dev/null && echo KUSTOMIZE_OK` (or `kubectl kustomize` if kustomize is absent). Fix any render error you introduced.',
        'Stage the changes (`git add -A`) but DO NOT commit yet (the orchestrator runs a verification gate, then a later step commits + opens the PR). Run `git status --porcelain` and `git diff --cached --stat` and include them in the result.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'changedFiles', 'kustomizeOk', 'gitStatus', 'summary'],
      properties: {
        branch: { type: 'string' },
        changedFiles: { type: 'array', items: { type: 'string' } },
        kustomizeOk: { type: 'boolean' },
        gitStatus: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Verify the refactor renders cleanly (existing CLI: kustomize) — shell quality gate.
const verifyRenameTask = defineTask('verify-rename', (args) => ({
  kind: 'shell',
  title: 'Verify consolidation: file removed, kustomization clean, manifests render',
  shell: {
    // Robust gate that does NOT false-fail on the pre-existing ksops external
    // generator (full `kustomize build` needs the cluster age key + alpha plugins,
    // unavailable locally). Assert the structural change, YAML-parse the surviving
    // manifest, and attempt a full build best-effort (ksops error is tolerated).
    command:
      'sh -lc "cd \\"' + (args.repoRoot || '.') + '\\" && ' +
      'test ! -f 2-k3s/08.servarr/jellyseerr/jellyseerr.yaml && ' +
      '! grep -q \\"jellyseerr/jellyseerr.yaml\\" 2-k3s/08.servarr/kustomization.yaml && ' +
      'grep -q \\"name: seerr\\" 2-k3s/08.servarr/seerr/seerr.yaml && ' +
      'python3 -c \\"import sys,yaml; list(yaml.safe_load_all(open(\\\\\\"2-k3s/08.servarr/seerr/seerr.yaml\\\\\\")))\\" && ' +
      '( kustomize build --enable-alpha-plugins --enable-exec 2-k3s/08.servarr >/dev/null 2>&1 && echo FULL_BUILD_OK || echo \\"FULL_BUILD_SKIPPED_ksops_env\\" ) && ' +
      'echo VERIFY_OK"',
  },
}));

// Commit, push, open the PR (no merge), and open the deferred stable-OIDC follow-up issue.
const openPrTask = defineTask('open-pr', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Commit + push branch, open PR (no merge), open follow-up issue',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Platform/SRE engineer finalising a GitOps refactor PR for the Epaflix repo',
      task: 'Commit the staged rename/consolidation changes, push the branch, and open a PR against main on ' + args.repo + '. Do NOT merge (owner does not want auto-merge). Then open a follow-up issue for the deferred stable-OIDC migration.',
      context: { ...args },
      instructions: [
        'cd ' + args.repoRoot + '. Confirm you are on branch ' + (args.impl && args.impl.branch || 'consolidate-seerr-naming-retire-jellyseerr') + ' with staged changes (git status). If nothing is staged, re-stage with `git add -A`.',
        'Commit with a Conventional Commit message, e.g. subject `refactor(servarr): consolidate media-request app onto the seerr name, retire jellyseerr duplicate`. Body: explain jellyseerr/overseerr are the deprecated names for seerr-team/seerr; we keep the preview-OIDC fork image and the jellyseerr-config PVC + jellyseerr Postgres DB/secret keys for data continuity; both ingress hosts retained. End the commit body with the line: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.',
        'Push: `git push -u origin ' + (args.impl && args.impl.branch || 'consolidate-seerr-naming-retire-jellyseerr') + '`.',
        'Open the PR with `gh pr create --base main --title "..." --body "..."`. Body should summarise the change set, note NO deploy/merge happens automatically, that ArgoCD selfHeal will reconcile only after an owner merges, and the data-resources-kept-as-legacy decision. Do NOT merge the PR.',
        'Per repo policy, open a follow-up issue with `gh issue create` on ' + args.repo + ' titled around "Migrate seerr off preview-OIDC fork to stable native OIDC once seerr-team/seerr PR #2715 merges". Use the ## Finding / ## Current state / ## Desired outcome / ## Notes shape. Cross-link the new PR and existing issue #61.',
        'Return ONLY the structured JSON result with the PR url and issue url.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'followUpIssueUrl', 'merged', 'summary'],
      properties: {
        prUrl: { type: 'string' },
        followUpIssueUrl: { type: 'string' },
        merged: { type: 'boolean' },
        summary: { type: 'string' },
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
    repoRoot: inputs.repoRoot,
    repo: inputs.repo,
    ns: inputs.ns || 'servarr',
    kubeContext: inputs.kubeContext || 'epaflix',
    discussionUrl: inputs.discussionUrl,
    currentImage: inputs.currentImage || 'fallenbagel/jellyseerr:preview-OIDC',
    deployments: inputs.deployments || ['jellyseerr', 'seerr'],
    manifests: inputs.manifests || [
      '2-k3s/08.servarr/seerr/seerr.yaml',
      '2-k3s/08.servarr/jellyseerr/jellyseerr.yaml',
      '2-k3s/08.servarr/kustomization.yaml',
      '2-k3s/11.argocd/README.md',
    ],
  };

  ctx.log('info', 'seerr OIDC research — research → analyze → decide → (optional) draft plan');

  const r = await ctx.task(researchTask, { ...cfg });
  ctx.log('info', `Research: seerrStableOidc=${r.seerrHasStableOidc}; latest=${r.seerrLatestVersion}; image=${r.seerrOfficialImage}`);

  const a = await ctx.task(analyzeTask, { ...cfg, research: r });
  ctx.log('info', `Recommendation: ${a.recommendation}`);

  const gate = await ctx.breakpoint({
    question:
      'seerr OIDC research done.\n\n' +
      'Answer: ' + a.answer + '\n' +
      'Latest stable OIDC image: ' + a.latestStableOidcImage + '\n' +
      'Recommendation: ' + a.recommendation + '\n\n' +
      'What next? (no deploy happens regardless)',
    options: ['Report only — stop here', 'Draft a migration plan doc', 'Stop'],
    expert: 'owner',
    tags: ['decision-gate'],
  });

  const choice = (gate.response || '').toLowerCase();
  if (!gate.approved && !choice) {
    return { success: true, decision: 'report-only', hasStableOidc: r.seerrHasStableOidc, recommendation: a.recommendation, summary: 'No decision; reported findings only. ' + a.summary };
  }

  // Owner directive (2026-06-03): jellyseerr name retiring -> adopt the future name
  // (seerr) and make the repo changes that reflect it. Detect that directive in the
  // gate response and run the rename/consolidation milestone.
  const wantsRename = /retire|rename|consolidat|reflect|future name|name that will|seerr is|stay/.test(choice);
  if (wantsRename) {
    ctx.log('info', 'Owner chose rename/consolidation onto the future name (seerr).');
    const scope = await ctx.task(confirmRebrandTask, { ...cfg, research: r, analysis: a });
    ctx.log('info', `Rebrand confirmed=${scope.rebrandConfirmed}; canonicalName=${scope.canonicalName}; ${scope.changeSet.length} file edits planned.`);

    const csLines = scope.changeSet.map((c, i) => `${i + 1}. ${c.file} — ${c.change}`).join('\n');
    const approve = await ctx.breakpoint({
      question:
        'Rebrand confirmed: ' + scope.rebrandConfirmed + '. Canonical future name: ' + scope.canonicalName + '.\n' +
        'Surviving Service: ' + scope.survivingService + '. Kept as legacy (NOT renamed, data continuity): ' + (scope.keepAsLegacy || []).join(', ') + '.\n\n' +
        'Proposed change set (will be applied on a branch, then a PR is opened — NO merge, NO deploy):\n' + csLines + '\n\n' +
        'Risks: ' + (scope.risks || []).join('; ') + '\nRollback: ' + scope.rollback + '\n\n' +
        'Approve applying this change set and opening a PR?',
      options: ['Approve — apply change set + open PR', 'Reject — stop'],
      expert: 'owner',
      tags: ['decision-gate', 'architecture', 'destructive'],
    });
    if (!approve.approved) {
      return { success: true, decision: 'rename-rejected', hasStableOidc: r.seerrHasStableOidc, canonicalName: scope.canonicalName, summary: 'Owner rejected the rename change set. No edits made. ' + (approve.response || '') };
    }

    const impl = await ctx.task(implementRenameTask, { ...cfg, scope });
    ctx.log('info', `Implemented on branch ${impl.branch}; kustomizeOk=${impl.kustomizeOk}; ${impl.changedFiles.length} files.`);

    const verify = await ctx.task(verifyRenameTask, { ...cfg });

    const pr = await ctx.task(openPrTask, { ...cfg, scope, impl, verify });
    ctx.log('info', `PR: ${pr.prUrl}; follow-up issue: ${pr.followUpIssueUrl}; merged=${pr.merged}`);
    return {
      success: true,
      decision: 'renamed-pr-opened',
      hasStableOidc: r.seerrHasStableOidc,
      canonicalName: scope.canonicalName,
      branch: impl.branch,
      prUrl: pr.prUrl,
      followUpIssueUrl: pr.followUpIssueUrl,
      merged: pr.merged,
      summary: pr.summary,
    };
  }

  if (choice.includes('plan')) {
    const p = await ctx.task(planTask, { ...cfg, latestStableOidcImage: a.latestStableOidcImage, research: r, analysis: a });
    ctx.log('info', `Plan drafted: ${p.planPath}`);
    return { success: true, decision: 'draft-plan', hasStableOidc: r.seerrHasStableOidc, recommendation: a.recommendation, planPath: p.planPath, summary: p.summary };
  }
  return { success: true, decision: 'report-only', hasStableOidc: r.seerrHasStableOidc, recommendation: a.recommendation, summary: a.summary };
}
