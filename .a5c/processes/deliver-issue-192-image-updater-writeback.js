/**
 * @process project/deliver-issue-192-image-updater-writeback
 * @description Issue #192 — ArgoCD Image Updater's `write-back-method: git` (direct push to `main`) is
 * rejected on every poll by branch protection (required `validate` check, ruleset 16805247), so digest
 * automation is silently broken for the `servarr` Application (and the `authentik` Application carries the
 * identical latent breakage). Renovate's independently-opened digest PRs became the de-facto delivery path,
 * but they are NOT automerged (digest updates are classified `digest`, not `patch`, so the existing
 * patch-automerge rule misses them) — they pile up and need manual rebase+merge (5 such PRs shepherded
 * 2026-06-07: #150/#156/#151/#162/#190).
 *
 * Owner goal (stated this run): ALL apps auto-update end-to-end with NO manual branch rebasing.
 * Chosen path = issue Option 3 (consolidate on Renovate), delivered as a reversible increment:
 *   1. Add a Renovate packageRule that AUTOMERGES `digest` updates for the servarr kustomization's
 *      docker images (closes the gap that stranded the 5 PRs).
 *   2. Add `rebaseWhen: behind-base-branch` globally so every Renovate branch is kept rebased onto main
 *      automatically — no human rebasing (this is the owner's explicit pain point).
 *   3. Remove the now-broken Image Updater annotations from app-servarr.yaml AND app-authentik.yaml
 *      (authentik is already Renovate-owned: patch automerged, minor/major manual by deliberate
 *      DB-migration safety gate — that gate is PRESERVED). This stops the failing 3-minute push loop.
 *   4. Fix the now-stale comments ("image-updater already covers most patch bumps").
 *   5. DEFER full decommission of the now-idle Image Updater install (Application + image-updater/ dir +
 *      git-creds) to a follow-up issue, after a soak proves Renovate covers everything — that is a
 *      destructive deploy, kept out of this PR (evolutionary adopt -> soak -> decommission).
 *
 * Risky surfaces (merge to main + ArgoCD reconcile) are gated behind two owner breakpoints: a DESIGN gate
 * (the Option-3 decision + scope, incl. whether to include authentik) and a DEPLOY/MERGE gate
 * (alwaysBreakOn: deploy, destructive-git). Annotation removal is benign for ArgoCD sync (image-updater
 * reads those annotations, ArgoCD does not), so there is essentially no runtime/pod risk — the gate is
 * about merging to main. Everything else runs through validate + adversarial-review refinement loops.
 *
 * @inputs { issue: number, issueUrl: string, repo: string, dashboardIssue: number, ruleset: string }
 * @outputs { success: boolean, prUrl: string, merged: boolean, issuesClosed: array, followUps: array }
 *
 * Composition references (process library):
 *  - specializations/devops-sre-platform/iac-implementation.js   (plan -> implement -> validate(kustomize build + renovate-config-validator) -> refine loop)
 *  - methodologies/gsd/verify-work                               (quality gate = renders, config valid, scope correct, post-merge ArgoCD Synced/Healthy + push loop quiet)
 *  - methodologies/gsd/iterative-convergence                     (adopt -> soak -> decommission; defer the destructive decommission to a follow-up)
 *  - project/deliver-issue-185-authentik-service-account-token.js (same-repo conventions: branch off origin/main, Epaflix semi-linear merge policy, owner breakpoints, PR test-plan ticking)
 *
 * @skill none
 * @agent general-purpose specializations/devops-sre-platform/agents
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

export async function process(inputs, ctx) {
  const {
    issue = 192,
    issueUrl = 'https://github.com/SpyrosPsarras/epaflix/issues/192',
    repo = 'SpyrosPsarras/epaflix',
    dashboardIssue = 31,
    ruleset = '16805247',
  } = inputs || {};

  const REPO_ROOT = '/home/spy/Documents/Epaflix/k3s-swarm-proxmox';
  const branch = `issue-${issue}-image-updater-writeback-consolidate-renovate`;

  ctx.log('info', `Issue #${issue}: consolidate digest automation on Renovate (automerge digests + auto-rebase), retire broken Image Updater git write-back for servarr + authentik.`);

  // ──────────────────────────────────────────────────────────────────────────
  // PHASE 1: ASSESS + DESIGN (read-only) — produce a concrete, repo-accurate
  // edit plan; confirm Renovate already resolves these digest-only entries;
  // confirm the exact matcher + rebase knob; settle authentik scope + decommission defer.
  // ──────────────────────────────────────────────────────────────────────────
  let design = await ctx.task(designTask, {
    repoRoot: REPO_ROOT, issue, issueUrl, repo, dashboardIssue, ruleset, branch,
  });

  // DESIGN breakpoint (owner) — the Option-3 decision + scope (authentik inclusion, decommission defer).
  let designApproved = false;
  let designFeedback = null;
  for (let attempt = 0; attempt < 5; attempt++) {
    if (designFeedback) {
      const refined = await ctx.task(designTask, {
        repoRoot: REPO_ROOT, issue, issueUrl, repo, dashboardIssue, ruleset, branch,
        feedback: designFeedback, attempt: attempt + 1,
      });
      design = refined;
    }
    const bp = await ctx.breakpoint({
      question:
        `DESIGN APPROVAL — consolidate digest automation on Renovate (#${issue}).\n\n` +
        `Chosen path: Option 3 (drop Image Updater for these apps; Renovate owns digests end-to-end).\n` +
        `Renovate matcher for digest-automerge: ${design.renovateMatcher}\n` +
        `Auto-rebase knob (no manual rebasing): ${design.rebaseKnob}\n` +
        `Apps having Image Updater annotations removed: ${design.appsToStrip}\n` +
        `authentik DB-migration manual gate preserved: ${design.authentikGatePreserved}\n` +
        `Image Updater install decommission: ${design.decommissionDisposition}\n\n` +
        `Files to change: ${(design.filesToChange || []).join(', ')}\n\n` +
        `Summary:\n${design.summary}\n\n` +
        `Approve this design to implement on branch \`${branch}\`?`,
      title: 'Design approval — Renovate digest consolidation (#192)',
      context: {
        runId: ctx.runId, issue, issueUrl, dashboardIssue, ruleset,
        plan: design.plan,
        renovateMatcher: design.renovateMatcher,
        rebaseKnob: design.rebaseKnob,
        appsToStrip: design.appsToStrip,
        filesToChange: design.filesToChange,
        decommissionDisposition: design.decommissionDisposition,
        followUpCandidates: design.followUpCandidates,
        renovateAlreadyResolvesDigests: design.renovateAlreadyResolvesDigests,
      },
      expert: 'owner',
      tags: ['approval-gate', 'architecture'],
      previousFeedback: designFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    if (bp.approved) { designApproved = true; break; }
    designFeedback = bp.response || bp.feedback || 'Not approved';
    ctx.log('warn', `Design breakpoint not approved (attempt ${attempt + 1}): ${designFeedback}`);
  }
  if (!designApproved) {
    ctx.log('error', 'Design not approved after retries; aborting before any change.');
    return { success: false, prUrl: null, merged: false, issuesClosed: [], followUps: [], aborted: 'design-not-approved' };
  }

  // ──────────────────────────────────────────────────────────────────────────
  // PHASE 2: IMPLEMENT (fresh branch) with a VALIDATE quality gate + refine loop.
  // ──────────────────────────────────────────────────────────────────────────
  let impl;
  let implFeedback = null;
  let validation = { pass: false };
  for (let attempt = 0; attempt < 4; attempt++) {
    impl = await ctx.task(implementTask, {
      repoRoot: REPO_ROOT, branch, issue, repo,
      plan: design.plan, renovateMatcher: design.renovateMatcher, rebaseKnob: design.rebaseKnob,
      appsToStrip: design.appsToStrip,
      feedback: implFeedback || undefined, attempt: attempt + 1,
    });
    validation = await ctx.task(validateTask, {
      repoRoot: REPO_ROOT, branch, issue, filesChanged: impl.filesChanged,
    });
    if (validation.pass) break;
    implFeedback = (validation.issues && validation.issues.join('; ')) || 'Validation failed; address issues.';
    ctx.log('warn', `Validation failed (attempt ${attempt + 1}): ${implFeedback}`);
  }
  if (!validation.pass) {
    ctx.log('error', 'Implementation did not pass validation after retries; leaving branch for inspection.');
    return { success: false, prUrl: null, merged: false, issuesClosed: [], followUps: [], aborted: 'validation-failed', branch };
  }

  // ──────────────────────────────────────────────────────────────────────────
  // PHASE 3: ADVERSARIAL REVIEW (quality gate) — correctness vs #192, matcher
  // really catches all servarr digest images, no over-broad automerge (authentik
  // major NOT auto-merged), annotation removal doesn't break the manifests. Refine loop.
  // ──────────────────────────────────────────────────────────────────────────
  let review = { pass: false };
  let reviewFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (reviewFeedback) {
      impl = await ctx.task(implementTask, {
        repoRoot: REPO_ROOT, branch, issue, repo,
        plan: design.plan, renovateMatcher: design.renovateMatcher, rebaseKnob: design.rebaseKnob,
        appsToStrip: design.appsToStrip,
        feedback: reviewFeedback, attempt: attempt + 1,
      });
      const reval = await ctx.task(validateTask, {
        repoRoot: REPO_ROOT, branch, issue, filesChanged: impl.filesChanged,
      });
      if (!reval.pass) { reviewFeedback = (reval.issues || []).join('; ') || 'Re-validation failed'; continue; }
    }
    review = await ctx.task(reviewTask, {
      repoRoot: REPO_ROOT, branch, issue, filesChanged: impl.filesChanged, plan: design.plan,
    });
    if (review.pass) break;
    reviewFeedback = (review.issues && review.issues.join('; ')) || 'Review failed; address issues.';
    ctx.log('warn', `Review failed (attempt ${attempt + 1}): ${reviewFeedback}`);
  }
  if (!review.pass) {
    ctx.log('error', 'Review did not pass after retries; leaving branch for inspection.');
    return { success: false, prUrl: null, merged: false, issuesClosed: [], followUps: [], aborted: 'review-failed', branch };
  }

  // ──────────────────────────────────────────────────────────────────────────
  // PHASE 4: FINALIZE — push branch, open PR (closes #192), open follow-ups,
  // fill + tick the static (pre-merge) test-plan boxes. Does NOT merge.
  // ──────────────────────────────────────────────────────────────────────────
  const finalize = await ctx.task(finalizeTask, {
    repoRoot: REPO_ROOT, branch, issue, issueUrl, repo, dashboardIssue,
    implSummary: impl.summary, reviewSummary: review.summary,
    followUpCandidates: design.followUpCandidates,
    decommissionDisposition: design.decommissionDisposition,
  });

  // ──────────────────────────────────────────────────────────────────────────
  // PHASE 5: DEPLOY/MERGE BREAKPOINT (owner; alwaysBreakOn deploy + destructive-git)
  // ──────────────────────────────────────────────────────────────────────────
  let deployApproved = false;
  let deployFeedback = null;
  for (let attempt = 0; attempt < 5; attempt++) {
    const bp = await ctx.breakpoint({
      question:
        `DEPLOY + MERGE approval (#${issue}).\n` +
        `PR open: ${finalize.prUrl}\n\n` +
        `On approval I will: rebase onto origin/main, wait for the required \`validate\` check, ` +
        `\`gh pr merge --merge\` (Epaflix semi-linear policy), then confirm ArgoCD app-servarr + ` +
        `app-authentik stay Synced/Healthy and the Image Updater push-rejection loop goes quiet ` +
        `(0 images considered for servarr/authentik, errors=0). Renovate then owns digests: it ` +
        `auto-rebases its branches and auto-merges digest PRs.\n\n` +
        `Merges to main. Annotation removal is a no-op for ArgoCD sync (no pod restart). Approve?`,
      title: 'Deploy + merge — Renovate digest consolidation (#192)',
      context: {
        runId: ctx.runId, prUrl: finalize.prUrl, issue,
        followUps: finalize.followUps,
        testPlan: finalize.verification,
        liveEffect: 'merge to main; ArgoCD reconciles app-servarr + app-authentik annotation removal (no pod restart); Image Updater stops failing pushes',
      },
      expert: 'owner',
      tags: ['approval-gate', 'deploy', 'destructive-git'],
      previousFeedback: deployFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    if (bp.approved) { deployApproved = true; break; }
    deployFeedback = bp.response || bp.feedback || 'Not approved';
    ctx.log('warn', `Deploy breakpoint not approved (attempt ${attempt + 1}): ${deployFeedback}`);
  }
  if (!deployApproved) {
    ctx.log('warn', 'Deploy/merge not approved; leaving PR open for the owner.');
    return { success: true, prUrl: finalize.prUrl, merged: false, issuesClosed: [], followUps: finalize.followUps };
  }

  // ──────────────────────────────────────────────────────────────────────────
  // PHASE 6: MERGE -> ArgoCD reconcile -> VERIFY push loop quiet -> close #192
  // ──────────────────────────────────────────────────────────────────────────
  const deploy = await ctx.task(deployVerifyTask, {
    repoRoot: REPO_ROOT, branch, issue, issueUrl, repo, prUrl: finalize.prUrl,
  });

  ctx.log('info', `Done. PR ${finalize.prUrl} merged=${deploy.merged}, verified=${deploy.verified}, issues closed=${JSON.stringify(deploy.issuesClosed)}.`);
  return {
    success: deploy.merged && deploy.verified,
    prUrl: finalize.prUrl,
    merged: deploy.merged,
    verified: deploy.verified,
    issuesClosed: deploy.issuesClosed,
    followUps: finalize.followUps,
  };
}

// ════════════════════════════════════════════════════════════════════════════
// TASKS
// ════════════════════════════════════════════════════════════════════════════

export const designTask = defineTask('design-img-updater-writeback', (args, taskCtx) => ({
  kind: 'agent',
  title: `Assess + design Renovate digest consolidation${args.attempt ? ` (refine ${args.attempt})` : ''}`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'DevOps / GitOps specialist working in a Kustomize+Helm+ArgoCD IaC repo',
      task: `Design the fix for issue #${args.issue}. READ-ONLY — make NO edits and NO commits.`,
      context: {
        repoRoot: args.repoRoot, issue: args.issue, issueUrl: args.issueUrl, repo: args.repo,
        dashboardIssue: args.dashboardIssue, ruleset: args.ruleset, branch: args.branch,
        feedback: args.feedback || null,
      },
      instructions: [
        `cd ${args.repoRoot}.`,
        `Read the issue: gh issue view ${args.issue} --repo ${args.repo}. The owner's stated goal THIS run: ALL apps auto-update end-to-end with NO manual branch rebasing. Chosen path = Option 3 (consolidate on Renovate). Your job is to make that concrete and verify it actually works.`,
        `Read first: .github/renovate.json (the full config + existing packageRules), 2-k3s/11.argocd/apps/app-servarr.yaml (image-updater annotations + image-list), 2-k3s/11.argocd/apps/app-authentik.yaml (it ALSO has write-back-method: git -> main = identical latent breakage), 2-k3s/08.servarr/kustomization.yaml (the digest-pinned images: block), .github/workflows/ci.yml (the required \`validate\` gate). Also skim CLAUDE.md (merge policy, follow-up-issue rule).`,
        `CONFIRM the key premise: the 9 servarr images in 2-k3s/08.servarr/kustomization.yaml are digest-only entries (name + digest, no newTag) and Renovate ALREADY opens digest PRs for them (proven by merged PRs #150/#156/#151/#162/#190 on 2026-06-07). Verify with: gh pr list --repo ${args.repo} --state merged --search "digest" --limit 10, and by reading the kustomization images: block. Report renovateAlreadyResolvesDigests=true/false with evidence. If for any image Renovate would NOT resolve a digest (e.g. an image referenced only in a Deployment spec, not the images: block — newtarr:rolling is NOT in the images: block), call it out explicitly so we do not silently lose its automation.`,
        `Decide the EXACT Renovate packageRule to AUTOMERGE digest updates for the servarr images. Recommended matcher: { matchFileNames: ["2-k3s/08.servarr/kustomization.yaml"], matchDatasources: ["docker"], matchUpdateTypes: ["digest"], automerge: true, automergeType: "pr", platformAutomerge: true } with a clear description. Justify the matcher choice (file-scoped + docker + digest is precise and future-proof vs listing each image). Confirm it will NOT over-match (must NOT enable automerge for authentik minor/major, or any non-digest update). Return it as renovateMatcher (human-readable) and include the exact JSON in plan.`,
        `Decide the auto-rebase knob so NO Renovate branch needs manual rebasing (owner's explicit pain). Recommended: add top-level "rebaseWhen": "behind-base-branch" to renovate.json so every Renovate branch is kept rebased onto main automatically; combined with platformAutomerge this is hands-off under the strict "branch up to date" protection. Confirm this is a valid Renovate option and explain the interaction with the strict required-\`validate\` branch protection. Return as rebaseKnob.`,
        `Decide app scope. Recommended appsToStrip = ["app-servarr.yaml", "app-authentik.yaml"]: remove the argocd-image-updater.argoproj.io/* annotations from BOTH (authentik has the same broken git write-back; it is already Renovate-owned — patch automerged, minor/major manually gated for DB migrations). CRITICAL: the authentik minor/major manual-merge gate MUST be preserved (do not add authentik to any automerge rule). Set authentikGatePreserved accordingly. If you judge authentik should be out of scope for this issue, say so and propose it as a follow-up instead — but default to including it.`,
        `Decide the Image Updater install decommission. Recommended decommissionDisposition = "DEFER to a follow-up issue after a soak": leave the Image Updater Application (2-k3s/11.argocd/apps/app-argocd-image-updater.yaml) and 2-k3s/11.argocd/image-updater/ installed but IDLE (0 tracked images after annotation removal) for this PR — decommissioning it (Application + dir + argocd:git-creds secret) is a destructive deploy better done after Renovate coverage is proven. Confirm that with 0 image-list annotations the controller simply does nothing (no more failing pushes) so leaving it idle is harmless.`,
        `Build the exact filesToChange edit plan. Expected: .github/renovate.json (new digest-automerge rule + rebaseWhen + fix the now-stale comment "image-updater already covers most patch bumps"), 2-k3s/11.argocd/apps/app-servarr.yaml (remove image-updater annotations + the now-wrong header comment about image-updater write-back), 2-k3s/11.argocd/apps/app-authentik.yaml (remove image-updater annotations). NO change to 2-k3s/08.servarr/kustomization.yaml itself (the digest entries stay; Renovate keeps bumping them).`,
        `Propose follow-up gh issues (enhancement shape) to open at finalize, cross-linking #${args.issue}: (a) DECOMMISSION the now-idle ArgoCD Image Updater install (Application + image-updater/ dir + argocd:git-creds secret + helm-values) after a soak window confirms Renovate covers all servarr digests; (b) SOAK verification — confirm the next servarr :latest digest PR opens, auto-rebases, and AUTO-MERGES with zero manual touch (target ~1 week); (c) any newtarr-style gap you found where an image is no longer digest-tracked. Only propose ones that genuinely apply.`,
        `If "feedback" is present, revise the design accordingly.`,
        `Write the design to artifacts/design-issue-${args.issue}.md and produce a concise human-readable summary + a machine-usable plan.`,
        'Return ONLY the JSON result object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['summary', 'plan', 'renovateMatcher', 'rebaseKnob', 'appsToStrip', 'authentikGatePreserved', 'decommissionDisposition', 'filesToChange', 'followUpCandidates', 'renovateAlreadyResolvesDigests'],
      properties: {
        summary: { type: 'string' },
        plan: { type: 'object' },
        renovateMatcher: { type: 'string' },
        rebaseKnob: { type: 'string' },
        appsToStrip: { type: 'string' },
        authentikGatePreserved: { type: 'string' },
        decommissionDisposition: { type: 'string' },
        filesToChange: { type: 'array', items: { type: 'string' } },
        followUpCandidates: { type: 'array', items: { type: 'string' } },
        renovateAlreadyResolvesDigests: { type: 'boolean' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'design', 'gitops', 'renovate'],
}));

export const implementTask = defineTask('implement-img-updater-writeback', (args, taskCtx) => ({
  kind: 'agent',
  title: `Implement Renovate digest-automerge + auto-rebase + strip image-updater annotations (attempt ${args.attempt})`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'DevOps engineer editing Renovate config + ArgoCD Application manifests in a GitOps repo',
      task: `Implement the approved design for issue #${args.issue} on a fresh branch. Do NOT push.`,
      context: {
        repoRoot: args.repoRoot, branch: args.branch, issue: args.issue, repo: args.repo,
        plan: args.plan, renovateMatcher: args.renovateMatcher, rebaseKnob: args.rebaseKnob,
        appsToStrip: args.appsToStrip, feedback: args.feedback || null,
      },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. Branch: git fetch origin && git checkout -B ${args.branch} origin/main. If "feedback" is provided you are REFINING the existing ${args.branch} — incorporate the feedback, do not start over.`,
        `2. .github/renovate.json: ADD a packageRule that automerges digest updates for the servarr images, exactly per plan (recommended: { "description": "...", "matchFileNames": ["2-k3s/08.servarr/kustomization.yaml"], "matchDatasources": ["docker"], "matchUpdateTypes": ["digest"], "automerge": true, "automergeType": "pr", "platformAutomerge": true }). The description must explain WHY (digest updates are classified \`digest\`, not \`patch\`, so the existing patch-automerge rule missed them — #${args.issue}). Place it AFTER the patch-automerge rule. Do NOT touch the authentik minor/major rule (it must keep automerge:false).`,
        `3. .github/renovate.json: ADD top-level "rebaseWhen": "behind-base-branch" so every Renovate branch is auto-rebased onto main (no manual rebasing). Keep valid JSON. Also UPDATE the now-stale patch-automerge rule description that says "image-updater already covers most patch bumps" — image-updater is being retired for these apps, so reword it (Renovate now owns all docker digest + patch bumps for servarr).`,
        `4. 2-k3s/11.argocd/apps/app-servarr.yaml: REMOVE every argocd-image-updater.argoproj.io/* annotation (the whole image-list + per-image update-strategy/allow-tags + write-back-method/target/git-branch/git-credentials block). Also remove/condense the long header comment that describes Image Updater write-back, replacing it with a short note that digest/patch updates are now delivered by Renovate PRs (auto-merged) — see .github/renovate.json. Leave spec.syncPolicy, ignoreDifferences, and everything else untouched.`,
        `5. 2-k3s/11.argocd/apps/app-authentik.yaml (only if "${args.appsToStrip}" includes it): REMOVE the argocd-image-updater.argoproj.io/* annotations there too. Authentik stays Renovate-owned via the existing renovate.json grouping + minor/major manual gate — do NOT change renovate.json's authentik rules. Add a short comment that authentik image/chart bumps are delivered by Renovate (grouped; minor/major manual).`,
        `6. Do NOT edit 2-k3s/08.servarr/kustomization.yaml — the digest entries stay so Renovate keeps bumping them. Do NOT decommission the Image Updater install in this PR (deferred to a follow-up).`,
        `7. Guard: git diff --name-only origin/main must list ONLY: .github/renovate.json, 2-k3s/11.argocd/apps/app-servarr.yaml, and (if in scope) 2-k3s/11.argocd/apps/app-authentik.yaml. Nothing else.`,
        `8. Commit (do NOT push) subject: "feat(renovate): automerge servarr image digests + auto-rebase; retire broken Image Updater git write-back (#${args.issue})". Body: explain Option 3 consolidation, why (Image Updater push to main rejected by required \`validate\`; digest PRs piled up because not automerged), what (digest-automerge rule + rebaseWhen + strip annotations from app-servarr${(args.appsToStrip || '').includes('authentik') ? ' + app-authentik' : ''}), and that the idle Image Updater install decommission is a follow-up. End the commit body with: Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`,
        'Return ONLY the JSON result object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'filesChanged', 'commitSha', 'summary'],
      properties: {
        branch: { type: 'string' },
        filesChanged: { type: 'array', items: { type: 'string' } },
        commitSha: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'implement', 'renovate', 'gitops'],
}));

export const validateTask = defineTask('validate-img-updater-writeback', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Quality gate — renovate config valid, kustomize renders, scope correct',
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'CI/quality-gate engineer for a GitOps IaC repo',
      task: `Validate the branch ${args.branch} for issue #${args.issue} by running real checks. Make NO edits.`,
      context: { repoRoot: args.repoRoot, branch: args.branch, issue: args.issue, filesChanged: args.filesChanged },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. Scope: git diff --name-only origin/main must list ONLY .github/renovate.json and 2-k3s/11.argocd/apps/app-servarr.yaml and (optionally) 2-k3s/11.argocd/apps/app-authentik.yaml. FAIL otherwise.`,
        `2. Renovate config validity: run \`npx --yes --package renovate -- renovate-config-validator .github/renovate.json\` (or \`npx --yes renovate-config-validator\` if that resolves). It MUST report the config is valid. If npx cannot fetch the package (offline), fall back to: validate strict JSON with \`python3 -c "import json;json.load(open('.github/renovate.json'))"\` AND manually confirm the new rule's keys are all valid Renovate fields — note that the validator could not run. FAIL on invalid JSON or an invalid Renovate field.`,
        `3. Confirm the new digest-automerge rule is correct: matchUpdateTypes includes "digest", matchDatasources includes "docker", it is file-scoped to 2-k3s/08.servarr/kustomization.yaml, automerge:true + platformAutomerge:true. Confirm "rebaseWhen": "behind-base-branch" is present at top level. Confirm the authentik minor/major rule STILL has automerge:false (the digest rule must not override it). FAIL on any of these.`,
        `4. Render: mirror the CI \`validate\` gate's "Kustomize build" step on the apps. Run \`kustomize build 2-k3s/11.argocd/apps\` (the Application manifests) and confirm app-servarr and app-authentik still render as valid Application YAML with the annotations removed. If that dir needs --enable-helm or is sops-gated, mirror what ci.yml does; if it cannot build locally, fall back to \`python3 -c "import yaml,sys;list(yaml.safe_load_all(open(f)))"\` on each changed Application file to confirm valid YAML. FAIL on a render/parse error.`,
        `5. Confirm NO argocd-image-updater.argoproj.io/ annotation remains in the stripped app file(s): grep -c should be 0 in app-servarr.yaml (and app-authentik.yaml if in scope).`,
        `6. Confirm 2-k3s/08.servarr/kustomization.yaml is UNCHANGED (its digest entries must remain).`,
        `Return pass=true only if all hold; else pass=false with a concrete issues list.`,
        'Return ONLY the JSON result object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['pass', 'issues'],
      properties: {
        pass: { type: 'boolean' },
        issues: { type: 'array', items: { type: 'string' } },
        renderOk: { type: 'boolean' },
        renovateConfigValid: { type: 'boolean' },
        notes: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'validate', 'quality-gate'],
}));

export const reviewTask = defineTask('review-img-updater-writeback', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Adversarial review — correctness vs #192, matcher coverage, no over-broad automerge',
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Adversarial reviewer for a GitOps + dependency-automation change',
      task: `Verify the change on ${args.branch} fully and correctly delivers issue #${args.issue} with no regressions.`,
      context: { repoRoot: args.repoRoot, branch: args.branch, issue: args.issue, filesChanged: args.filesChanged, plan: args.plan },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. git diff origin/main...${args.branch} — confirm scope is exactly the intended files; nothing unrelated changed; 2-k3s/08.servarr/kustomization.yaml is untouched.`,
        `2. COVERAGE: cross-check the digest-automerge matcher against the actual images in 2-k3s/08.servarr/kustomization.yaml. Every digest-pinned docker image there (sonarr/radarr/prowlarr/bazarr/cleanuparr/flaresolverr/jellyfin/homarr/bazarr-autotranslate) MUST be caught by the rule (file-scoped + docker + digest). Confirm no servarr digest image is left without automerge. If any image was tracked by Image Updater but is NOT in the kustomization images: block (e.g. newtarr:rolling), confirm it was flagged as a known gap / follow-up rather than silently dropped.`,
        `3. NO OVER-MATCH: confirm the new rule cannot automerge anything it should not — specifically authentik minor/major must remain automerge:false, and the rule must not automerge non-digest updates. Re-read all packageRules in order (later rules win in Renovate) and confirm rule ordering does not accidentally re-enable authentik automerge.`,
        `4. ANNOTATION REMOVAL is benign: confirm removing argocd-image-updater.argoproj.io/* annotations does not affect ArgoCD's sync of the Application (those annotations are read by the image-updater controller only). Confirm the Application spec (source/destination/syncPolicy/ignoreDifferences) is intact and the YAML is valid.`,
        `5. rebaseWhen: confirm "behind-base-branch" is a valid value and genuinely addresses "no manual rebasing" under the strict required-\`validate\` protection.`,
        `6. Comments: confirm the stale "image-updater already covers most patch bumps" comment was corrected and the app header comments no longer describe a write-back mechanism that is being retired.`,
        `7. Commit subject/footer correct (Co-Authored-By present).`,
        `Return pass=true only if all hold; else pass=false with a concrete, actionable issues list.`,
        'Return ONLY the JSON result object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['pass', 'issues'],
      properties: {
        pass: { type: 'boolean' },
        issues: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'review', 'quality-gate'],
}));

export const finalizeTask = defineTask('finalize-img-updater-writeback', (args, taskCtx) => ({
  kind: 'agent',
  title: `Push branch, open PR (closes #${args.issue}), open follow-ups`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Release manager following the Epaflix merge policy',
      task: `Push ${args.branch}, open the PR, open follow-ups, post a progress note. Do NOT merge.`,
      context: {
        repoRoot: args.repoRoot, branch: args.branch, issue: args.issue, issueUrl: args.issueUrl,
        repo: args.repo, dashboardIssue: args.dashboardIssue,
        implSummary: args.implSummary, reviewSummary: args.reviewSummary,
        followUpCandidates: args.followUpCandidates, decommissionDisposition: args.decommissionDisposition,
      },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. git push -u origin ${args.branch} (force-with-lease if it already exists remotely).`,
        `2. gh pr create --repo ${args.repo} --base main --head ${args.branch}. Title: "feat(renovate): automerge servarr image digests + auto-rebase; retire broken Image Updater git write-back (#${args.issue})". Body: explain the problem (Image Updater push to main rejected every poll by required \`validate\`; Renovate digest PRs piled up because digest updates are classified \`digest\`, not \`patch\`), the chosen path (Option 3 — Renovate owns digests end-to-end), and the changes (digest-automerge packageRule + rebaseWhen:behind-base-branch + strip image-updater annotations from app-servarr + app-authentik). Note the idle Image Updater install decommission is a follow-up. Use "Closes #${args.issue}". Include a "## Test plan" checklist: "- [ ] renovate-config-validator: config valid", "- [ ] new rule matches all servarr digest images; authentik minor/major still automerge:false", "- [ ] kustomize/YAML: app-servarr + app-authentik still render with annotations removed", "- [ ] (post-merge) ArgoCD app-servarr + app-authentik Synced/Healthy", "- [ ] (post-merge) Image Updater logs: 0 images considered for servarr/authentik, errors=0 (push-rejection loop quiet)", "- [ ] (soak) next servarr :latest digest PR opens, auto-rebases, and auto-merges with no manual touch". End the body with: 🤖 Generated with [Claude Code](https://claude.com/claude-code).`,
        `3. Run the PRE-merge verifications NOW and tick those boxes by EDITING the PR body (never add a comment): renovate-config-validator passes; rule coverage + authentik gate intact; manifests render. Leave the (post-merge) and (soak) boxes unchecked.`,
        `4. Open follow-up gh issues (enhancement shape: ## Finding / ## Current state / ## Desired outcome / ## Notes), cross-linking #${args.issue}, only for items that genuinely apply from followUpCandidates: (a) decommission the now-idle ArgoCD Image Updater install after soak; (b) soak verification that the next digest PR auto-merges with zero manual touch; (c) any image-tracking gap found (e.g. newtarr). Cross-link the Dependency Dashboard #${args.dashboardIssue} where relevant.`,
        `5. Post a brief progress note on #${args.issue} (a comment is fine here) linking the PR; do NOT close the issue (the merge closes it via "Closes").`,
        'Return ONLY the JSON result object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'followUps', 'verification', 'summary'],
      properties: {
        prUrl: { type: 'string' },
        followUps: { type: 'array', items: { type: 'string' } },
        verification: { type: 'object' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'pr', 'github'],
}));

export const deployVerifyTask = defineTask('deploy-verify-img-updater-writeback', (args, taskCtx) => ({
  kind: 'agent',
  title: `Rebase+merge per policy, confirm ArgoCD Synced/Healthy + push loop quiet, close #${args.issue}`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Release manager + cluster operator following the Epaflix semi-linear merge policy',
      task: `Merge ${args.prUrl}, confirm ArgoCD reconciles cleanly and the Image Updater push-rejection loop goes quiet, close #${args.issue}.`,
      context: {
        repoRoot: args.repoRoot, branch: args.branch, issue: args.issue, issueUrl: args.issueUrl,
        repo: args.repo, prUrl: args.prUrl,
      },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. Rebase onto latest origin/main: git fetch origin && git checkout ${args.branch} && git rebase origin/main ; resolve any conflicts ; git push --force-with-lease.`,
        `2. Wait for the required \`validate\` check: gh pr checks ${args.prUrl} --watch (or poll). Do not merge until green AND branch is up to date (strict).`,
        `3. Merge: gh pr merge ${args.prUrl} --merge --repo ${args.repo} (merge-commit per Epaflix policy). Confirm main advanced with a "Merge pull request #N" marker.`,
        `4. ArgoCD: confirm the \`servarr\` and \`authentik\` Applications reconcile the new commit and stay Synced + Healthy (selfHeal is on for servarr). Use \`kubectl -n argocd get applications servarr authentik\` or argocd CLI if available. Annotation removal is a no-op for sync — confirm NO pod restart and NO degraded state. If the cluster is unreachable from here, report that and mark verified based on the merge + manifest correctness, noting the live check is pending.`,
        `5. Image Updater push loop quiet: check the controller logs — \`kubectl -n argocd logs deploy/argocd-image-updater --tail=80\` (name may differ; find it). After annotation removal, servarr/authentik should drop out of the considered set and there must be NO more "remote rejected ... GH013 ... validate" push errors for them. Report the observed images_considered / errors line. If unreachable, note pending.`,
        `6. Tick the (post-merge) test-plan boxes on the PR by EDITING the PR body (never a comment) with what you observed. Leave the (soak) box for the follow-up.`,
        `7. Close #${args.issue} if the merge did not auto-close it (the "Closes" line should). Confirm state=closed via gh issue view.`,
        `8. Optionally delete the remote branch.`,
        'Return ONLY the JSON result object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['merged', 'verified', 'issuesClosed', 'summary'],
      properties: {
        merged: { type: 'boolean' },
        verified: { type: 'boolean' },
        issuesClosed: { type: 'array', items: { type: 'number' } },
        mergeCommit: { type: 'string' },
        argocdStatus: { type: 'string' },
        imageUpdaterLoopQuiet: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'merge', 'deploy', 'verify', 'github'],
}));
