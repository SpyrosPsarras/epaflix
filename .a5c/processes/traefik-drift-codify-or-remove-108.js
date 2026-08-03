/**
 * @process specializations/devops-sre-platform/iac-implementation
 * @description Resolve Epaflix issue #108: the live IngressRoute/https-redirect-epaflix in
 *   namespace traefik-system is an untracked orphan — present in the cluster (~103d old,
 *   imperatively applied 2026-02-17) but NOT in git (2-k3s/05.traefik-deployment) and NOT
 *   ArgoCD-tracked (no app.kubernetes.io/instance / argocd tracking-id). It is a catch-all
 *   HTTP→HTTPS redirect on the `web` entrypoint matching `HostRegexp({subdomain}.epaflix.com)
 *   || Host(epaflix.com)`, routing through the in-git `redirect-https` Middleware to
 *   noop@internal. Because the traefik Application now runs prune:true (#51, PR #107) but
 *   prune ignores UNTRACKED resources, the orphan survives — leaving traefik-system with
 *   non-zero untracked drift.
 *
 *   Decision lens (codify vs remove): the Helm values (values/traefik-values.yaml) already
 *   configure an ENTRYPOINT-LEVEL redirect — `ports.web.http.redirections.entryPoint`
 *   (permanent, scheme:https, to:websecure) — which redirects ALL :80 traffic to :443
 *   globally, before any IngressRoute match. That makes the catch-all IngressRoute
 *   REDUNDANT. Recommended path: (b) DELETE the live orphan + add a short doc note so it is
 *   not recreated. Alternative path: (a) CODIFY it into the git source so ArgoCD adopts and
 *   tracks it. The owner chooses at a mandatory destructive/deploy gate.
 *
 *   Flow: analyze+decide (read-only, prove redundancy and that no host depends solely on the
 *   catch-all) → owner gate (delete | codify | abort) → execute chosen path (DELETE: imperative
 *   kubectl delete of the untracked resource — safe & permanent because prune/selfHeal ignore
 *   untracked, plus a doc-note branch; CODIFY: author the IngressRoute manifest + kustomization
 *   entry on a branch) → push+PR+merge per Epaflix merge policy → post verify (HTTP→HTTPS still
 *   works via the entrypoint redirect; traefik-system has zero untracked drift; orphan gone or
 *   now ArgoCD-tracked) → closeout (#108, PR test plan, follow-ups).
 *
 * @inputs { repoRoot, orphanKind, orphanName, ns, appName, appPath, kustomization, valuesFile, masterSsh, issue, repo, branch }
 * @outputs { success, decision, deletedLive, codified, merged, prUrl, zeroDrift, httpsRedirectWorks, issueState, followUpIssue }
 *
 * Local render caveat: the orchestrator host has no cluster context; kubectl is over SSH to a
 * master (masterSsh). Drift/health is read via ArgoCD's own live status + direct kubectl,
 * never a local render.
 *
 * @agent general-purpose (kubectl-over-ssh / curl / git / gh executor + classification & verification)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';

// ---------------------------------------------------------------------------
// Phase 1 — analyze + decide (READ-ONLY, no mutation).
// ---------------------------------------------------------------------------
const analyzeDecideTask = defineTask('analyze-decide', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Confirm the orphan is untracked, prove HTTP→HTTPS redundancy via the entrypoint-level redirect, and recommend delete-or-codify',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Kubernetes/Traefik/ArgoCD SRE on the Epaflix k3s cluster',
      task:
        'Establish the ground truth for issue #' + args.issue + ': prove that `' + args.orphanKind + '/' +
        args.orphanName + '` in namespace ' + args.ns + ' is an UNTRACKED orphan, determine whether its ' +
        'HTTP→HTTPS function is REDUNDANT with the entrypoint-level redirect configured in the Traefik ' +
        'Helm values, confirm NO host would lose its HTTP→HTTPS redirect if the orphan were deleted, and ' +
        'recommend delete-or-codify with rationale. DO NOT change anything.',
      context: { ...args },
      instructions: [
        'kubectl access is over SSH: prefix every cluster command with `' + args.masterSsh + ' \'<kubectl ...>\'`. Run git/file reads locally from repoRoot=' + args.repoRoot + '.',
        'ORPHAN STATE: `' + args.masterSsh + " 'kubectl -n " + args.ns + ' get ' + args.orphanKind.toLowerCase() + ' ' + args.orphanName + " -o json'`. Record spec (entryPoints, the match rule, middlewares, services) and confirm it is UNTRACKED: no app.kubernetes.io/instance label and no argocd.argoproj.io/tracking-id annotation. Confirm it is NOT in the traefik Application `.status.resources[]` (`" + args.masterSsh + " 'kubectl -n argocd get application " + args.appName + " -o json'`).",
        'GIT ABSENCE: confirm no manifest under ' + args.appPath + ' defines this IngressRoute (grep for `' + args.orphanName + '` and for an IngressRoute on the `web` entrypoint with a HostRegexp catch-all). The in-git `redirect-https` Middleware (' + args.appPath + '/middleware/redirect-https.yaml) exists and is referenced by the orphan — note that the Middleware itself is tracked and must NOT be touched.',
        'ENTRYPOINT-REDIRECT PROOF (the crux): read ' + args.valuesFile + ' and confirm `ports.web.http.redirections.entryPoint` is set (permanent:true, scheme:https, to:websecure). Then confirm it is LIVE in the running Traefik static config — e.g. inspect the traefik pod args/config or the rendered command (`' + args.masterSsh + " 'kubectl -n " + args.ns + " get pods -l app.kubernetes.io/name=traefik -o yaml'` and look for the entrypoint redirect flags/config). An entrypoint-level redirect rewrites ALL :80 traffic to :443 before routing, which makes a catch-all redirect IngressRoute redundant.",
        'LIVE BEHAVIOUR TEST (read-only): from a master, curl an http URL and confirm a 301/308 redirect to https is returned even for a host that has NO dedicated *-http IngressRoute, proving the redirect comes from the entrypoint and not the orphan. Example: `' + args.masterSsh + " 'curl -sS -o /dev/null -w \"%{http_code} %{redirect_url}\\n\" -H \"Host: nonexistent-" + args.issue + ".epaflix.com\" http://192.168.10.101/'`. A 30x→https response with no matching IngressRoute demonstrates the entrypoint redirect is doing the work.",
        'DEPENDENCY CHECK: enumerate IngressRoutes on the `web` entrypoint across namespaces (`' + args.masterSsh + " 'kubectl get ingressroute -A -o json'`). Determine whether any host relies SOLELY on the catch-all orphan for its HTTP→HTTPS redirect. Given an active entrypoint-level redirect, the answer should be NONE — but verify and list any exception.",
        'RECOMMENDATION: set recommendation=`delete` if (orphan is untracked) AND (entrypoint-level redirect is configured in values AND live) AND (the live curl test shows redirect works without the orphan) AND (no host depends solely on the orphan). Otherwise recommendation=`codify` with the specific reason it is NOT safe to delete (e.g. entrypoint redirect not actually live, or a host depends on it).',
        'Return ONLY the structured JSON result, not a plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['recommendation', 'orphanUntracked', 'inGit', 'entrypointRedirectConfigured', 'entrypointRedirectLive', 'liveRedirectTest', 'hostsDependingSolelyOnOrphan', 'rationale', 'summary'],
      properties: {
        recommendation: { type: 'string', enum: ['delete', 'codify'] },
        orphanUntracked: { type: 'boolean' },
        inGit: { type: 'boolean' },
        entrypointRedirectConfigured: { type: 'boolean' },
        entrypointRedirectLive: { type: 'boolean' },
        liveRedirectTest: { type: 'string' },
        hostsDependingSolelyOnOrphan: { type: 'array', items: { type: 'string' } },
        rationale: { type: 'string' },
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
// Phase 2a (DELETE path) — imperatively delete the untracked live orphan.
// Safe & permanent: prune ignores untracked; selfHeal only manages tracked resources.
// ---------------------------------------------------------------------------
const deleteLiveTask = defineTask('delete-live', (args, taskCtx) => ({
  kind: 'agent',
  title: 'kubectl delete the untracked orphan IngressRoute (owner-approved, destructive)',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE executing an owner-approved destructive cleanup on the Epaflix k3s cluster',
      task:
        'Delete the untracked live `' + args.orphanKind + '/' + args.orphanName + '` from namespace ' +
        args.ns + '. This was explicitly approved at the deploy/destructive gate. Capture a backup of the ' +
        'resource YAML first so it can be re-applied if needed.',
      context: { ...args },
      instructions: [
        'kubectl access is over SSH: prefix commands with `' + args.masterSsh + ' \'<kubectl ...>\'`.',
        'BACKUP FIRST: capture the full resource YAML to stdout (`' + args.masterSsh + " 'kubectl -n " + args.ns + ' get ' + args.orphanKind.toLowerCase() + ' ' + args.orphanName + " -o yaml'`) and include it VERBATIM in the result `backupYaml` field, so the delete is reversible.",
        'DELETE: `' + args.masterSsh + " 'kubectl -n " + args.ns + ' delete ' + args.orphanKind.toLowerCase() + ' ' + args.orphanName + "'`.",
        'CONFIRM GONE: re-get and confirm a NotFound (`' + args.masterSsh + " 'kubectl -n " + args.ns + ' get ' + args.orphanKind.toLowerCase() + ' ' + args.orphanName + " 2>&1'` should report NotFound).",
        'Do NOT touch the `redirect-https` Middleware or any other resource. Do NOT git-commit here.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['deleted', 'confirmedGone', 'backupYaml'],
      properties: {
        deleted: { type: 'boolean' },
        confirmedGone: { type: 'boolean' },
        backupYaml: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// Phase 2b (DELETE path) — doc-note branch + commit (no push), so the orphan is not recreated.
// ---------------------------------------------------------------------------
const prepareDocNoteTask = defineTask('prepare-doc-note', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Author a short doc note (entrypoint redirect supersedes per-route redirects) + branch + local commit',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer documenting a settled drift-cleanup decision in the Epaflix repo',
      task:
        'Add a short, accurate doc note in the traefik deployment docs recording that the entrypoint-level ' +
        'HTTP→HTTPS redirect (ports.web.http.redirections.entryPoint in values/traefik-values.yaml) is the ' +
        'global redirect, so a catch-all redirect IngressRoute (the deleted ' + args.orphanName + ') is ' +
        'REDUNDANT and must NOT be recreated. Then branch + one local commit. Do NOT push or open a PR.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Respect CLAUDE.md (no secrets; docs-only change).',
        'Choose the most fitting existing doc under ' + args.appPath + ' (README.md and/or QUICKSTART.md). Add a concise note in the redirect/middleware section: the `web` entrypoint performs the global HTTP→HTTPS redirect (ports.web.http.redirections.entryPoint), so per-host or catch-all redirect IngressRoutes are unnecessary; the previously-orphaned `' + args.orphanName + '` catch-all (untracked, imperatively applied) was removed on cleanup of issue #' + args.issue + '. Keep wording tight and factual; do not invent config that is not present.',
        'Do NOT add the IngressRoute back. Do NOT change kustomization.yaml or any manifest. Docs only.',
        'If feedback is present in context (a prior breakpoint rejection), incorporate it.',
        'Create branch ' + args.branch + ' off origin/main (fetch first; reuse if it exists). Stage ONLY the doc file(s) changed. ONE commit referencing #' + args.issue + ' (suggested subject: `docs(traefik): note entrypoint-level https redirect supersedes catch-all IngressRoute (#' + args.issue + ')`). End the commit body with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.',
        'Return ONLY the structured JSON result. The prBody MUST include a Test Plan section with checkbox items for the post-merge verification (orphan gone from traefik-system; HTTP→HTTPS still returns 30x→https; traefik-system has zero untracked drift; traefik app Synced+Healthy).',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'changedFiles', 'commitSha', 'diff', 'prTitle', 'prBody'],
      properties: {
        branch: { type: 'string' },
        changedFiles: { type: 'array', items: { type: 'string' } },
        commitSha: { type: 'string' },
        diff: { type: 'string' },
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
// Phase 2 (CODIFY path) — author the IngressRoute manifest + kustomization entry + branch + commit.
// ---------------------------------------------------------------------------
const prepareCodifyTask = defineTask('prepare-codify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Author the orphan IngressRoute as a git manifest + add to kustomization + branch + local commit',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer codifying a live resource into the GitOps source so ArgoCD adopts it',
      task:
        'Write a manifest under ' + args.appPath + '/ingress that reproduces the live `' + args.orphanKind + '/' +
        args.orphanName + '` EXACTLY (so ArgoCD adopts the existing live object with no diff), add it to ' +
        args.kustomization + ', then branch + one local commit. Do NOT push or open a PR.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Respect CLAUDE.md (no secrets).',
        'Use the live spec captured in context (liveSpec) as the source of truth. Create ' + args.appPath + '/ingress/https-redirect.yaml with apiVersion traefik.io/v1alpha1, kind IngressRoute, metadata.name=' + args.orphanName + ' (namespace is set by the kustomization `namespace:` field — do not hardcode a conflicting one), and the SAME spec: entryPoints [web], the HostRegexp catch-all route, middlewares [redirect-https], services [noop@internal TraefikService]. Strip live-only metadata (resourceVersion, uid, creationTimestamp, generation, last-applied-configuration annotation).',
        'Add `- ingress/https-redirect.yaml` to the `resources:` list in ' + args.kustomization + ' (alongside the other ingress/*.yaml entries).',
        'Validate locally if possible: `kustomize build --enable-helm ' + args.appPath + '` should render without error and include the new IngressRoute. (If kustomize/helm plugins are unavailable locally, at minimum `kubectl --dry-run=client` or a YAML lint; note what was run.)',
        'If feedback is present in context (a prior breakpoint rejection), incorporate it.',
        'Create branch ' + args.branch + ' off origin/main (fetch first; reuse if it exists). Stage ONLY the new manifest + kustomization.yaml. ONE commit referencing #' + args.issue + ' (suggested subject: `feat(traefik): codify https-redirect-epaflix catch-all IngressRoute into git (#' + args.issue + ')`). End the commit body with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.',
        'Return ONLY the structured JSON result. The prBody MUST include a Test Plan section with checkbox items for the post-merge verification (ArgoCD adopts the IngressRoute → it appears in traefik app .status.resources[] with tracking labels; traefik-system has zero untracked drift; HTTP→HTTPS still returns 30x→https; traefik app Synced+Healthy).',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'changedFiles', 'commitSha', 'diff', 'renderOk', 'prTitle', 'prBody'],
      properties: {
        branch: { type: 'string' },
        changedFiles: { type: 'array', items: { type: 'string' } },
        commitSha: { type: 'string' },
        diff: { type: 'string' },
        renderOk: { type: 'boolean' },
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
// Phase 3 — push + PR + merge per Epaflix merge policy.
// ---------------------------------------------------------------------------
const publishMergeTask = defineTask('publish-merge', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Push branch, open PR, rebase + merge per Epaflix policy',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer publishing an OWNER-APPROVED change to SpyrosPsarras/epaflix',
      task:
        'Push the branch, open a PR, and merge it per the Epaflix policy (merge-commit + mandatory rebase / ' +
        'semi-linear, PR required, 0 approvals).',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Rebase branch ' + args.branch + ' onto origin/main and `git push --force-with-lease` (strict up-to-date + required `validate` check block stale branches — see feedback_epaflix_merge_policy).',
        'Open a PR to main with the approved title/body (context: approvedPrTitle / approvedPrBody). Cross-link issue #' + args.issue + '.',
        'Wait for the required `validate` check to pass, then merge with `gh pr merge --merge` (merge commit — never squash/rebase-merge).',
        'Capture the PR URL and merge commit SHA. Confirm the PR is MERGED before returning.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'merged', 'mergeSha'],
      properties: {
        prUrl: { type: 'string' },
        merged: { type: 'boolean' },
        mergeSha: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// ---------------------------------------------------------------------------
// Phase 4 — post-merge verification (zero drift + redirect still works).
// ---------------------------------------------------------------------------
const postVerifyTask = defineTask('post-verify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify zero untracked drift in traefik-system, HTTP→HTTPS still works, traefik app Synced+Healthy',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'ArgoCD/Traefik SRE verifying a drift-cleanup outcome',
      task:
        'Confirm issue #' + args.issue + ' is resolved per the chosen decision (' + args.decision + '): the ' +
        'orphan is no longer untracked drift, HTTP→HTTPS still redirects, and the traefik Application is ' +
        'Synced+Healthy.',
      context: { ...args },
      instructions: [
        'kubectl access is over SSH: prefix cluster commands with `' + args.masterSsh + ' \'<kubectl ...>\'`. Allow ArgoCD app-of-apps/selfHeal a short while to reconcile if a git change was merged.',
        'DECISION=delete: confirm `' + args.orphanKind + '/' + args.orphanName + '` is GONE from ' + args.ns + ' (NotFound) and was NOT recreated by selfHeal (it is untracked, so it must stay gone).',
        'DECISION=codify: confirm `' + args.orphanKind + '/' + args.orphanName + '` now EXISTS and is ArgoCD-TRACKED — it appears in the traefik Application `.status.resources[]` and carries app.kubernetes.io/instance / argocd tracking-id labels.',
        'ZERO-DRIFT CHECK: enumerate all IngressRoutes (and other traefik CRD objects) in ' + args.ns + ' and confirm none is an untracked orphan attributable to this issue. Report the traefik app sync/health (expect Synced/Healthy) and any remaining untracked drift.',
        'REDIRECT STILL WORKS: repeat the live curl test (`' + args.masterSsh + " 'curl -sS -o /dev/null -w \"%{http_code} %{redirect_url}\\n\" -H \"Host: nonexistent-" + args.issue + ".epaflix.com\" http://192.168.10.101/'`) and confirm a 30x→https response — proving HTTP→HTTPS still works (entrypoint redirect) regardless of the decision.",
        'Set verified=true ONLY if: the decision outcome holds (gone/untracked-free OR tracked) AND traefik app Synced+Healthy AND the redirect test returns 30x→https AND no issue-attributable untracked drift remains.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['verified', 'orphanGone', 'orphanTracked', 'zeroDrift', 'httpsRedirectWorks', 'appSync', 'appHealth', 'anomalies', 'summary'],
      properties: {
        verified: { type: 'boolean' },
        orphanGone: { type: 'boolean' },
        orphanTracked: { type: 'boolean' },
        zeroDrift: { type: 'boolean' },
        httpsRedirectWorks: { type: 'boolean' },
        appSync: { type: 'string' },
        appHealth: { type: 'string' },
        anomalies: { type: 'array', items: { type: 'string' } },
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
// Phase 5 — closeout: close #108, tick PR test plan, open any follow-up.
// ---------------------------------------------------------------------------
const closeoutTask = defineTask('closeout', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Close #108 with outcome, update PR test plan, open any follow-up',
  execution: { model: 'claude-opus-4-8' },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer reconciling issues/PR after a verified GitOps change in SpyrosPsarras/epaflix',
      task:
        'Record the verified outcome: close issue #' + args.issue + ', tick the PR test-plan checkboxes by ' +
        'EDITING the PR body (never a new comment), and open a follow-up issue ONLY if verify surfaced new drift.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Repo is ' + args.repo + '.',
        'Comment on issue #' + args.issue + ' summarizing the verified result (decision=' + args.decision + '; orphan gone-or-tracked; traefik-system zero untracked drift; HTTP→HTTPS still 30x→https via the entrypoint redirect; traefik app Synced+Healthy) and CLOSE it. Cross-link #51 and PR #107 as the prune-flip context that surfaced this.',
        'Edit the PR body (gh pr edit --body) to check off the Test Plan items that passed, recording observed evidence inline. Do NOT add a separate comment for the test plan (see feedback_pr_test_plans).',
        'Follow-up policy (CLAUDE.md): open ONE new gh issue ONLY if verify surfaced a DIFFERENT untracked orphan / drift in traefik-system worth tracking, using the repo enhancement-issue shape (## Finding / ## Current state / ## Desired outcome / ## Notes), cross-linking #' + args.issue + '. Otherwise return followUpIssueUrl as empty string.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['issueState', 'prUpdated', 'followUpIssueUrl'],
      properties: {
        issueState: { type: 'string' },
        prUpdated: { type: 'boolean' },
        followUpIssueUrl: { type: 'string' },
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
    orphanKind: 'IngressRoute',
    orphanName: 'https-redirect-epaflix',
    ns: 'traefik-system',
    appName: 'traefik',
    appPath: '2-k3s/05.traefik-deployment',
    kustomization: '2-k3s/05.traefik-deployment/kustomization.yaml',
    valuesFile: '2-k3s/05.traefik-deployment/values/traefik-values.yaml',
    masterSsh: 'ssh ubuntu@192.168.10.51',
    issue: '108',
    repo: 'SpyrosPsarras/epaflix',
    branch: 'traefik-drift-108-codify-or-remove',
    ...inputs,
  };

  ctx.log('info', 'traefik drift #108 — analyze+decide (prove entrypoint-redirect redundancy) → owner gate (delete|codify|abort) → execute → PR+merge → verify zero-drift → closeout');

  // PHASE 1 — analyze + decide (read-only).
  const analysis = await ctx.task(analyzeDecideTask, {
    repoRoot: cfg.repoRoot, orphanKind: cfg.orphanKind, orphanName: cfg.orphanName, ns: cfg.ns,
    appName: cfg.appName, appPath: cfg.appPath, valuesFile: cfg.valuesFile, masterSsh: cfg.masterSsh, issue: cfg.issue,
  });
  ctx.log('info', `Analysis: rec=${analysis.recommendation}; untracked=${analysis.orphanUntracked}; entrypointRedirect cfg=${analysis.entrypointRedirectConfigured}/live=${analysis.entrypointRedirectLive}; soleDeps=${(analysis.hostsDependingSolelyOnOrphan || []).length}`);

  // GATE 1 (destructive + deploy) — mandatory owner decision: delete | codify | abort.
  const gate = await ctx.breakpoint({
    question:
      'Issue #' + cfg.issue + ' — codify-or-remove the untracked `' + cfg.orphanKind + '/' + cfg.orphanName + '` in ' + cfg.ns + '.\n\n' +
      'Recommendation: ' + analysis.recommendation.toUpperCase() + '\n' +
      'Orphan untracked: ' + analysis.orphanUntracked + ' | in git: ' + analysis.inGit + '\n' +
      'Entrypoint-level HTTP→HTTPS redirect — configured: ' + analysis.entrypointRedirectConfigured + ', live: ' + analysis.entrypointRedirectLive + '\n' +
      'Live redirect test: ' + analysis.liveRedirectTest + '\n' +
      'Hosts depending SOLELY on the orphan: ' + JSON.stringify(analysis.hostsDependingSolelyOnOrphan) + '\n\n' +
      'Rationale: ' + analysis.rationale + '\n' +
      'Summary: ' + analysis.summary + '\n\n' +
      'DELETE = imperatively remove the live orphan (safe & permanent — untracked, so prune/selfHeal ignore it) + merge a doc note.\n' +
      'CODIFY = author it into git so ArgoCD adopts/tracks it (no live delete).\n' +
      'Both merge a PR per Epaflix policy. Choose:',
    options: ['Delete (remove orphan)', 'Codify (adopt into git)', 'Abort'],
    expert: 'owner',
    tags: ['destructive-git', 'deploy', 'approval-gate'],
  });

  const resp = (gate.response || '').toLowerCase();
  if (!gate.approved || resp.includes('abort')) {
    ctx.log('warn', 'No decision approved — aborting before any mutation.');
    return { success: false, decision: 'aborted', deletedLive: false, codified: false, merged: false, reason: 'not-approved', feedback: gate.response || gate.feedback || '', analysis };
  }
  // 'replace' = owner choice B: delete the dirty untracked live object, THEN codify a clean
  // copy into git so ArgoCD recreates+tracks it fresh. 'codify' = adopt live in place (no delete).
  // 'delete' = remove + doc note (no git resource).
  let decision;
  if (resp.includes('replace') || (resp.includes('delete') && resp.includes('codify'))) {
    decision = 'replace';
  } else if (resp.includes('codify')) {
    decision = 'codify';
  } else {
    decision = 'delete';
  }
  ctx.log('info', `Owner decision: ${decision}`);

  // PHASE 2 — execute the chosen path → branch + local commit (+ live delete for delete/replace).
  let deletedLive = false;
  let change;
  if (decision === 'delete') {
    const del = await ctx.task(deleteLiveTask, {
      orphanKind: cfg.orphanKind, orphanName: cfg.orphanName, ns: cfg.ns, masterSsh: cfg.masterSsh,
    });
    deletedLive = !!del.confirmedGone;
    ctx.log('info', `Live delete: deleted=${del.deleted}, confirmedGone=${del.confirmedGone}`);
    change = await ctx.task(prepareDocNoteTask, {
      repoRoot: cfg.repoRoot, appPath: cfg.appPath, orphanName: cfg.orphanName, branch: cfg.branch, issue: cfg.issue,
    });
  } else if (decision === 'replace') {
    const del = await ctx.task(deleteLiveTask, {
      orphanKind: cfg.orphanKind, orphanName: cfg.orphanName, ns: cfg.ns, masterSsh: cfg.masterSsh,
    });
    deletedLive = !!del.confirmedGone;
    ctx.log('info', `Live delete (replace): deleted=${del.deleted}, confirmedGone=${del.confirmedGone} — will recreate via git`);
    change = await ctx.task(prepareCodifyTask, {
      repoRoot: cfg.repoRoot, appPath: cfg.appPath, kustomization: cfg.kustomization,
      orphanKind: cfg.orphanKind, orphanName: cfg.orphanName, branch: cfg.branch, issue: cfg.issue,
      liveSpec: analysis.summary,
    });
  } else {
    change = await ctx.task(prepareCodifyTask, {
      repoRoot: cfg.repoRoot, appPath: cfg.appPath, kustomization: cfg.kustomization,
      orphanKind: cfg.orphanKind, orphanName: cfg.orphanName, branch: cfg.branch, issue: cfg.issue,
      liveSpec: analysis.summary,
    });
  }
  ctx.log('info', `Prepared (${decision}): branch=${change.branch} commit=${change.commitSha} files=${JSON.stringify(change.changedFiles)}`);

  // PHASE 3 — push + PR + merge.
  const pub = await ctx.task(publishMergeTask, {
    repoRoot: cfg.repoRoot, branch: change.branch, issue: cfg.issue, repo: cfg.repo,
    approvedPrTitle: change.prTitle, approvedPrBody: change.prBody,
  });
  ctx.log('info', `Merged: ${pub.merged}; PR=${pub.prUrl}; sha=${pub.mergeSha}`);

  // PHASE 4 — post-merge verify, with an owner gate on anomaly.
  // 'replace' ends tracked-in-git just like 'codify', so verify against codify semantics.
  const verifyDecision = decision === 'delete' ? 'delete' : 'codify';
  let post = await ctx.task(postVerifyTask, {
    decision: verifyDecision, orphanKind: cfg.orphanKind, orphanName: cfg.orphanName, ns: cfg.ns,
    appName: cfg.appName, masterSsh: cfg.masterSsh, issue: cfg.issue,
  });
  if (!pub.merged || !post.verified) {
    const recover = await ctx.breakpoint({
      question:
        'Post-merge verification found problems (decision=' + decision + ').\n' +
        'Merged: ' + pub.merged + '\n' +
        'Orphan gone: ' + post.orphanGone + '; orphan tracked: ' + post.orphanTracked + '\n' +
        'Zero drift: ' + post.zeroDrift + '; HTTP→HTTPS works: ' + post.httpsRedirectWorks + '\n' +
        'App: ' + post.appSync + '/' + post.appHealth + '\n' +
        'Anomalies: ' + JSON.stringify(post.anomalies) + '\n' +
        'Summary: ' + post.summary + '\n\nHow to proceed?',
      options: ['Re-verify (transient)', 'Continue to closeout (accept state)', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (recover.response || '').toLowerCase();
    if (recover.approved && r.includes('re-verify')) {
      post = await ctx.task(postVerifyTask, {
        decision: verifyDecision, orphanKind: cfg.orphanKind, orphanName: cfg.orphanName, ns: cfg.ns,
        appName: cfg.appName, masterSsh: cfg.masterSsh, issue: cfg.issue, attempt: 2,
      });
    } else if (!recover.approved || r.includes('stop')) {
      return { success: false, decision, deletedLive, codified: decision !== 'delete', merged: pub.merged, prUrl: pub.prUrl, reason: 'verification-stop', post };
    }
  }

  // PHASE 5 — closeout.
  const close = await ctx.task(closeoutTask, {
    repoRoot: cfg.repoRoot, issue: cfg.issue, repo: cfg.repo, decision, prUrl: pub.prUrl,
  });
  ctx.log('info', `Closeout: #${cfg.issue}=${close.issueState}; follow-up=${close.followUpIssueUrl}`);

  return {
    success: true,
    decision,
    deletedLive,
    codified: decision !== 'delete',
    merged: pub.merged,
    prUrl: pub.prUrl,
    zeroDrift: post.zeroDrift,
    httpsRedirectWorks: post.httpsRedirectWorks,
    issueState: close.issueState,
    followUpIssue: close.followUpIssueUrl,
  };
}
