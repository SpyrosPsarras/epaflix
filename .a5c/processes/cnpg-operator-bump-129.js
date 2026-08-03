/**
 * @process specializations/devops-sre-platform/cnpg-operator-bump-129
 * @description Deliver Epaflix issue #128 (re-targeted): the requested CNPG 1.30 does not
 *   exist (latest upstream is v1.29.1, 2026-05-08). Re-vendor the GitOps-managed CloudNativePG
 *   operator manifest from v1.28.0 -> v1.29.1, bump version notes, validate (`kustomize build`),
 *   ship via the repo merge policy, and let the (already selfHeal-ON, #127) `cnpg-operator`
 *   ArgoCD Application reconcile. Watch CRD schema churn 1.28->1.29 — the risky part — and
 *   confirm the live postgres Cluster stays Healthy. Re-target + close #128.
 * @inputs { repoRoot, appName, appNs, operatorManifest, kustomization, readme,
 *           fromVersion, toVersion, releaseManifestUrl, branch, repo, issue }
 * @outputs { success, merged, reconciled, prUrl, operatorImage, clusterHealthy, issueFinal }
 *
 * Deploy risk: the `cnpg-operator` App has selfHeal ON + ServerSideApply (#127), so a MERGE to
 * main auto-applies the new operator + upgraded CRD schemas to the live cluster (cnpg-system) and
 * rolls the controller-manager. Gated by a mandatory deploy/destructive-git breakpoint before
 * push+PR+merge, and a recovery breakpoint if post-merge verification regresses. Rollback = revert PR.
 *
 * @agent general-purpose (work executor for kubectl/argocd/kustomize/git/gh + diff classification + verification)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';

// ---------------------------------------------------------------------------
// Task definitions
// ---------------------------------------------------------------------------

// Phase 1 — pre-flight: snapshot live operator + classify upstream 1.29.1 diff. NO mutation.
const preflightTask = defineTask('preflight-diff', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Snapshot live CNPG operator + classify v1.28.0 -> v1.29.1 upstream diff (no mutation)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Kubernetes/CloudNativePG SRE working on the Epaflix k3s cluster',
      task:
        'Capture the current live state of the CloudNativePG operator + its ArgoCD Application, fetch the ' +
        'upstream ' + args.toVersion + ' release manifest, and classify the delta vs the vendored ' +
        args.fromVersion + ' manifest. Pay special attention to CRD schema churn. DO NOT change anything.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. You have kubectl + argocd + kustomize CLI configured.',
        'SANITY: confirm upstream v1.30 truly does not exist (`gh release list --repo cloudnative-pg/cloudnative-pg --limit 20` / tags) and that ' + args.toVersion + ' is the newest stable. If a 1.30 GA appeared since, STOP and report it (target may change).',
        'Live snapshot: `kubectl -n ' + args.appNs + ' get deploy -o wide` and the controller image (`kubectl -n ' + args.appNs + ' get deploy cnpg-controller-manager -o jsonpath="{.spec.template.spec.containers[0].image}"`; expect 1.28.0). Record `kubectl get crd | grep cnpg.io` with their versions, and `argocd app get ' + args.appName + ' --refresh` SYNC/HEALTH + syncPolicy (expect selfHeal ON + ServerSideApply per #127).',
        'Record live postgres Cluster health: `kubectl -n ' + args.appNs + ' get cluster` (name, instances, status, primary) — this is the pre-change baseline.',
        'Fetch the upstream release manifest to a scratch path (NOT into the repo yet): `curl -fsSL ' + args.releaseManifestUrl + ' -o /tmp/cnpg-' + args.toVersion + '.yaml`. Verify it is the operator manifest and note the image tag it ships.',
        'Diff the upstream ' + args.toVersion + ' manifest against the vendored ' + args.operatorManifest + '. CLASSIFY changes into: (a) IMAGE bump (1.28.0 -> ' + args.toVersion + '); (b) CRD schema changes (new/removed fields, new stored/served versions, conversion-webhook changes) — enumerate the riskiest; (c) RBAC (ClusterRole/Role) changes; (d) webhook / Deployment / config changes; (e) added/removed resources. The repo intentionally vendors operator + Barman plugin in the same kustomize root (LoadRestrictionsRootOnly) — do NOT touch barman-manifest.yaml.',
        'Barman compat: confirm Barman Cloud Plugin v0.12.0 requirement (CNPG >= 1.26) is still satisfied at ' + args.toVersion + '. Confirm native barmanObjectStore is already migrated to the plugin (#10) so the 1.30-deprecation note in the README is not a blocker for 1.29.',
        'Assess upgrade risk: will applying the new CRDs require conversion of existing Cluster CRs? Will the controller-manager rollout cause a postgres failover? Flag anything that needs the owner to know before merging.',
        'Save raw captures (live snapshot, classified diff) under tasks/' + taskCtx.effectId + '/ if helpful. Return ONLY the structured JSON result, not a plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['liveOperatorImage', 'appSync', 'appHealth', 'crdChanges', 'rbacChanges', 'otherChanges', 'clusterBaseline', 'barmanCompatOk', 'v130Exists', 'riskAssessment', 'summary'],
      properties: {
        liveOperatorImage: { type: 'string' },
        appSync: { type: 'string' },
        appHealth: { type: 'string' },
        selfHealOn: { type: 'boolean' },
        crdChanges: { type: 'array', items: { type: 'string' } },
        rbacChanges: { type: 'array', items: { type: 'string' } },
        otherChanges: { type: 'array', items: { type: 'string' } },
        clusterBaseline: { type: 'object' },
        barmanCompatOk: { type: 'boolean' },
        v130Exists: { type: 'boolean' },
        riskAssessment: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 2 — re-vendor manifest + bump notes + kustomize build validate + local commit (reversible, NO push).
const revendorTask = defineTask('revendor-validate', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Re-vendor operator manifest to ' + args.toVersion + ', bump notes, kustomize build, local commit',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer maintaining the Epaflix GitOps repo',
      task:
        'Re-vendor the CloudNativePG operator manifest at ' + args.toVersion + ', update all version notes, ' +
        'validate with `kustomize build`, and make ONE local commit on a branch. DO NOT push, DO NOT open a PR.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Respect CLAUDE.md: never commit secrets; do NOT relax kustomize LoadRestrictions; keep operator + barman manifests in the same root.',
        'Replace ' + args.operatorManifest + ' wholesale with the upstream ' + args.toVersion + ' manifest fetched in preflight (/tmp/cnpg-' + args.toVersion + '.yaml). Preserve the repo convention: this is the verbatim vendored upstream manifest (the only image refs should now be ' + args.toVersion + '). Do NOT hand-edit individual resources beyond what upstream ships.',
        'Update version notes everywhere ' + args.fromVersion + ' is mentioned: ' + args.kustomization + ' header (line ~15 "CNPG operator v1.28.0"), and ' + args.readme + ' (lines referencing "v1.28.0" operator install + the directory comment). Update the README "v1.28" deprecation/monitoring notes ONLY where they are version-pinned to the operator release; leave behavioral notes (enablePodMonitor deprecated) intact unless ' + args.toVersion + ' changes them.',
        'VALIDATE: `kustomize build 2-k3s/06.postgres/operator-kustomization/ > /dev/null` must succeed. Also `kustomize build 2-k3s/06.postgres/ > /dev/null` (the parent App root) must succeed. Capture any error verbatim. If validation fails, fix the vendoring and re-run until clean (or return validationOk=false with the exact error).',
        'Confirm `git diff --stat` shows only cnpg-operator.yaml + the note files changed (NOT barman-manifest.yaml, NOT secrets, NOT *.enc.yaml).',
        'If feedback is present in context (a prior breakpoint rejection), incorporate it before committing.',
        'Create branch ' + args.branch + ' off origin/main (fetch first; if it already exists, reuse + reset to origin/main). Stage only the intended files. Make ONE commit. Conventional-commit subject e.g. "feat(cnpg): bump operator v1.28.0 -> ' + args.toVersion + ' (#' + args.issue + ')". Body explains 1.30 does not exist so #128 re-targets to ' + args.toVersion + '. End the commit body with the Co-Authored-By trailer for Claude Opus 4.8 (1M context). Do NOT push.',
        'Draft the PR title + body (do not open it). Body must note: re-targets #128 from non-existent 1.30 to ' + args.toVersion + '; CRD schema churn summary from preflight; selfHeal ON so merge auto-applies; rollback = revert. Include a test-plan checklist (kustomize build, App Synced/Healthy, operator pod on ' + args.toVersion + ', CRDs Established, postgres Cluster Healthy + no failover).',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['validationOk', 'changedFiles', 'commitSha', 'branch', 'prTitle', 'prBody', 'diffStat'],
      properties: {
        validationOk: { type: 'boolean' },
        validationError: { type: 'string' },
        changedFiles: { type: 'array', items: { type: 'string' } },
        commitSha: { type: 'string' },
        branch: { type: 'string' },
        prTitle: { type: 'string' },
        prBody: { type: 'string' },
        diffStat: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 3 — publish: push, open PR, rebase per merge policy, wait validate, merge.
const publishMergeTask = defineTask('publish-merge', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Push branch, open PR, rebase per policy, wait validate, merge --merge',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer publishing an OWNER-APPROVED change to SpyrosPsarras/epaflix',
      task:
        'Execute the approved publish+merge: push the branch, open the PR, rebase per the repo merge policy, ' +
        'wait for the `validate` check, then merge with --merge. This MERGE deploys to the live cluster via selfHeal.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Push branch ' + args.branch + ' to origin.',
        'Open PR to main with approvedPrTitle / approvedPrBody (in context). Cross-link #' + args.issue + '.',
        'Repo merge policy (semi-linear): rebase the branch onto origin/main and `git push --force-with-lease`; the strict up-to-date block + required `validate` check must pass. Wait for `validate` to go green: `gh pr checks <n> --watch`. If `validate` fails with the known unpinned-kustomize install rate-limit flake (#164, fast ~6s fail), just `gh run rerun --failed` and re-wait. Do NOT disable or bypass the gate.',
        'Once `validate` is green and the PR is mergeable, merge: `gh pr merge <n> --merge`. Capture the merge commit SHA.',
        'Do NOT delete the branch immediately (keep for rollback reference). Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'merged', 'mergeSha', 'validatePassed', 'detail'],
      properties: {
        prUrl: { type: 'string' },
        prNumber: { type: 'number' },
        merged: { type: 'boolean' },
        mergeSha: { type: 'string' },
        validatePassed: { type: 'boolean' },
        detail: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 4 — reconcile + verify: ArgoCD applies new operator (selfHeal), verify health.
const verifyTask = defineTask('reconcile-verify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Reconcile cnpg-operator App + verify operator ' + args.toVersion + ', CRDs, postgres Cluster health',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'CloudNativePG SRE verifying a post-merge GitOps reconcile',
      task:
        'Confirm the merged ' + args.toVersion + ' manifest reconciled cleanly: operator upgraded, CRDs upgraded ' +
        'and Established, ArgoCD App Synced/Healthy, and the live postgres Cluster stayed Healthy (no lost primary).',
      context: { ...args },
      instructions: [
        'selfHeal is ON, so the merge should auto-trigger a sync. To apply immediately rather than wait for the poll, you MAY run `argocd app get ' + args.appName + ' --refresh` then `argocd app sync ' + args.appName + ' --server-side` (ServerSideApply is the app convention; NEVER --prune). If the first CRD apply hits a field-manager conflict, retry with `--force-conflicts` on the chart/manifest resources only.',
        'Wait for the operator rollout: `kubectl -n ' + args.appNs + ' rollout status deploy/cnpg-controller-manager --timeout=300s`. Confirm the new controller image == ' + args.toVersion + '.',
        'Verify CRDs: `kubectl get crd | grep cnpg.io` all Established=True; no CRD stuck NonStructural. Check the operator logs for upgrade/conversion errors: `kubectl -n ' + args.appNs + ' logs deploy/cnpg-controller-manager --tail=80`.',
        'Verify postgres Cluster (NOTE: the Cluster CR lives in namespace ' + args.clusterNs + ', NOT ' + args.appNs + '): `kubectl -n ' + args.clusterNs + ' get cluster` — instances == baseline, status Healthy, a primary is elected. Compare to clusterBaseline in context. A brief controller restart is fine; a LOST primary / Cluster not Healthy is a regression.',
        'Verify ArgoCD: `argocd app get ' + args.appName + '` Synced + Healthy (or only known/benign noise). `kubectl -n ' + args.appNs + ' get pods` all Ready.',
        'Set healthy=true ONLY if: operator on ' + args.toVersion + ', all CRDs Established, App Synced/Healthy, Cluster Healthy with a primary, zero regressions. List any regressions precisely.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['healthy', 'operatorImage', 'crdsEstablished', 'appSync', 'appHealth', 'clusterHealth', 'regressions', 'summary'],
      properties: {
        healthy: { type: 'boolean' },
        operatorImage: { type: 'string' },
        crdsEstablished: { type: 'boolean' },
        appSync: { type: 'string' },
        appHealth: { type: 'string' },
        clusterHealth: { type: 'object' },
        regressions: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 5 — finalize: execute PR test plan, re-target + close issue #128, open any follow-ups.
const finalizeTask = defineTask('finalize-issue', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Execute PR test plan + re-target/close issue #' + args.issue,
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer closing out an Epaflix delivery',
      task:
        'Finalize: record the verified test-plan results on the merged PR (edit the PR body checkboxes, do not add a ' +
        'new comment), re-target and close issue #' + args.issue + ', and open follow-up issues for any deferred work.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Use the verification results in context (verify.*).',
        'PR test plan: edit the merged PR (' + args.prUrl + ') BODY to check off each test-plan box with the observed outcome (operator image, CRDs Established, App Synced/Healthy, Cluster Healthy). Per repo convention, record by EDITING the PR body, NEVER by adding a new comment.',
        'Issue #' + args.issue + ': it was filed for a non-existent 1.30. Edit the title to reflect the delivered ' + args.toVersion + ' bump, add a brief comment noting 1.30 never shipped (latest ' + args.toVersion + ') and that the GitOps bump is delivered + reconciled, then CLOSE it as completed. Cross-link the PR.',
        'Follow-ups (CLAUDE.md rule — every deferred item gets a gh issue): if the original 1.30 intent should be revisited when 1.30 GA ships, open a tracking issue "Bump CNPG operator to 1.30 when it ships" using the ## Finding / ## Current state / ## Desired outcome / ## Notes shape, cross-linking #' + args.issue + '. If verification surfaced any benign-but-notable drift, file it too. If nothing is deferred, say so explicitly.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['testPlanRecorded', 'issueFinal', 'followUpIssues', 'summary'],
      properties: {
        testPlanRecorded: { type: 'boolean' },
        issueFinal: { type: 'string' },
        followUpIssues: { type: 'array', items: { type: 'string' } },
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
// Orchestration
// ---------------------------------------------------------------------------

export async function process(inputs, ctx) {
  const cfg = {
    repoRoot: '/home/spy/Documents/Epaflix/k3s-swarm-proxmox',
    appName: 'cnpg-operator',
    appNs: 'cnpg-system',
    clusterNs: 'postgres-system',
    operatorManifest: '2-k3s/06.postgres/operator-kustomization/cnpg-operator.yaml',
    kustomization: '2-k3s/06.postgres/operator-kustomization/kustomization.yaml',
    readme: '2-k3s/06.postgres/README.md',
    fromVersion: 'v1.28.0',
    toVersion: 'v1.29.1',
    releaseManifestUrl: 'https://github.com/cloudnative-pg/cloudnative-pg/releases/download/v1.29.1/cnpg-1.29.1.yaml',
    branch: 'cnpg-operator-bump-129',
    repo: 'SpyrosPsarras/epaflix',
    issue: '128',
    ...inputs,
  };

  ctx.log('info', `CNPG operator bump ${cfg.fromVersion} -> ${cfg.toVersion} (issue #${cfg.issue}, re-targeted from non-existent 1.30)`);

  // PHASE 1 — pre-flight diff classification (no mutation)
  const pre = await ctx.task(preflightTask, {
    repoRoot: cfg.repoRoot, appName: cfg.appName, appNs: cfg.appNs,
    operatorManifest: cfg.operatorManifest, fromVersion: cfg.fromVersion,
    toVersion: cfg.toVersion, releaseManifestUrl: cfg.releaseManifestUrl,
  });
  ctx.log('info', `Pre-flight: liveImage=${pre.liveOperatorImage}; CRD changes=${(pre.crdChanges || []).length}; v130Exists=${pre.v130Exists}; barmanOk=${pre.barmanCompatOk}`);

  if (pre.v130Exists) {
    const branchGate = await ctx.breakpoint({
      question:
        'Pre-flight reports a CNPG v1.30 release now EXISTS upstream. The issue originally asked for 1.30.\n\n' +
        pre.summary + '\n\nTarget 1.30 instead of ' + cfg.toVersion + ', or proceed with ' + cfg.toVersion + '?',
      options: ['Proceed with ' + cfg.toVersion, 'Abort (re-plan for 1.30)'],
      expert: 'owner',
      tags: ['architecture', 'approval-gate'],
    });
    if (!branchGate.approved || /abort/i.test(branchGate.response || '')) {
      return { success: false, reason: 'v1.30-now-exists-replan', pre };
    }
  }

  // PHASE 2 — re-vendor + validate + local commit (reversible, no push)
  let rev = await ctx.task(revendorTask, {
    repoRoot: cfg.repoRoot, operatorManifest: cfg.operatorManifest,
    kustomization: cfg.kustomization, readme: cfg.readme,
    fromVersion: cfg.fromVersion, toVersion: cfg.toVersion,
    branch: cfg.branch, issue: cfg.issue,
  });
  ctx.log('info', `Re-vendor: validationOk=${rev.validationOk}; files=${JSON.stringify(rev.changedFiles)}; commit=${rev.commitSha}`);

  // GATE 1 (deploy + destructive-git) — review diff/CRD churn + validation, approve push+PR+merge.
  // Merging auto-applies to the live cluster (selfHeal ON). Retry/refine loop on rejection.
  let lastFeedback = null;
  let publishApproved = false;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (lastFeedback) {
      rev = await ctx.task(revendorTask, {
        repoRoot: cfg.repoRoot, operatorManifest: cfg.operatorManifest,
        kustomization: cfg.kustomization, readme: cfg.readme,
        fromVersion: cfg.fromVersion, toVersion: cfg.toVersion,
        branch: cfg.branch, issue: cfg.issue,
        feedback: lastFeedback, attempt: attempt + 1,
      });
    }
    const gate = await ctx.breakpoint({
      question:
        'Approve PUSH + PR + MERGE of the CNPG operator bump?\n\n' +
        'Merging auto-applies to the LIVE cluster (cnpg-operator selfHeal ON + ServerSideApply, #127): ' +
        'new operator + upgraded CRDs roll out to cnpg-system and may briefly restart the controller.\n\n' +
        cfg.fromVersion + ' -> ' + cfg.toVersion + '\n' +
        'kustomize build OK: ' + rev.validationOk + (rev.validationOk ? '' : ('  ERROR: ' + (rev.validationError || ''))) + '\n' +
        'Changed files: ' + JSON.stringify(rev.changedFiles) + '\n' +
        'CRD schema changes (' + (pre.crdChanges || []).length + '): ' + JSON.stringify(pre.crdChanges) + '\n' +
        'RBAC changes: ' + JSON.stringify(pre.rbacChanges) + '\n' +
        'Risk: ' + pre.riskAssessment + '\n' +
        'Cluster baseline: ' + JSON.stringify(pre.clusterBaseline) + '\n\n' +
        'PR title: ' + rev.prTitle + '\n\nProceed to push + PR + merge?',
      options: ['Approve publish + merge', 'Request changes', 'Abort (keep local only)'],
      expert: 'owner',
      tags: ['deploy', 'destructive-git', 'approval-gate'],
      previousFeedback: lastFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    const r = (gate.response || '').toLowerCase();
    if (gate.approved && r.includes('approve')) { publishApproved = true; break; }
    if (r.includes('abort')) {
      ctx.log('warn', 'Publish aborted by owner — local branch/commit retained.');
      return { success: true, merged: false, published: false, branch: rev.branch, commitSha: rev.commitSha, reason: 'aborted-local-only', pre };
    }
    lastFeedback = gate.response || gate.feedback || 'Changes requested';
  }
  if (!publishApproved) {
    return { success: true, merged: false, published: false, reason: 'publish-not-approved-after-retries', branch: rev.branch, commitSha: rev.commitSha };
  }

  // PHASE 3 — publish + merge (deploys via selfHeal on merge)
  const pub = await ctx.task(publishMergeTask, {
    repoRoot: cfg.repoRoot, branch: rev.branch, issue: cfg.issue, repo: cfg.repo,
    approvedPrTitle: rev.prTitle, approvedPrBody: rev.prBody,
  });
  ctx.log('info', `Publish: PR=${pub.prUrl}; merged=${pub.merged}; mergeSha=${pub.mergeSha}`);
  if (!pub.merged) {
    return { success: false, merged: false, prUrl: pub.prUrl, reason: 'merge-failed', detail: pub.detail };
  }

  // PHASE 4 — reconcile + verify, with a recovery gate on regression.
  let verify = await ctx.task(verifyTask, {
    repoRoot: cfg.repoRoot, appName: cfg.appName, appNs: cfg.appNs, clusterNs: cfg.clusterNs,
    toVersion: cfg.toVersion, clusterBaseline: pre.clusterBaseline,
  });
  ctx.log('info', `Verify: healthy=${verify.healthy}; operator=${verify.operatorImage}; appSync=${verify.appSync}/${verify.appHealth}`);

  if (!verify.healthy) {
    const recover = await ctx.breakpoint({
      question:
        'Post-merge verification found problems.\n' +
        'Operator image: ' + verify.operatorImage + '\n' +
        'CRDs Established: ' + verify.crdsEstablished + '\n' +
        'App: ' + verify.appSync + '/' + verify.appHealth + '\n' +
        'Cluster health: ' + JSON.stringify(verify.clusterHealth) + '\n' +
        'Regressions: ' + JSON.stringify(verify.regressions) + '\n' +
        'Summary: ' + verify.summary + '\n\n' +
        'Rollback = revert the merge PR (selfHeal will re-apply 1.28.0). How to proceed?',
      options: ['Re-verify (transient)', 'Continue anyway (accept state)', 'Stop here (I will revert)'],
      expert: 'owner',
      tags: ['deploy', 'verification-gate'],
    });
    const resp = (recover.response || '').toLowerCase();
    if (recover.approved && resp.includes('re-verify')) {
      verify = await ctx.task(verifyTask, {
        repoRoot: cfg.repoRoot, appName: cfg.appName, appNs: cfg.appNs, clusterNs: cfg.clusterNs,
        toVersion: cfg.toVersion, clusterBaseline: pre.clusterBaseline, attempt: 2,
      });
    } else if (!recover.approved || resp.includes('stop')) {
      return { success: false, merged: true, reconciled: false, prUrl: pub.prUrl, reason: 'verification-stop', verify };
    }
    // 'Continue anyway' falls through.
  }

  // PHASE 5 — finalize: PR test plan + re-target/close issue + follow-ups.
  const fin = await ctx.task(finalizeTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo, issue: cfg.issue, toVersion: cfg.toVersion,
    prUrl: pub.prUrl, verify,
  });
  ctx.log('info', `Finalize: issue=${fin.issueFinal}; followUps=${JSON.stringify(fin.followUpIssues)}`);

  return {
    success: true,
    merged: pub.merged,
    reconciled: verify.healthy,
    prUrl: pub.prUrl,
    operatorImage: verify.operatorImage,
    clusterHealthy: !!(verify.clusterHealth),
    issueFinal: fin.issueFinal,
    followUpIssues: fin.followUpIssues,
  };
}
