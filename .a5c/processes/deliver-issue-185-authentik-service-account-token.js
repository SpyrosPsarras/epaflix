/**
 * @process project/deliver-issue-185-authentik-service-account-token
 * @description Issue #185 — Provide a DURABLE, scoped Authentik service-account token for IaC/automation
 * so out-of-band Authentik edits during deploys stop failing when ad-hoc tokens expire (this bit twice
 * mid-run during #183). Best-for-services outcome (owner-approved): deliver a declarative, GitOps-managed
 * machine identity instead of ad-hoc personal tokens —
 *   1. an Authentik BLUEPRINT (applied by the worker at startup) that creates a dedicated service-account
 *      user, gives it admin-group membership, and mints a NON-EXPIRING API token whose key value we control;
 *   2. the token/blueprint stored as a SOPS+age `*.enc.yaml` Secret, mounted via the chart's
 *      `blueprints.secrets`, reconciled by ksops on argocd-repo-server (existing infra);
 *   3. fold in #175: retire the ad-hoc personal superuser token from the git-ignored secrets.yml and write
 *      ONE consistent token-policy doc (durable service account = sanctioned automation path; on-demand
 *      minting = humans/edge) — supersede the now-contradictory open PR #225;
 *   4. close #185 and #175; open scoped-down / rotation follow-ups.
 *
 * Risky surfaces (live cluster + secrets + supersedes a PR) are gated behind two owner breakpoints:
 * a DESIGN-approval gate (architecture/secrets decision) and a DEPLOY/MERGE gate (alwaysBreakOn: deploy,
 * destructive-git). Everything else runs through quality/refinement loops.
 *
 * @inputs { issue: number, issueUrl: string, repo: string, relatedIssue: number, relatedPr: number }
 * @outputs { success: boolean, prUrl: string, issuesClosed: array, followUps: array, tokenVerified: boolean }
 *
 * Composition references (process library):
 *  - specializations/devops-sre-platform/secrets-management.js  (assess -> design -> implement -> review-gate -> deploy -> verify -> document)
 *  - specializations/devops-sre-platform/iac-implementation.js  (plan -> implement -> validate(kustomize build) -> refine loop)
 *  - methodologies/gsd/verify-work                              (quality gate = renders, no secret leak, live token authenticates)
 *  - project/rotate-authentik-token.js                          (same-repo conventions: branch off origin/main, Epaflix merge policy, owner breakpoints)
 *
 * @skill none
 * @agent general-purpose specializations/devops-sre-platform/agents
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

export async function process(inputs, ctx) {
  const {
    issue = 185,
    issueUrl = 'https://github.com/SpyrosPsarras/epaflix/issues/185',
    repo = 'SpyrosPsarras/epaflix',
    relatedIssue = 175,
    relatedPr = 225,
  } = inputs || {};

  const REPO_ROOT = '/home/spy/Documents/Epaflix/k3s-swarm-proxmox';
  const APP_DIR = '2-k3s/07.authentik-deployment';
  const branch = `issue-${issue}-authentik-service-account-token`;

  ctx.log('info', `Issue #${issue}: durable Authentik service-account token (blueprint + SOPS), folding #${relatedIssue}, superseding PR #${relatedPr}.`);

  // ──────────────────────────────────────────────────────────────────────────
  // PHASE 1: ASSESS + DESIGN (read-only) — produce the concrete, repo-accurate
  // design + edit plan; verify chart blueprint mechanics; probe old token state.
  // ──────────────────────────────────────────────────────────────────────────
  const design = await ctx.task(designTask, {
    repoRoot: REPO_ROOT, appDir: APP_DIR, issue, issueUrl, repo, relatedIssue, relatedPr,
  });

  // DESIGN breakpoint (owner) — architecture + secrets decision; supersedes a PR.
  let designApproved = false;
  let designFeedback = null;
  for (let attempt = 0; attempt < 5; attempt++) {
    if (designFeedback) {
      // refine the design with the owner's feedback before re-asking
      const refined = await ctx.task(designTask, {
        repoRoot: REPO_ROOT, appDir: APP_DIR, issue, issueUrl, repo, relatedIssue, relatedPr,
        feedback: designFeedback, attempt: attempt + 1,
      });
      design.plan = refined.plan;
      design.summary = refined.summary;
      design.blueprintMechanism = refined.blueprintMechanism;
      design.oldTokenStatus = refined.oldTokenStatus;
    }
    const bp = await ctx.breakpoint({
      question:
        `DESIGN APPROVAL — durable Authentik service-account token (#${issue}).\n\n` +
        `Mechanism: ${design.blueprintMechanism}\n` +
        `Permission scope: ${design.scope}\n` +
        `Old personal token (#${relatedIssue}) live status: ${design.oldTokenStatus}\n` +
        `PR #${relatedPr} disposition: ${design.pr225Disposition}\n\n` +
        `Summary:\n${design.summary}\n\n` +
        `Approve this design to implement on branch \`${branch}\`?`,
      title: 'Design approval — Authentik service-account token',
      context: {
        runId: ctx.runId, issue, issueUrl, relatedIssue, relatedPr,
        plan: design.plan,
        blueprintMechanism: design.blueprintMechanism,
        scope: design.scope,
        filesToChange: design.filesToChange,
        oldTokenStatus: design.oldTokenStatus,
        followUpCandidates: design.followUpCandidates,
      },
      expert: 'owner',
      tags: ['approval-gate', 'architecture', 'secrets-rotation'],
      previousFeedback: designFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    if (bp.approved) { designApproved = true; break; }
    designFeedback = bp.response || bp.feedback || 'Not approved';
    ctx.log('warn', `Design breakpoint not approved (attempt ${attempt + 1}): ${designFeedback}`);
  }
  if (!designApproved) {
    ctx.log('error', 'Design not approved after retries; aborting before any change.');
    return { success: false, prUrl: null, issuesClosed: [], followUps: [], tokenVerified: false, aborted: 'design-not-approved' };
  }

  // ──────────────────────────────────────────────────────────────────────────
  // PHASE 2: IMPLEMENT (fresh branch) with a VALIDATE quality gate + refine loop.
  // Generates the token value (never printed), the SOPS blueprint Secret, wires
  // helm-values/ksops/kustomization, updates secrets.yml (git-ignored) + docs.
  // ──────────────────────────────────────────────────────────────────────────
  let impl;
  let implFeedback = null;
  let validation = { pass: false };
  for (let attempt = 0; attempt < 4; attempt++) {
    impl = await ctx.task(implementTask, {
      repoRoot: REPO_ROOT, appDir: APP_DIR, branch, issue, repo, relatedIssue,
      plan: design.plan, blueprintMechanism: design.blueprintMechanism, scope: design.scope,
      feedback: implFeedback || undefined, attempt: attempt + 1,
    });
    validation = await ctx.task(validateTask, {
      repoRoot: REPO_ROOT, appDir: APP_DIR, branch, issue, filesChanged: impl.filesChanged,
    });
    if (validation.pass) break;
    implFeedback = (validation.issues && validation.issues.join('; ')) || 'Validation failed; address issues.';
    ctx.log('warn', `Validation failed (attempt ${attempt + 1}): ${implFeedback}`);
  }
  if (!validation.pass) {
    ctx.log('error', 'Implementation did not pass validation after retries; leaving branch for inspection.');
    return { success: false, prUrl: null, issuesClosed: [], followUps: [], tokenVerified: false, aborted: 'validation-failed', branch };
  }

  // ──────────────────────────────────────────────────────────────────────────
  // PHASE 3: ADVERSARIAL REVIEW (quality gate) — correctness vs #185/#175,
  // no secret leak in tracked diff, scope correct, docs consistent. Refine loop.
  // ──────────────────────────────────────────────────────────────────────────
  let review = { pass: false };
  let reviewFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (reviewFeedback) {
      impl = await ctx.task(implementTask, {
        repoRoot: REPO_ROOT, appDir: APP_DIR, branch, issue, repo, relatedIssue,
        plan: design.plan, blueprintMechanism: design.blueprintMechanism, scope: design.scope,
        feedback: reviewFeedback, attempt: attempt + 1,
      });
      const reval = await ctx.task(validateTask, {
        repoRoot: REPO_ROOT, appDir: APP_DIR, branch, issue, filesChanged: impl.filesChanged,
      });
      if (!reval.pass) { reviewFeedback = (reval.issues || []).join('; ') || 'Re-validation failed'; continue; }
    }
    review = await ctx.task(reviewTask, {
      repoRoot: REPO_ROOT, appDir: APP_DIR, branch, issue, relatedIssue, relatedPr,
      filesChanged: impl.filesChanged, plan: design.plan,
    });
    if (review.pass) break;
    reviewFeedback = (review.issues && review.issues.join('; ')) || 'Review failed; address issues.';
    ctx.log('warn', `Review failed (attempt ${attempt + 1}): ${reviewFeedback}`);
  }
  if (!review.pass) {
    ctx.log('error', 'Review did not pass after retries; leaving branch for inspection.');
    return { success: false, prUrl: null, issuesClosed: [], followUps: [], tokenVerified: false, aborted: 'review-failed', branch };
  }

  // ──────────────────────────────────────────────────────────────────────────
  // PHASE 4: FINALIZE — push branch, open PR (closes #185 + #175), supersede
  // PR #225, open follow-ups, update issues, fill + tick the static test-plan
  // boxes. Does NOT merge.
  // ──────────────────────────────────────────────────────────────────────────
  const finalize = await ctx.task(finalizeTask, {
    repoRoot: REPO_ROOT, appDir: APP_DIR, branch, issue, issueUrl, repo, relatedIssue, relatedPr,
    implSummary: impl.summary, reviewSummary: review.summary,
    followUpCandidates: design.followUpCandidates,
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
        `\`gh pr merge --merge\` (Epaflix semi-linear policy), then let ArgoCD reconcile the \`authentik\` ` +
        `Application so the worker applies the blueprint and creates the live service account + token. ` +
        `Then I verify the new token authenticates against /api/v3/ and close #${issue} + #${relatedIssue}.\n\n` +
        `This changes LIVE Authentik state (new service-account user + token) and merges to main. Approve?`,
      title: 'Deploy + merge — Authentik service-account token',
      context: {
        runId: ctx.runId, prUrl: finalize.prUrl, issue, relatedIssue, relatedPr,
        followUps: finalize.followUps,
        testPlan: finalize.verification,
        liveEffect: 'ArgoCD sync of app-authentik → blueprint creates service-account user + non-expiring API token',
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
    return { success: true, prUrl: finalize.prUrl, issuesClosed: [], followUps: finalize.followUps, tokenVerified: false, merged: false };
  }

  // ──────────────────────────────────────────────────────────────────────────
  // PHASE 6: MERGE → DEPLOY → VERIFY live token → close issues
  // ──────────────────────────────────────────────────────────────────────────
  const deploy = await ctx.task(deployVerifyTask, {
    repoRoot: REPO_ROOT, appDir: APP_DIR, branch, issue, issueUrl, repo, relatedIssue,
    prUrl: finalize.prUrl,
  });

  ctx.log('info', `Done. PR ${finalize.prUrl} merged=${deploy.merged}, tokenVerified=${deploy.tokenVerified}, issues closed=${JSON.stringify(deploy.issuesClosed)}.`);
  return {
    success: deploy.merged && deploy.tokenVerified,
    prUrl: finalize.prUrl,
    merged: deploy.merged,
    tokenVerified: deploy.tokenVerified,
    issuesClosed: deploy.issuesClosed,
    followUps: finalize.followUps,
  };
}

// ════════════════════════════════════════════════════════════════════════════
// TASKS
// ════════════════════════════════════════════════════════════════════════════

export const designTask = defineTask('design-authentik-sa-token', (args, taskCtx) => ({
  kind: 'agent',
  title: `Assess + design durable Authentik service-account token${args.attempt ? ` (refine ${args.attempt})` : ''}`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'DevOps / IAM / secrets-management specialist working in a GitOps IaC repo',
      task: `Design the durable Authentik service-account token for issue #${args.issue}. READ-ONLY — make NO edits, NO commits, do not touch live state beyond a read-only token probe.`,
      context: {
        repoRoot: args.repoRoot, appDir: args.appDir, issue: args.issue, issueUrl: args.issueUrl,
        repo: args.repo, relatedIssue: args.relatedIssue, relatedPr: args.relatedPr,
        feedback: args.feedback || null,
      },
      instructions: [
        `cd ${args.repoRoot}.`,
        `Context you must read first: ${args.appDir}/kustomization.yaml, ${args.appDir}/helm-values.yaml, ${args.appDir}/ksops-generator.yaml, ${args.appDir}/authentik-app-secrets.enc.yaml (structure only), ${args.appDir}/README.md, .github/instructions/sops.instructions.md, .sops.yaml, .github/hooks/check-sops-encrypted.sh, and the goauthentik chart under ${args.appDir}/charts/ for how \`blueprints.secrets\` / \`blueprints.configMaps\` mount and how the worker applies blueprints.`,
        `Goal (owner-approved): a DURABLE, GitOps-managed machine identity. Design an Authentik BLUEPRINT that, when applied by the worker at startup, creates: (a) a dedicated service-account user (e.g. ak-iac / "IaC Automation"), (b) admin-group membership sufficient to mutate providers, outpost provider arrays, groups, and bindings (recommended scope = membership in the built-in Authentik admins/superuser group for durability; note literal "scoped RBAC" as a future tightening follow-up), and (c) a NON-EXPIRING API token (intent=api) whose key value WE control.`,
        `Decide and JUSTIFY the exact blueprint MECHANISM for chart 2026.5.2. Recommended: ship the whole blueprint YAML (including the token \`key\`) inside a NEW SOPS-encrypted Secret (e.g. ${args.appDir}/authentik-iac-blueprint.enc.yaml) whose stringData has one \`*.yaml\` key holding the blueprint; mount it via helm-values \`blueprints.secrets: [{ name: <secretName> }]\`; add the file to ksops-generator.yaml + kustomization (it is decrypted by ksops on argocd-repo-server, so the token key never sits in git plaintext). If a cleaner chart-native mechanism exists (e.g. token key via env/!Env reference), compare and pick the most robust; explain the trade-off.`,
        `Probe the OLD personal token (#${args.relatedIssue}) live status READ-ONLY: extract authentik_admin_api_token from .github/instructions/secrets.yml IF still present (it may already be a RETIRED comment), and curl -s -o /dev/null -w "%{http_code}" -m 10 -H "Authorization: Bearer <TOKEN>" https://auth.epaflix.com/api/v3/core/users/me/ . Report the code (expect 401/403 = already dead). NEVER print the token value. If the key is already a retired comment, report that.`,
        `Coordinate #${args.relatedIssue} + PR #${args.relatedPr}: read PR #${args.relatedPr} (gh pr view ${args.relatedPr} --repo ${args.repo}) and the local branch issue-${args.relatedIssue}-authentik-admin-token-revoke. PR #${args.relatedPr} documents "no standing token / mint on-demand", which CONTRADICTS this durable-token work. Plan to SUPERSEDE PR #${args.relatedPr}: fold its useful on-demand-minting runbook into ONE consolidated token-policy section in ${args.appDir}/README.md (durable service account = sanctioned automation path; on-demand minting = humans/edge cases), then close PR #${args.relatedPr} as superseded by the new PR. This work should close BOTH #${args.issue} and #${args.relatedIssue}.`,
        `Build the exact filesToChange edit plan: (1) NEW ${args.appDir}/authentik-iac-blueprint.enc.yaml (SOPS Secret carrying the blueprint); (2) ${args.appDir}/helm-values.yaml (blueprints.secrets wiring); (3) ${args.appDir}/ksops-generator.yaml (+ the new enc file); (4) ${args.appDir}/kustomization.yaml comment update if needed; (5) .github/instructions/secrets.yml (git-ignored) — ADD the new SA token key (e.g. authentik_iac_service_account_token) and RETIRE authentik_admin_api_token if not already; (6) ${args.appDir}/README.md (consolidated token-policy doc). Confirm whether check-sops-encrypted.sh needs the new file allowlisted or whether the .enc.yaml suffix already satisfies it.`,
        `Propose follow-up issues (enhancement shape) to open at finalize: e.g. (a) tighten the service account to scoped RBAC instead of admin-group membership; (b) define a rotation cadence/mechanism for the non-expiring token; (c) any chart-version coupling to watch. Cross-link #${args.issue}.`,
        `If "feedback" is present, revise the design accordingly.`,
        `Produce a concise human-readable design summary and a machine-usable plan. Write the design to artifacts/design-issue-${args.issue}.md as well.`,
        'Return ONLY the JSON result object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['summary', 'plan', 'blueprintMechanism', 'scope', 'oldTokenStatus', 'pr225Disposition', 'filesToChange', 'followUpCandidates'],
      properties: {
        summary: { type: 'string' },
        plan: { type: 'object' },
        blueprintMechanism: { type: 'string' },
        scope: { type: 'string' },
        oldTokenStatus: { type: 'string' },
        pr225Disposition: { type: 'string' },
        filesToChange: { type: 'array', items: { type: 'string' } },
        followUpCandidates: { type: 'array', items: { type: 'string' } },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'design', 'secrets', 'iam'],
}));

export const implementTask = defineTask('implement-authentik-sa-token', (args, taskCtx) => ({
  kind: 'agent',
  title: `Implement blueprint + SOPS token + wiring + docs (attempt ${args.attempt})`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'DevOps engineer authoring GitOps Kustomize+Helm+SOPS manifests',
      task: `Implement the approved design for issue #${args.issue} on a fresh branch. Generate the durable service-account blueprint + SOPS Secret, wire it in, update secrets.yml (git-ignored) + docs.`,
      context: {
        repoRoot: args.repoRoot, appDir: args.appDir, branch: args.branch, issue: args.issue,
        repo: args.repo, relatedIssue: args.relatedIssue,
        plan: args.plan, blueprintMechanism: args.blueprintMechanism, scope: args.scope,
        feedback: args.feedback || null,
      },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. Branch: git fetch origin && git checkout -B ${args.branch} origin/main. If "feedback" is provided you are REFINING the existing ${args.branch} — incorporate the feedback, do not start over from scratch (re-checkout only if needed).`,
        `2. Generate a high-entropy token value (e.g. openssl rand -hex 32 or python secrets.token_urlsafe(48)). This is the API token key. NEVER print it to stdout/logs and NEVER write it to any tracked plaintext file. It goes ONLY into the SOPS-encrypted Secret and into the git-ignored secrets.yml.`,
        `3. Author the Authentik blueprint exactly per plan.blueprintMechanism: a service-account user, admin-group membership (${args.scope}), and a NON-EXPIRING api token whose key = the generated value. Use the correct goauthentik blueprint schema (authentik_core.user with type=service_account or internal_service_account; group membership; authentik_core.token with intent=api, expiring=false, the user as the token's user, and the explicit key). Validate the schema against the chart/docs.`,
        `4. Write it as the SOPS Secret: create ${args.appDir}/authentik-iac-blueprint.enc.yaml. Draft plaintext (kind: Secret, namespace: app-authentik, type: Opaque) with stringData holding a single \`<name>.yaml\` key whose value is the full blueprint YAML (block scalar). Encrypt per .github/instructions/sops.instructions.md: sops -e plaintext > authentik-iac-blueprint.enc.yaml ; then shred/rm the plaintext. The age recipient comes from .sops.yaml automatically. The pre-commit hook must accept it (it carries a sops: block).`,
        `5. Wire it: helm-values.yaml -> set blueprints.secrets to include { name: <secretName> } (the metadata.name of the SOPS Secret). ksops-generator.yaml -> add the new enc file to files:. kustomization.yaml -> update the Secrets comment block to mention the new blueprint Secret. Keep image-updater \`images:\` block and existing generators intact.`,
        `6. secrets.yml (.github/instructions/secrets.yml, git-ignored — NEVER git-add): add the new key (e.g. authentik_iac_service_account_token: "<value>") with a short comment cross-linking #${args.issue}; and RETIRE authentik_admin_api_token (#${args.relatedIssue}) if it is still an active key (replace with a retired comment). Make a backup .github/instructions/secrets.yml.bak-${args.issue} first. Confirm git check-ignore passes for both.`,
        `7. Docs: update ${args.appDir}/README.md with ONE consolidated "Admin/Automation API tokens" policy section: the durable service-account token (this work, #${args.issue}) is the sanctioned path for IaC/automation that mutates Authentik objects (created declaratively via the mounted blueprint; key lives in SOPS); the personal superuser token was retired (#${args.relatedIssue}); on-demand scoped+expiring minting remains for one-off human use. Fold in the useful runbook content from PR #${args.relatedPr}. Use ONLY placeholders for any token value.`,
        `8. Guard: git diff --name-only origin/main must list ONLY expected files (the .enc.yaml, helm-values.yaml, ksops-generator.yaml, kustomization.yaml, README.md). secrets.yml must NOT appear (git-ignored). NO plaintext token anywhere in the tracked diff.`,
        `9. Commit (do NOT push) subject: "feat(authentik): durable service-account API token via declarative blueprint + SOPS (#${args.issue})". In the body note it also retires the personal admin token (#${args.relatedIssue}) and supersedes PR #${args.relatedPr}. End the commit body with: Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`,
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
        secretsYmlUpdated: { type: 'boolean' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
  labels: ['agent', 'implement', 'secrets', 'iac'],
}));

export const validateTask = defineTask('validate-authentik-sa-token', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Quality gate — kustomize build renders, SOPS decrypts, no secret leak, hook passes',
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'CI/quality-gate engineer for a GitOps IaC repo',
      task: `Validate the implementation on branch ${args.branch} for issue #${args.issue} by running the repo's own checks. Make NO edits.`,
      context: { repoRoot: args.repoRoot, appDir: args.appDir, branch: args.branch, issue: args.issue, filesChanged: args.filesChanged },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. Confirm git diff --name-only origin/main lists ONLY expected files; .github/instructions/secrets.yml must NOT be tracked. FAIL otherwise.`,
        `2. SOPS sanity: sops -d ${args.appDir}/authentik-iac-blueprint.enc.yaml | head -40 must decrypt and show a kind: Secret whose stringData holds a *.yaml blueprint with a service-account user, group membership, and a token (intent api, non-expiring). Do NOT print the actual token key value in your report — confirm presence only.`,
        `3. Render: kustomize build --enable-helm --enable-alpha-plugins --enable-exec ${args.appDir} (use the repo's pinned kustomize if the CI wrapper does; mirror buildOptions from 2-k3s/11.argocd/helm-values.yaml). It must succeed AND the rendered output must include the blueprint Secret and the chart must reference it under blueprints.secrets. If ksops cannot run locally (no age key), fall back to validating the kustomization structurally and note it. FAIL on a hard render error unrelated to a missing local age key.`,
        `4. Pre-commit hook: run .github/hooks/check-sops-encrypted.sh against the staged/changed files (or simulate its logic) — the new .enc.yaml must pass (has sops: block); FAIL if it flags any plaintext Secret.`,
        `5. Secret-leak scan: git diff origin/main must contain NO real token value (long hex/base64/urlsafe strings outside the sops ciphertext, no "Bearer <real>"). Only placeholders + sops ciphertext allowed. FAIL on any real value.`,
        `6. Confirm helm-values blueprints.secrets name matches the SOPS Secret metadata.name, and ksops-generator files: includes the new enc file.`,
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

export const reviewTask = defineTask('review-authentik-sa-token', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Adversarial review — correctness vs #185/#175, scope, docs consistency, no leak',
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Adversarial reviewer for a GitOps IaC + IAM change',
      task: `Verify the change on ${args.branch} fully and correctly delivers issue #${args.issue} (and folds #${args.relatedIssue}) with no regressions or secret leaks.`,
      context: {
        repoRoot: args.repoRoot, appDir: args.appDir, branch: args.branch, issue: args.issue,
        relatedIssue: args.relatedIssue, relatedPr: args.relatedPr, filesChanged: args.filesChanged, plan: args.plan,
      },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. git diff origin/main...${args.branch} — confirm scope is exactly the intended files; nothing unrelated changed.`,
        `2. Blueprint correctness: decrypt the .enc.yaml and confirm the blueprint will create a service-account user, give it the intended admin-group membership (${'${scope}'} per plan), and a NON-EXPIRING api token with an explicit key. Confirm the goauthentik blueprint schema is valid (entries with model/identifiers/attrs as the chart expects). FAIL on schema errors.`,
        `3. Wiring: helm-values blueprints.secrets references the exact Secret name; ksops-generator lists the new file; kustomization comments are accurate. The chart's blueprint mount actually picks up *.yaml keys from that Secret.`,
        `4. #${args.relatedIssue} fold-in: README has ONE consistent token-policy section (durable SA = automation; personal token retired; on-demand = human). No contradictory "no standing token at all" language remains. secrets.yml (read-only check, do not stage) has the new key and the old key retired.`,
        `5. No secret leak in the tracked diff (only placeholders + sops ciphertext).`,
        `6. Commit subject/footer correct.`,
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

export const finalizeTask = defineTask('finalize-authentik-sa-token', (args, taskCtx) => ({
  kind: 'agent',
  title: `Push branch, open PR (closes #${args.issue}+#${args.relatedIssue}), supersede PR #${args.relatedPr}, follow-ups`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Release manager following the Epaflix merge policy',
      task: `Push ${args.branch}, open the PR, supersede PR #${args.relatedPr}, open follow-ups, update issues. Do NOT merge.`,
      context: {
        repoRoot: args.repoRoot, appDir: args.appDir, branch: args.branch, issue: args.issue,
        issueUrl: args.issueUrl, repo: args.repo, relatedIssue: args.relatedIssue, relatedPr: args.relatedPr,
        implSummary: args.implSummary, reviewSummary: args.reviewSummary,
        followUpCandidates: args.followUpCandidates,
      },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. git push -u origin ${args.branch} (force-with-lease if it already exists remotely).`,
        `2. gh pr create --repo ${args.repo} --base main --head ${args.branch}. Title: "feat(authentik): durable service-account API token via declarative blueprint + SOPS (#${args.issue})". Body: explain the durable machine identity (blueprint + SOPS, non-expiring), why (#${args.issue}: on-demand tokens expired mid-run during #183); state it RETIRES the personal admin token and SUPERSEDES PR #${args.relatedPr} (folds its runbook into the README); use "Closes #${args.issue}" and "Closes #${args.relatedIssue}". Include a "## Test plan" checklist: "- [ ] kustomize build renders (blueprint Secret present)", "- [ ] SOPS decrypts; no plaintext token in tracked diff", "- [ ] pre-commit hook accepts the .enc.yaml", "- [ ] (post-merge) ArgoCD app-authentik Synced/Healthy", "- [ ] (post-merge) blueprint applied: service-account user + token exist", "- [ ] (post-merge) new token authenticates: GET /api/v3/core/users/me/ returns 200". End the body with: 🤖 Generated with [Claude Code](https://claude.com/claude-code).`,
        `3. Run the PRE-merge verifications now and tick those boxes by EDITING the PR body (never add a comment): kustomize build renders; SOPS decrypts + no plaintext token in diff; hook accepts the file. Leave the (post-merge) boxes unchecked.`,
        `4. Supersede PR #${args.relatedPr}: gh pr comment ${args.relatedPr} --repo ${args.repo} explaining it is superseded by the new PR (link it) because #${args.issue} establishes a durable service-account token that makes the "no standing token" framing obsolete; the on-demand runbook was folded into the new PR. Then gh pr close ${args.relatedPr} --repo ${args.repo}. (Do not delete its branch.)`,
        `5. Open follow-up gh issues (enhancement shape: ## Finding / ## Current state / ## Desired outcome / ## Notes), cross-linking #${args.issue}, only for items that genuinely apply from followUpCandidates (e.g. tighten to scoped RBAC; rotation cadence for the non-expiring token).`,
        `6. Post a brief progress note on #${args.issue} (a comment is fine here) summarizing the PR; do NOT close issues yet (merge closes them via "Closes").`,
        'Return ONLY the JSON result object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'followUps', 'verification', 'pr225Closed', 'summary'],
      properties: {
        prUrl: { type: 'string' },
        followUps: { type: 'array', items: { type: 'string' } },
        verification: { type: 'object' },
        pr225Closed: { type: 'boolean' },
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

export const deployVerifyTask = defineTask('deploy-verify-authentik-sa-token', (args, taskCtx) => ({
  kind: 'agent',
  title: `Rebase+merge per policy, ArgoCD sync, verify live token, close #${args.issue}+#${args.relatedIssue}`,
  agent: {
    name: 'general-purpose',
    prompt: {
      role: 'Release manager + cluster operator following the Epaflix semi-linear merge policy',
      task: `Merge ${args.prUrl}, let ArgoCD apply the blueprint, verify the new service-account token works, close #${args.issue} and #${args.relatedIssue}.`,
      context: {
        repoRoot: args.repoRoot, appDir: args.appDir, branch: args.branch, issue: args.issue,
        issueUrl: args.issueUrl, repo: args.repo, relatedIssue: args.relatedIssue, prUrl: args.prUrl,
      },
      instructions: [
        `cd ${args.repoRoot}.`,
        `1. Rebase onto latest origin/main: git fetch origin && git checkout ${args.branch} && git rebase origin/main ; resolve any conflicts ; git push --force-with-lease.`,
        `2. Wait for the required \`validate\` check: gh pr checks ${args.prUrl} --watch (or poll). Do not merge until green AND branch is up to date (strict).`,
        `3. Merge: gh pr merge ${args.prUrl} --merge --repo ${args.repo} (merge-commit per Epaflix policy). Confirm main advanced with a "Merge pull request #N" marker.`,
        `4. ArgoCD sync: ensure the \`authentik\` Application reconciles the new commit. If selfHeal/auto-sync is on it will; otherwise trigger a sync (argocd app sync authentik, or kubectl-based). Wait for Synced + Healthy. The new blueprint Secret must land in app-authentik and the worker must apply the blueprint (check worker logs for blueprint application; e.g. kubectl -n app-authentik logs deploy/authentik-worker | grep -i blueprint).`,
        `5. Verify the live service account + token: the blueprint should have created the service-account user and a non-expiring api token. Extract the new token value from .github/instructions/secrets.yml (git-ignored) and curl -s -o /dev/null -w "%{http_code}" -m 10 -H "Authorization: Bearer <TOKEN>" https://auth.epaflix.com/api/v3/core/users/me/ — expect 200. Also confirm it can read an admin-scoped endpoint it must mutate, e.g. GET /api/v3/outposts/instances/ returns 200 (read-only check; do not mutate). NEVER print the token value. If 401/403, the blueprint did not apply or the key mismatched — report tokenVerified=false with diagnostics (do NOT close issues).`,
        `6. If verified: close #${args.issue} and #${args.relatedIssue} if the merge did not auto-close them (the PR "Closes" lines should). Confirm state=closed via gh issue view.`,
        `7. Optionally delete the remote branch.`,
        'Return ONLY the JSON result object.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['merged', 'tokenVerified', 'issuesClosed', 'summary'],
      properties: {
        merged: { type: 'boolean' },
        tokenVerified: { type: 'boolean' },
        issuesClosed: { type: 'array', items: { type: 'number' } },
        mergeCommit: { type: 'string' },
        argocdStatus: { type: 'string' },
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
