/**
 * @process specializations/devops-sre-platform/issue-147-controlplane-endpoints-gitops
 * @description Deliver Epaflix issue #147: make the control-plane exporter Endpoints
 *   (kube-controller-manager / kube-scheduler / kube-etcd in kube-system, carrying master
 *   InternalIPs 10.0.0.51/52/53) durable in git so ArgoCD reconcile/selfHeal can recreate
 *   them after a delete/rebuild. Today ArgoCD's GLOBAL `resource.exclusions` drops the core
 *   `Endpoints` kind, so the chart-rendered static Endpoints were applied out-of-band during
 *   the #121 remediation and silently go dark if wiped. The Service selector-strip (so the
 *   k8s endpoint-controller stops zeroing those Endpoints) was a manual one-time step too.
 *
 *   Flow: analyze live+git state and enumerate the durable options (A: global un-exclude
 *   Endpoints; B: explicit static EndpointSlice manifests in the observability kustomization —
 *   EndpointSlice is already tracked for jellyfin-truenas, so this is scoped + k8s-1.33-future-
 *   proof; C: at minimum document the live-apply + selector-strip reproducibly in the runbook)
 *   -> OWNER picks the approach (architecture-change gate) -> implement on a branch (reversible
 *   local commit) -> validate (kustomize build / helm pins / ArgoCD diff reasoning, refine loop)
 *   -> OWNER approves push+PR+merge (destructive-git + deploy gate) -> publish following the
 *   Epaflix merge policy (rebase onto origin/main, force-with-lease, wait `validate`, gh pr
 *   merge --merge) + open follow-up issues -> verify post-merge (ArgoCD Synced/Healthy,
 *   Endpoints/EndpointSlices managed + populated, Prometheus control-plane targets UP) and
 *   execute the PR test plan by editing the PR body, then close #147.
 *
 * @inputs { repoRoot, repo, appName, appPath, appManifest, argocdValues, valuesFile,
 *           kustomization, readme, issue, branch }
 * @outputs { success, approach, prUrl, merged, verified, issue147State, followUpIssues }
 *
 * Risk: option A rewrites a CLUSTER-GLOBAL ArgoCD config (argocd-cm resource.exclusions)
 * affecting every Application + needs an argocd reconcile — gated. Any approach lands via a
 * PR whose merge triggers a live ArgoCD sync of the observability app — gated as deploy.
 * No secrets are touched (docs/manifests only); SOPS *.enc.yaml are left untouched.
 *
 * @agent general-purpose (kubectl/argocd/git/gh executor + design/validation/verification)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

// ---------------------------------------------------------------------------
// Task definitions
// ---------------------------------------------------------------------------

// Phase 1 — analyze live + git state, enumerate durable options, recommend one. No mutation.
const designTask = defineTask('design-analysis', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Analyze control-plane Endpoints state + design durable GitOps approach',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Kubernetes/ArgoCD SRE designing a GitOps-durability fix on the Epaflix k3s cluster',
      task:
        'Investigate the live + git state of the control-plane exporter Endpoints and Services and ' +
        'produce a concrete design that makes them durable in git for issue #' + args.issue + '. DO NOT mutate anything.',
      context: { ...args, feedback: args.feedback || null },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. You have kubectl + argocd CLI + gh configured. READ-ONLY this phase.',
        'LIVE: capture for kube-system the three control-plane exporter Services and Endpoints — ' +
          '`kubectl -n kube-system get svc,endpoints kube-prometheus-stack-kube-controller-manager kube-prometheus-stack-kube-scheduler kube-prometheus-stack-kube-etcd -o yaml` ' +
          '(name prefixes may differ — first `kubectl -n kube-system get svc,endpoints | grep -Ei "controller-manager|scheduler|etcd"` to find exact names). Record: does each Service still have NO spec.selector (selector-strip intact)? Do the Endpoints carry addresses 10.0.0.51/52/53 with the expected ports (10257 / 10259 / 2381)? Note managedFields field-managers on both.',
        'LIVE: confirm whether ArgoCD currently tracks these — `argocd app get ' + args.appName + ' --refresh` and look for the kube-system Endpoints/Services in the resource tree (they will likely be ABSENT because of the global Endpoints exclusion).',
        'GIT: read ' + args.argocdValues + ' (the `resource.exclusions` block — confirm it excludes core `Endpoints` cluster-wide and that EndpointSlice is NOT excluded), ' + args.valuesFile + ' (kubeControllerManager/kubeScheduler/kubeEtcd endpoints + service blocks), ' + args.kustomization + ', ' + args.appManifest + ' (selfHeal/prune/ServerSideApply + ignoreDifferences), and the jellyfin-truenas static EndpointSlice precedent (grep the repo for `kind: EndpointSlice`).',
        'KEY CONSTRAINT to verify and state plainly: ArgoCD `resource.exclusions` is CLUSTER-GLOBAL (argocd-cm) and filters only by apiGroups/kinds/clusters — it CANNOT be scoped to a single namespace or Application. So "un-exclude Endpoints" un-excludes it for EVERY app (selector-based Services across the cluster would then expose controller-managed Endpoints to ArgoCD diff/prune). Confirm this against ArgoCD docs/behavior and the live app set.',
        'Enumerate the durable options with concrete pros/cons/risk/blast-radius/effort, each tied to THIS repo: ' +
          'A) remove/narrow the global Endpoints exclusion in ' + args.argocdValues + ' (global blast radius, argocd-cm reload, prune risk to other apps — the issue explicitly prefers NOT a global un-exclude); ' +
          'B) stop relying on the chart-rendered (excluded) Endpoints and instead add explicit STATIC EndpointSlice manifest(s) for the 3 exporters into the observability kustomization (EndpointSlice is already ArgoCD-tracked via jellyfin-truenas, so this is scoped to the observability app, GitOps-durable, selfHeal-recreatable, and forward-compatible since v1 Endpoints is deprecated in k8s 1.33+). Spell out exactly how: selector-less headless Services already render from the chart in kube-system; add EndpointSlice objects (addressType IPv4, endpoints=master IPs, ports per exporter, label `kubernetes.io/service-name: <svc>` + `endpointslice.kubernetes.io/managed-by` left to us) targeting kube-system, wired through the same kustomization that already templates the exporter Services into kube-system; ' +
          'C) minimum: document the live-apply + the SSA selector-strip as a reproducible runbook step in ' + args.readme + ' so it can be reconstructed after a delete/rebuild.',
        'Also address the SECOND half of the issue: the Service selector-strip reproducibility — does the chart already render the Services selector-less when `endpoints:` is set (so git IS the source of truth and only the historical live Services needed the one-time strip), or is an explicit strip still required? State the durable answer for whichever option is chosen, and capture the exact reproducible kubectl/argocd commands.',
        'RECOMMEND one option (default lean: B — scoped, durable, future-proof — optionally + C runbook docs), with a crisp rationale and the precise file edits each option requires.',
        'Write a DESIGN doc to tasks/' + taskCtx.effectId + '/DESIGN.md capturing live state, the global-exclusion constraint, the A/B/C analysis, the recommendation, and the exact planned edits. If feedback is present in context, revise accordingly.',
        'Return ONLY the structured JSON result, not a plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['liveState', 'globalExclusionConfirmed', 'options', 'recommendation', 'selectorStripAnswer', 'designDocPath', 'summary'],
      properties: {
        liveState: { type: 'object' },
        globalExclusionConfirmed: { type: 'boolean' },
        options: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              id: { type: 'string' },
              title: { type: 'string' },
              pros: { type: 'array', items: { type: 'string' } },
              cons: { type: 'array', items: { type: 'string' } },
              risk: { type: 'string' },
              blastRadius: { type: 'string' },
              fileEdits: { type: 'array', items: { type: 'string' } },
            },
          },
        },
        recommendation: { type: 'string' },
        selectorStripAnswer: { type: 'string' },
        designDocPath: { type: 'string' },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 2 — implement the approved approach on a branch (reversible local commit, no push).
const implementTask = defineTask('implement', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Implement chosen approach + local commit on branch',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer implementing a GitOps-durability fix in the Epaflix repo',
      task:
        'Implement the OWNER-APPROVED approach (' + args.approach + ') for issue #' + args.issue + ': make it so the ' +
        'control-plane exporter Endpoints are durable in git and reconstructable by ArgoCD, then create a branch and ONE local commit. ' +
        'DO NOT push, DO NOT open a PR, DO NOT touch issues yet.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. CLAUDE.md rules: never hardcode/commit secrets; do NOT touch any *.enc.yaml; placeholders only. This change is manifests/docs only.',
        'Implement EXACTLY the approved approach described in context (approach=' + args.approach + ', design notes in approvedDesign). Honor any ownerNotes in context verbatim.',
        'If approach B (explicit EndpointSlice): add static EndpointSlice manifest(s) for kube-controller-manager / kube-scheduler / kube-etcd into the observability kustomization (' + args.kustomization + ' resources list), addressType IPv4, endpoints = the master InternalIPs 10.0.0.51/52/53, one EndpointSlice per exporter Service in namespace kube-system, labeled `kubernetes.io/service-name: <exact chart Service name>` and ports matching 10257/10259/2381 (named to match the Service port names the ServiceMonitor scrapes). Ensure the chart-rendered (excluded) Endpoints do not conflict — if both exist for the same Service, prefer the EndpointSlice path and document why the legacy Endpoints stay chart-side/ignored. Verify the headless Services render selector-less.',
        'If approach A (global un-exclude): edit ' + args.argocdValues + ' resource.exclusions to stop excluding core Endpoints (or narrow it), updating the explanatory comment to reflect the new global behavior and the prune implications for other apps. Flag clearly that this needs an argocd-cm reload on sync.',
        'If approach C (runbook only): document in ' + args.readme + ' the reproducible live-apply + SSA selector-strip procedure (exact kubectl/argocd commands, expected output) so the Endpoints + selector-strip can be reconstructed after a delete/rebuild.',
        'In ALL approaches: update the ' + args.readme + ' runbook with a short "Control-plane exporter Endpoints" section describing the chosen durability mechanism, the selector-strip answer, and how to verify/reconstruct. Keep the existing kustomization.yaml header rationale consistent.',
        'Validate your own edits before committing: `kustomize build --enable-helm --enable-alpha-plugins --enable-exec ' + args.appPath + '` must still succeed (ksops decryption may need the cluster age key locally — if it errors only on SOPS/ksops decryption, note it and rely on the CI `validate` gate; the manifest/structure must otherwise be clean). Also `yamllint`/`kubectl --dry-run=client` the new manifests if tools exist.',
        'If feedback is present in context (a prior validation failure or breakpoint rejection), incorporate it.',
        'Create branch ' + args.branch + ' off origin/main (fetch first; if the branch exists, reuse it). Stage ONLY the intended files. Make ONE commit. End the commit message body with the trailer: Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>. Reference #' + args.issue + ' (and cross-link #121 / #148 / #53 where apt). Do NOT push.',
        'Draft the PR title/body (## Summary + ## Test plan checklist with concrete verification boxes: ArgoCD observability Synced/Healthy, Endpoints/EndpointSlices managed+populated 10.0.0.51/52/53, Prometheus kube-controller-manager/scheduler/etcd targets UP, simulate-delete-and-confirm-selfHeal-recreates) and a follow-up-issues plan (e.g. EndpointSlice migration if not chosen now, soak/flip, link #148 master config drift).',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'changedFiles', 'commitSha', 'localBuildOk', 'prTitle', 'prBody', 'followUpPlan', 'summary'],
      properties: {
        branch: { type: 'string' },
        changedFiles: { type: 'array', items: { type: 'string' } },
        commitSha: { type: 'string' },
        localBuildOk: { type: 'boolean' },
        prTitle: { type: 'string' },
        prBody: { type: 'string' },
        followUpPlan: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 3 — validate the change (build + ArgoCD diff reasoning + secret-safety). No push.
const validateTask = defineTask('validate', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Validate render + ArgoCD reconcile reasoning + safety',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Reviewer validating a GitOps change before it is published',
      task:
        'Validate the committed change on branch ' + args.branch + ' for issue #' + args.issue + ': it renders, it is secret-safe, ' +
        'and it actually achieves GitOps durability for the control-plane Endpoints without unintended blast radius. DO NOT push.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + ' on branch ' + args.branch + '.',
        'BUILD: run the CI-equivalent local validation that .github/workflows/ci.yml performs (read that file for exact steps). At minimum `kustomize build --enable-helm --enable-alpha-plugins --enable-exec ' + args.appPath + '` and any helm-pin / yaml checks. A SOPS/ksops decryption error due to a missing local age key is acceptable (CI validates without the key) — but any structural/kustomize/helm error is a FAIL.',
        'CORRECTNESS: confirm the rendered output now contains the desired durable objects (e.g. the EndpointSlices for approach B, or the edited exclusions for approach A) with the correct master IPs 10.0.0.51/52/53 and ports, in kube-system, correctly associated to the exporter Services.',
        'ARGOCD REASONING: reason precisely about what ArgoCD will do on sync — will the new objects now be IN the observability app resource tree (no longer excluded)? For approach A, enumerate the cluster-wide side effects (which other apps would suddenly see Endpoints diffs / prune candidates) and whether that is acceptable. Confirm no prune will delete something live and needed.',
        'SAFETY: grep the diff to confirm NO secrets and NO plaintext kind: Secret were added, no *.enc.yaml was modified, and the pre-commit hook would pass.',
        'Set ok=true ONLY if build passes (modulo the allowed local SOPS decryption gap) AND correctness holds AND no unsafe blast radius. Otherwise ok=false with precise, actionable failures.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['ok', 'buildResult', 'correctness', 'argocdImpact', 'safety', 'failures', 'summary'],
      properties: {
        ok: { type: 'boolean' },
        buildResult: { type: 'string' },
        correctness: { type: 'string' },
        argocdImpact: { type: 'string' },
        safety: { type: 'string' },
        failures: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 4 — publish: rebase, push, open PR, wait validate, optionally merge, open follow-ups.
const publishTask = defineTask('publish', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Rebase + push + PR + (merge) + follow-up issues',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer publishing an OWNER-APPROVED change to SpyrosPsarras/epaflix',
      task:
        'Execute the approved publish plan for issue #' + args.issue + ' following the Epaflix merge policy, and open the approved follow-up issues. ' +
        'mergeAfterValidate=' + String(args.mergeAfterValidate) + '.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Epaflix merge policy (merge-commit + mandatory rebase, semi-linear; PR required, 0 approvals): rebase ' + args.branch + ' onto origin/main (`git fetch origin && git rebase origin/main`), then `git push --force-with-lease`.',
        'Open a PR to main with the approved title/body (context: approvedPrTitle / approvedPrBody). Cross-link #' + args.issue + ' and related #121 / #148 / #53.',
        'Wait for the required `validate` check: poll `gh pr checks <pr> --watch` (or `gh run watch`). If `validate` fails on the known unpinned-kustomize rate-limit flake (see repo notes), `gh run rerun --failed` once. If it fails for a real reason, do NOT merge — capture the failure and return merged=false with detail.',
        args.mergeAfterValidate
          ? 'Once `validate` is green and the branch is up-to-date with main, merge: `gh pr merge <pr> --merge`. Confirm the Merge-PR marker landed on main.'
          : 'Do NOT merge — leave the PR open for the owner to merge manually. Return merged=false with the PR url and check status.',
        'Open the approved follow-up issues on SpyrosPsarras/epaflix using the enhancement shape (## Finding / ## Current state / ## Desired outcome / ## Notes), per the approvedFollowUps list in context, cross-linking #' + args.issue + '.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'pushed', 'validateStatus', 'merged', 'followUpIssues', 'detail'],
      properties: {
        prUrl: { type: 'string' },
        pushed: { type: 'boolean' },
        validateStatus: { type: 'string' },
        merged: { type: 'boolean' },
        followUpIssues: { type: 'array', items: { type: 'string' } },
        detail: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// Phase 5 — verify post-merge live state + execute the PR test plan + close the issue.
const verifyTask = defineTask('verify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify live reconcile + execute PR test plan + close #' + args.issue,
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'ArgoCD/Prometheus SRE verifying a delivered GitOps change',
      task:
        'Verify issue #' + args.issue + ' is actually delivered against LIVE cluster state, record the PR test-plan outcomes by ' +
        'EDITING the PR description (never a new comment), and close #' + args.issue + ' if fully satisfied.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. You have kubectl + argocd + gh.',
        'If the PR was merged: trigger/await reconcile — `argocd app sync ' + args.appName + '` (or wait for auto-sync) then `argocd app get ' + args.appName + '` is Synced + Healthy.',
        'Confirm the durable objects are now GitOps-managed and populated: for approach B, the EndpointSlices appear in the ArgoCD ' + args.appName + ' resource tree and carry 10.0.0.51/52/53; for approach A, the chart Endpoints now appear in the tree. `kubectl -n kube-system get endpointslices,endpoints | grep -Ei "controller-manager|scheduler|etcd"` and confirm addresses + ports.',
        'Confirm Prometheus control-plane targets are UP: query the Prometheus targets/API for kube-controller-manager, kube-scheduler, kube-etcd jobs (port-forward + `/api/v1/targets` or via Grafana) — all three should report health=up.',
        'DURABILITY check (only if safe & approved by the test plan): optionally delete one managed EndpointSlice/Endpoints and confirm ArgoCD selfHeal recreates it, then leave state clean. If too risky to do live, mark that box as deferred with a note instead.',
        'Execute every unchecked box in the PR test plan. Record outcomes by EDITING the PR body (tick boxes, append inline results) via `gh pr edit` — NEVER add a new PR comment. Strike through any step no longer applicable with a reason.',
        'If everything passes, close issue #' + args.issue + ' with a short summary comment referencing the merged PR. If something fails, leave it open and report precisely.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['verified', 'appSynced', 'appHealthy', 'endpointsManaged', 'prometheusTargetsUp', 'testPlanRecorded', 'issueClosed', 'failures', 'summary'],
      properties: {
        verified: { type: 'boolean' },
        appSynced: { type: 'boolean' },
        appHealthy: { type: 'boolean' },
        endpointsManaged: { type: 'boolean' },
        prometheusTargetsUp: { type: 'boolean' },
        testPlanRecorded: { type: 'boolean' },
        issueClosed: { type: 'boolean' },
        failures: { type: 'array', items: { type: 'string' } },
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
    repo: 'SpyrosPsarras/epaflix',
    appName: 'observability',
    appPath: '2-k3s/10.observability',
    appManifest: '2-k3s/11.argocd/apps/app-observability.yaml',
    argocdValues: '2-k3s/11.argocd/helm-values.yaml',
    valuesFile: '2-k3s/10.observability/prometheus-values.yaml',
    kustomization: '2-k3s/10.observability/kustomization.yaml',
    readme: '2-k3s/10.observability/README.md',
    issue: '147',
    branch: 'issue-147-controlplane-endpoints-gitops',
    ...inputs,
  };

  ctx.log('info', 'Issue #147 — control-plane exporter Endpoints GitOps durability');

  // PHASE 1 — design analysis (read-only), with an owner refine loop available.
  let design = await ctx.task(designTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo, appName: cfg.appName, appPath: cfg.appPath,
    appManifest: cfg.appManifest, argocdValues: cfg.argocdValues, valuesFile: cfg.valuesFile,
    kustomization: cfg.kustomization, readme: cfg.readme, issue: cfg.issue,
  });
  ctx.log('info', `Design: recommend=${design.recommendation}; options=${(design.options || []).map(o => o.id).join('/')}`);

  // GATE 1 (architecture-change) — owner picks the durable approach.
  let approach = null;
  let ownerNotes = '';
  let lastFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (lastFeedback) {
      design = await ctx.task(designTask, {
        repoRoot: cfg.repoRoot, repo: cfg.repo, appName: cfg.appName, appPath: cfg.appPath,
        appManifest: cfg.appManifest, argocdValues: cfg.argocdValues, valuesFile: cfg.valuesFile,
        kustomization: cfg.kustomization, readme: cfg.readme, issue: cfg.issue,
        feedback: lastFeedback,
      });
    }
    const optLines = (design.options || []).map(o =>
      `  ${o.id}) ${o.title}\n     risk: ${o.risk} | blast: ${o.blastRadius}\n     edits: ${JSON.stringify(o.fileEdits)}`
    ).join('\n');
    const designGate = await ctx.breakpoint({
      question:
        'ARCHITECTURE DECISION — issue #147: make the control-plane exporter Endpoints durable in git.\n\n' +
        'Global-exclusion constraint confirmed: ' + design.globalExclusionConfirmed + ' (resource.exclusions is cluster-global, cannot scope per-app/namespace).\n' +
        'Selector-strip answer: ' + design.selectorStripAnswer + '\n\n' +
        'Options:\n' + optLines + '\n\n' +
        'Recommendation: ' + design.recommendation + '\n\n' +
        'Summary: ' + design.summary + '\n\n' +
        'Which approach should I implement? (Reply with A, B, or C — optionally add notes, e.g. "B + also update runbook".)',
      options: ['A — global un-exclude Endpoints', 'B — explicit static EndpointSlices (recommended)', 'C — document runbook only', 'Request changes to the analysis'],
      expert: 'owner',
      tags: ['architecture-change', 'approval-gate'],
      previousFeedback: lastFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    if (!designGate.approved) {
      const r = (designGate.response || '').toLowerCase();
      if (r.includes('request') || r.includes('change')) {
        lastFeedback = designGate.feedback || designGate.response || 'Revise the analysis';
        continue;
      }
      ctx.log('warn', 'Design not approved — aborting before any change.');
      return { success: false, reason: 'design-not-approved', design };
    }
    const resp = (designGate.response || '') + ' ' + (designGate.feedback || '');
    ownerNotes = designGate.feedback || designGate.response || '';
    if (/\bA\b|un-exclude/i.test(resp)) approach = 'A';
    else if (/\bB\b|endpointslice/i.test(resp)) approach = 'B';
    else if (/\bC\b|runbook|document/i.test(resp)) approach = 'C';
    else { lastFeedback = 'Could not parse the chosen approach — please reply A, B, or C.'; continue; }
    break;
  }
  if (!approach) {
    return { success: false, reason: 'approach-not-selected', design };
  }
  ctx.log('info', `Approved approach: ${approach}`);

  // PHASE 2 + 3 — implement on a branch, then validate; refine loop on validation failure.
  let impl = null;
  let validation = null;
  let implFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    impl = await ctx.task(implementTask, {
      repoRoot: cfg.repoRoot, appName: cfg.appName, appPath: cfg.appPath, kustomization: cfg.kustomization,
      argocdValues: cfg.argocdValues, valuesFile: cfg.valuesFile, readme: cfg.readme, issue: cfg.issue,
      branch: cfg.branch, approach, approvedDesign: design, ownerNotes,
      feedback: implFeedback || undefined, attempt: attempt + 1,
    });
    validation = await ctx.task(validateTask, {
      repoRoot: cfg.repoRoot, appName: cfg.appName, appPath: cfg.appPath, branch: cfg.branch, issue: cfg.issue,
    });
    ctx.log('info', `Validate attempt ${attempt + 1}: ok=${validation.ok}`);
    if (validation.ok) break;
    implFeedback = 'Validation failed: ' + JSON.stringify(validation.failures) + ' — ' + validation.summary;
  }

  // If validation still failing, ask the owner how to proceed.
  if (!validation.ok) {
    const vGate = await ctx.breakpoint({
      question:
        'Validation did not pass after retries.\n' +
        'Failures: ' + JSON.stringify(validation.failures) + '\n' +
        'Build: ' + validation.buildResult + '\n' +
        'Summary: ' + validation.summary + '\n\n' +
        'How do you want to proceed?',
      options: ['Retry implementation once more', 'Proceed to publish anyway (accept)', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (vGate.response || '').toLowerCase();
    if (vGate.approved && r.includes('retry')) {
      impl = await ctx.task(implementTask, {
        repoRoot: cfg.repoRoot, appName: cfg.appName, appPath: cfg.appPath, kustomization: cfg.kustomization,
        argocdValues: cfg.argocdValues, valuesFile: cfg.valuesFile, readme: cfg.readme, issue: cfg.issue,
        branch: cfg.branch, approach, approvedDesign: design, ownerNotes,
        feedback: 'FINAL retry. ' + (implFeedback || ''), attempt: 99,
      });
      validation = await ctx.task(validateTask, {
        repoRoot: cfg.repoRoot, appName: cfg.appName, appPath: cfg.appPath, branch: cfg.branch, issue: cfg.issue,
      });
    } else if (!vGate.approved || r.includes('stop')) {
      return { success: false, reason: 'validation-stop', approach, validation, branch: cfg.branch, commitSha: impl.commitSha };
    }
    // 'Proceed anyway' falls through.
  }

  // GATE 2 (destructive-git + deploy) — approve push + PR + (merge); refine loop.
  let mergeAfterValidate = false;
  let publishApproved = false;
  let pubFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (pubFeedback) {
      impl = await ctx.task(implementTask, {
        repoRoot: cfg.repoRoot, appName: cfg.appName, appPath: cfg.appPath, kustomization: cfg.kustomization,
        argocdValues: cfg.argocdValues, valuesFile: cfg.valuesFile, readme: cfg.readme, issue: cfg.issue,
        branch: cfg.branch, approach, approvedDesign: design, ownerNotes,
        feedback: pubFeedback, attempt: attempt + 1,
      });
    }
    const pubGate = await ctx.breakpoint({
      question:
        'PUBLISH + DEPLOY GATE — approach ' + approach + ', issue #147.\n\n' +
        'Branch: ' + impl.branch + ' (commit ' + impl.commitSha + ', localBuildOk=' + impl.localBuildOk + ')\n' +
        'Changed files: ' + JSON.stringify(impl.changedFiles) + '\n' +
        'Validation: ok=' + validation.ok + ' — ' + validation.summary + '\n' +
        'ArgoCD impact: ' + validation.argocdImpact + '\n\n' +
        'PR title: ' + impl.prTitle + '\n' +
        'PR body:\n' + impl.prBody + '\n\n' +
        'Follow-up issues planned: ' + JSON.stringify(impl.followUpPlan) + '\n\n' +
        'Merging triggers a live ArgoCD sync of the observability app' +
        (approach === 'A' ? ' AND a cluster-global argocd-cm resource.exclusions change affecting every Application.' : '.') + '\n' +
        'Approve to push + open PR. Choose whether I also merge automatically once `validate` is green.',
      options: ['Approve: push + PR + auto-merge when green', 'Approve: push + PR only (I will merge)', 'Request changes', 'Abort'],
      expert: 'owner',
      tags: ['destructive-git', 'deploy', 'approval-gate'],
      previousFeedback: pubFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    const r = (pubGate.response || '').toLowerCase();
    if (pubGate.approved && r.includes('approve')) {
      mergeAfterValidate = r.includes('auto-merge') || r.includes('when green');
      publishApproved = true;
      break;
    }
    if (r.includes('abort')) {
      return { success: false, reason: 'publish-aborted', approach, branch: impl.branch, commitSha: impl.commitSha, validation };
    }
    pubFeedback = pubGate.feedback || pubGate.response || 'Changes requested';
  }
  if (!publishApproved) {
    return { success: false, reason: 'publish-not-approved-after-retries', approach, branch: impl.branch, commitSha: impl.commitSha };
  }

  // PHASE 4 — publish.
  const pub = await ctx.task(publishTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo, appName: cfg.appName, branch: cfg.branch, issue: cfg.issue,
    approvedPrTitle: impl.prTitle, approvedPrBody: impl.prBody, approvedFollowUps: impl.followUpPlan,
    mergeAfterValidate,
  });
  ctx.log('info', `Published: PR=${pub.prUrl}; validate=${pub.validateStatus}; merged=${pub.merged}`);

  // If we did not merge (owner chose manual-merge, or merge blocked), stop cleanly here.
  if (!pub.merged) {
    return {
      success: true, approach, prUrl: pub.prUrl, merged: false, verified: false,
      reason: mergeAfterValidate ? 'merge-blocked-or-validate-failed' : 'manual-merge-chosen',
      validateStatus: pub.validateStatus, detail: pub.detail, followUpIssues: pub.followUpIssues,
      issue147State: 'open-pending-merge',
    };
  }

  // PHASE 5 — verify post-merge + execute PR test plan + close issue.
  let verify = await ctx.task(verifyTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo, appName: cfg.appName, issue: cfg.issue, prUrl: pub.prUrl, approach,
  });
  if (!verify.verified) {
    const vfGate = await ctx.breakpoint({
      question:
        'Post-merge verification incomplete.\n' +
        'Synced: ' + verify.appSynced + ' | Healthy: ' + verify.appHealthy + '\n' +
        'Endpoints managed: ' + verify.endpointsManaged + ' | Prometheus targets up: ' + verify.prometheusTargetsUp + '\n' +
        'Failures: ' + JSON.stringify(verify.failures) + '\n' +
        'Summary: ' + verify.summary + '\n\n' +
        'How do you want to proceed?',
      options: ['Re-verify (transient/propagation)', 'Accept current state', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (vfGate.response || '').toLowerCase();
    if (vfGate.approved && r.includes('re-verify')) {
      verify = await ctx.task(verifyTask, {
        repoRoot: cfg.repoRoot, repo: cfg.repo, appName: cfg.appName, issue: cfg.issue, prUrl: pub.prUrl, approach, attempt: 2,
      });
    } else if (!vfGate.approved || r.includes('stop')) {
      return { success: true, approach, prUrl: pub.prUrl, merged: true, verified: false, reason: 'verify-stop', verify, followUpIssues: pub.followUpIssues };
    }
  }

  return {
    success: true,
    approach,
    prUrl: pub.prUrl,
    merged: true,
    verified: verify.verified,
    issue147State: verify.issueClosed ? 'closed' : 'open',
    followUpIssues: pub.followUpIssues,
    summary: verify.summary,
  };
}
