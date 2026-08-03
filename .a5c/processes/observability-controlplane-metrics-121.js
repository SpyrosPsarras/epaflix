/**
 * @process specializations/devops-sre-platform/monitoring-setup
 * @description Resolve Epaflix issue #121: kube-prometheus-stack control-plane exporter
 *   Services for kube-controller-manager, kube-scheduler and kube-etcd exist in kube-system
 *   (tracked in git after #53) but their Endpoints carry NO addresses, so Prometheus has zero
 *   active targets for those three jobs (only `coredns` scrapes, up{job="coredns"}=1). Root
 *   cause: k3s binds these components' metrics to 127.0.0.1 — the k3s server args lack
 *   `--kube-controller-manager-arg=bind-address=0.0.0.0`, `--kube-scheduler-arg=bind-address=0.0.0.0`
 *   and `--etcd-expose-metrics=true` — AND the chart has no kubeControllerManager/kubeScheduler/
 *   kubeEtcd `endpoints:` so the manually-managed Endpoints stay empty.
 *
 *   Fix = TWO coordinated changes:
 *   (A) LIVE / imperative (deploy, control-plane restart, quorum risk): expose the three
 *       components' metrics on 0.0.0.0 on each master (51/52/53), ONE master at a time, via the
 *       durable k3s drop-in `/etc/rancher/k3s/config.yaml` (k3s merges it on start) + `systemctl
 *       restart k3s`, health-gating node-Ready + etcd-quorum + apiserver between each master.
 *   (B) GIT / GitOps: populate `kubeControllerManager.endpoints` / `kubeScheduler.endpoints` /
 *       `kubeEtcd.endpoints` (master node InternalIPs) with the correct ports/scheme
 *       (cm 10257 https, scheduler 10259 https, etcd 2381 http) in
 *       2-k3s/10.observability/prometheus-values.yaml so the chart fills the Endpoints with
 *       addresses; and add the three server args to the k3sup master install/join commands in
 *       .github/instructions/k3s.instructions.md so a rebuild stays correct.
 *
 *   Order matters: apply the LIVE bind-address change FIRST (so the ports answer), THEN merge the
 *   git endpoints change (so Prometheus only starts scraping ports that are already listening).
 *
 *   Flow: analyze (read-only: prove empty endpoints, confirm 127.0.0.1 bind, derive reachable
 *   master IPs + ports/scheme, recommend live method) → prepare git change locally (branch +
 *   commit, NO push) → ONE owner gate (deploy + destructive-git): show exact per-master live
 *   commands + the git diff + merge plan → live rollout (masters one-at-a-time, health-gated,
 *   hard-stop on failure; recovery gate on partial failure) → verify metrics ports now answer →
 *   push + PR + merge per Epaflix policy → post-verify (Endpoints populated, ArgoCD Synced/Healthy,
 *   up{job=kube-controller-manager|kube-scheduler|kube-etcd}=1; recovery gate on failure) →
 *   closeout (#121, PR test plan, follow-ups).
 *
 * @inputs { repoRoot, masters, masterSsh, ns, appName, valuesFile, k3sInstructions, issue, repo, branch }
 * @outputs { success, liveApplied, mastersDone, merged, prUrl, endpointsPopulated, jobsUp, issueState, followUpIssue }
 *
 * Local render caveat: the orchestrator host has no cluster context; kubectl + curl run over SSH
 * to a master (masterSsh). Drift/health is read via kubectl + ArgoCD live status + Prometheus
 * HTTP API, never a local render.
 *
 * @agent general-purpose (kubectl/curl/ssh + k3s config + git + gh executor; classification & verification)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

// ---------------------------------------------------------------------------
// Phase 1 — analyze (READ-ONLY): prove the gap, derive exact endpoint values + live method.
// ---------------------------------------------------------------------------
const analyzeTask = defineTask('analyze', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Confirm empty control-plane endpoints + 127.0.0.1 metrics bind, derive reachable master IPs/ports/scheme, recommend live method',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Kubernetes/k3s/Prometheus SRE on the Epaflix k3s cluster (3 masters 51/52/53)',
      task:
        'Establish ground truth for issue #' + args.issue + ': prove the kube-controller-manager / ' +
        'kube-scheduler / kube-etcd exporter Endpoints in ' + args.ns + ' are EMPTY, that k3s binds those ' +
        'components’ metrics to 127.0.0.1 (so they are unreachable from Prometheus pods), derive the exact ' +
        'master IPs + ports + scheme to scrape, and recommend the least-disruptive durable LIVE method to expose ' +
        'them on 0.0.0.0. DO NOT change anything.',
      context: { ...args },
      instructions: [
        'kubectl/curl run over SSH to a master: prefix every cluster command with `' + args.masterSsh + ' \'<cmd>\'`. Read git/files locally from repoRoot=' + args.repoRoot + '.',
        'EMPTY ENDPOINTS: `' + args.masterSsh + " 'kubectl -n " + args.ns + " get endpoints -o wide | grep -E \"kube-controller-manager|kube-scheduler|kube-etcd|coredns\"'`. Confirm the three control-plane Endpoints have NO addresses while coredns has one. Also `kubectl -n " + args.ns + " get svc` for those exporter Services (note their ports).",
        'TARGETS DOWN: query Prometheus for the three jobs. Find the prometheus svc/pod in ' + args.ns + ' and curl its API, e.g. `' + args.masterSsh + " 'kubectl -n " + args.ns + " exec deploy/<prom-or-via-svc> -c prometheus -- wget -qO- \"http://localhost:9090/api/v1/query?query=up\"'` (or port-forward / curl the svc clusterIP). Record up{job=...} for kube-controller-manager, kube-scheduler, kube-etcd, coredns.",
        '127.0.0.1 BIND PROOF: inspect the live k3s server args on a master via ' + args.masterSsh + ' — cat /etc/systemd/system/k3s.service and cat /etc/rancher/k3s/config.yaml (or report it is absent). Confirm NONE of bind-address=0.0.0.0 (controller-manager/scheduler) or etcd-expose-metrics is set. Optionally confirm the ports answer only on loopback with `sudo ss -ltnp` filtered for :10257 / :10259 / :2381 (expect 127.0.0.1 bindings; 2381 likely absent until etcd-expose-metrics).',
        'REACHABLE IPs: get each master node InternalIP — `' + args.masterSsh + " 'kubectl get nodes -o wide'`. Prometheus pods reach nodes via the node InternalIP (the k3s --node-ip, 10.0.0.51/52/53). Decide the endpoint IP list the chart should use (prefer the InternalIPs that Prometheus pods can route to). State the value chosen and why.",
        'PORTS/SCHEME for kube-prometheus-stack on k3s: kube-controller-manager metrics = port 10257 HTTPS (serviceMonitor.https=true, insecureSkipVerify=true); kube-scheduler = port 10259 HTTPS (same); kube-etcd = port 2381 HTTP (etcd-expose-metrics exposes plaintext on 2381, serviceMonitor scheme http). Confirm these against the running k3s version and report the exact values.',
        'LIVE METHOD RECOMMENDATION: recommend the DURABLE, least-disruptive way to add the args on each master. Prefer the k3s drop-in `/etc/rancher/k3s/config.yaml` (k3s merges it at startup, survives k3s upgrades; editing the k3sup-generated systemd unit ExecStart is fragile/overwritten on reinstall). The config.yaml additions: `kube-controller-manager-arg: ["bind-address=0.0.0.0"]`, `kube-scheduler-arg: ["bind-address=0.0.0.0"]`, `etcd-expose-metrics: true`. Note whether a config.yaml already exists (must MERGE, not clobber) and that a `systemctl restart k3s` is required for it to take effect.',
        'Return ONLY the structured JSON result, not a plan.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['endpointsEmpty', 'jobsUpBefore', 'metricsBoundLoopback', 'endpointIPs', 'cmPort', 'schedulerPort', 'etcdPort', 'liveMethod', 'existingConfigYaml', 'rationale', 'summary'],
      properties: {
        endpointsEmpty: { type: 'boolean' },
        jobsUpBefore: { type: 'object' },
        metricsBoundLoopback: { type: 'boolean' },
        endpointIPs: { type: 'array', items: { type: 'string' } },
        cmPort: { type: 'number' },
        schedulerPort: { type: 'number' },
        etcdPort: { type: 'number' },
        liveMethod: { type: 'string', enum: ['config-yaml', 'systemd-unit'] },
        existingConfigYaml: { type: 'string' },
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
// Phase 2 — prepare git change locally (branch + commit, NO push). Read-only on the cluster.
// ---------------------------------------------------------------------------
const prepareGitTask = defineTask('prepare-git', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Author prometheus-values endpoints + k3s.instructions rebuild args; branch + local commit (no push)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer wiring kube-prometheus-stack control-plane scraping into the Epaflix GitOps source',
      task:
        'Make two git edits and commit them to a branch locally (NO push, NO PR yet): (1) populate the ' +
        'kubeControllerManager/kubeScheduler/kubeEtcd endpoints+ports+scheme in ' + args.valuesFile + '; ' +
        '(2) add the three server args to the k3sup master commands in ' + args.k3sInstructions + '.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Respect CLAUDE.md (no secrets; manifests/docs only).',
        'EDIT 1 — ' + args.valuesFile + ': under the existing `kubeControllerManager:` (enabled:true) add `endpoints: ' + JSON.stringify(args.endpointIPs) + '`, a `service:` with `port: ' + args.cmPort + '` and `targetPort: ' + args.cmPort + '`, and a `serviceMonitor:` with `https: true` and `insecureSkipVerify: true`. Do the same for `kubeScheduler:` with port ' + args.schedulerPort + ' (https:true, insecureSkipVerify:true). For `kubeEtcd:` add `endpoints: ' + JSON.stringify(args.endpointIPs) + '`, `service.port: ' + args.etcdPort + '`, `service.targetPort: ' + args.etcdPort + '` and `serviceMonitor.scheme: http` (etcd-expose-metrics serves plaintext on ' + args.etcdPort + '; do NOT set https for etcd). Keep YAML style/indentation consistent with the file; do not disturb unrelated keys. Use the exact endpoint IPs/ports passed in context (from the analyze phase).',
        'EDIT 2 — ' + args.k3sInstructions + ': in the master install (`k3sup install`) and BOTH master `k3sup join` commands, append `--kube-controller-manager-arg=bind-address=0.0.0.0 --kube-scheduler-arg=bind-address=0.0.0.0 --etcd-expose-metrics=true` to the `--k3s-extra-args "..."` string so a future rebuild keeps these metrics exposed. Do NOT touch the worker join commands. Optionally add a one-line note that the running cluster applies the same via /etc/rancher/k3s/config.yaml (issue #' + args.issue + ').',
        'VALIDATE locally if tools exist: `kustomize build --enable-helm ' + args.appPath + '` (or `helm template` of the chart with the values) should render the three exporter Services with the endpoints; at minimum run a YAML lint and note what was run. Do not fail the task if helm plugins are unavailable locally — note it.',
        'If feedback is present in context (a prior breakpoint rejection), incorporate it before committing.',
        'Create branch ' + args.branch + ' off origin/main (fetch first; reuse if it exists). Stage ONLY the two changed files. ONE commit referencing #' + args.issue + ' (suggested subject: `feat(observability): scrape k3s control-plane metrics (cm/scheduler/etcd) (#' + args.issue + ')`). End the commit body with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.',
        'The prBody MUST include a Test Plan section with checkbox items for post-merge verification: Endpoints for the three exporter Services now carry master addresses; ArgoCD observability app Synced+Healthy; up{job="kube-controller-manager"}=1, up{job="kube-scheduler"}=1, up{job="kube-etcd"}=1 in Prometheus.',
        'Return ONLY the structured JSON result.',
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
// Phase 3 — LIVE rollout: expose metrics on 0.0.0.0, ONE master at a time, health-gated.
// Owner-approved at the deploy gate. Hard-stop on first master that does not return healthy.
// ---------------------------------------------------------------------------
const liveRolloutTask = defineTask('live-rollout', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Expose k3s control-plane metrics on 0.0.0.0 on each master (one at a time, health-gated, hard-stop on failure)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE executing an OWNER-APPROVED control-plane rollout on the Epaflix k3s cluster (etcd quorum 2/3 — never restart two masters at once)',
      task:
        'On masters ' + JSON.stringify(args.masters) + ', apply the metrics-bind change (' + args.liveMethod + ') and ' +
        'restart k3s ONE MASTER AT A TIME, verifying the cluster is healthy before moving to the next. This was ' +
        'explicitly approved at the deploy gate. STOP immediately if any master does not return healthy.',
      context: { ...args },
      instructions: [
        'For EACH master in order ' + JSON.stringify(args.masters) + ' (each has {host, ssh}): operate via its own ssh. NEVER restart the next master until the current one is fully healthy and etcd quorum is intact.',
        'BACKUP: `<ssh> \'sudo cp -a /etc/rancher/k3s/config.yaml /etc/rancher/k3s/config.yaml.bak-' + args.issue + ' 2>/dev/null || echo no-existing-config\'`. Capture the existing config.yaml content (if any) into the per-master result so the change is reversible.',
        'APPLY (config-yaml method): MERGE — read existing /etc/rancher/k3s/config.yaml; add/ensure the keys `kube-controller-manager-arg: ["bind-address=0.0.0.0"]`, `kube-scheduler-arg: ["bind-address=0.0.0.0"]`, `etcd-expose-metrics: true` WITHOUT removing any pre-existing keys. Write it back via a sudo tee from a heredoc. If liveMethod=systemd-unit instead, append the three `--kube-controller-manager-arg=bind-address=0.0.0.0 --kube-scheduler-arg=bind-address=0.0.0.0 --etcd-expose-metrics=true` flags to the ExecStart in /etc/systemd/system/k3s.service and `sudo systemctl daemon-reload`.',
        'RESTART: `<ssh> \'sudo systemctl restart k3s\'`. Then WAIT for readiness: poll (up to ~3 min) until `<ssh-to-a-DIFFERENT-healthy-master> \'kubectl get nodes\'` shows THIS master Ready again, AND etcd is healthy (`<ssh> \'sudo k3s etcd-snapshot ls >/dev/null 2>&1 || true\'` is not a health check — instead check `kubectl get --raw=/healthz`, and `kubectl -n kube-system get pods` settles). Confirm the apiserver on the VIP still answers.',
        'PER-MASTER METRICS CHECK: confirm the ports now answer on 0.0.0.0 on that master — `<ssh> \'sudo ss -ltnp | grep -E ":' + args.cmPort + '|:' + args.schedulerPort + '|:' + args.etcdPort + '"\'` should show 0.0.0.0 (or *) bindings, and a local curl to the etcd metrics port returns data: `<ssh> \'curl -sf http://127.0.0.1:' + args.etcdPort + '/metrics | head -1\'`.',
        'HARD-STOP: if a master fails to return Ready / etcd unhealthy within the window, DO NOT proceed to the next master. Restore that master’s config from the .bak, restart k3s, and return with success=false and the failing host recorded.',
        'Do NOT change anything else. Do NOT git-commit here. Return ONLY the structured JSON result with per-master status.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['success', 'mastersDone', 'perMaster', 'clusterHealthy', 'summary'],
      properties: {
        success: { type: 'boolean' },
        mastersDone: { type: 'array', items: { type: 'string' } },
        perMaster: { type: 'array', items: { type: 'object' } },
        clusterHealthy: { type: 'boolean' },
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
// Phase 4 — push + PR + merge per Epaflix merge policy.
// ---------------------------------------------------------------------------
const publishMergeTask = defineTask('publish-merge', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Push branch, open PR, rebase + merge per Epaflix policy',
  execution: { model: MODEL },
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
        'Open a PR to main with the approved title/body (context: approvedPrTitle / approvedPrBody). Cross-link issue #' + args.issue + ' and refs #53 (where this was surfaced), #25, #13.',
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
// Phase 5 — post-merge verification: endpoints populated + 3 jobs up=1 + ArgoCD Synced/Healthy.
// ---------------------------------------------------------------------------
const postVerifyTask = defineTask('post-verify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify Endpoints populated, up{job=cm|scheduler|etcd}=1, observability app Synced+Healthy',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Prometheus/ArgoCD SRE verifying issue #' + args.issue + ' is resolved end-to-end',
      task:
        'Confirm the three control-plane exporter Endpoints now carry master addresses, Prometheus reports ' +
        'up=1 for kube-controller-manager, kube-scheduler and kube-etcd, and the observability ArgoCD ' +
        'Application is Synced+Healthy.',
      context: { ...args },
      instructions: [
        'kubectl/curl over SSH: prefix with `' + args.masterSsh + ' \'<cmd>\'`. Allow ArgoCD selfHeal/app-of-apps a short while to reconcile the merged values, then re-check.',
        'ENDPOINTS: `' + args.masterSsh + " 'kubectl -n " + args.ns + " get endpoints | grep -E \"kube-controller-manager|kube-scheduler|kube-etcd\"'` must now show addresses (the master IPs), not <none>.",
        'TARGETS UP: query Prometheus up{} for the three jobs (same method as analyze). Require up{job=\"kube-controller-manager\"}=1, up{job=\"kube-scheduler\"}=1, up{job=\"kube-etcd\"}=1. Capture the raw values.',
        'ARGOCD: report observability app sync/health (`' + args.masterSsh + " 'kubectl -n argocd get application " + args.appName + " -o jsonpath=\"{.status.sync.status}/{.status.health.status}\"'`) — expect Synced/Healthy.",
        'Set verified=true ONLY if: all three Endpoints populated AND all three jobs up=1 AND observability app Synced+Healthy.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['verified', 'endpointsPopulated', 'jobsUp', 'appSync', 'appHealth', 'anomalies', 'summary'],
      properties: {
        verified: { type: 'boolean' },
        endpointsPopulated: { type: 'boolean' },
        jobsUp: { type: 'object' },
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
// Phase 6 — closeout: close #121, tick PR test plan, open any follow-up.
// ---------------------------------------------------------------------------
const closeoutTask = defineTask('closeout', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Close #121 with outcome, update PR test plan, open any follow-up',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer reconciling issues/PR after a verified change in SpyrosPsarras/epaflix',
      task:
        'Record the verified outcome: close issue #' + args.issue + ', tick the PR test-plan checkboxes by ' +
        'EDITING the PR body (never a new comment), and open follow-up issues for anything deferred.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Repo is ' + args.repo + '.',
        'Comment on issue #' + args.issue + ' summarizing the verified result (three control-plane jobs now up=1; Endpoints populated; observability app Synced+Healthy; live applied on masters ' + JSON.stringify(args.masters) + ' via ' + args.liveMethod + ') and CLOSE it. Cross-link #53/#25/#13.',
        'Edit the PR body (gh pr edit --body) to check off the Test Plan items that passed, recording observed evidence inline. Do NOT add a separate comment for the test plan (see feedback_pr_test_plans).',
        'Follow-up policy (CLAUDE.md): open a `gh issue` (repo enhancement shape: ## Finding / ## Current state / ## Desired outcome / ## Notes, cross-linking #' + args.issue + ') for any deferred item — e.g. durability of the /etc/rancher/k3s/config.yaml drop-in under the planned system-upgrade-controller bring-up (#44), or any control-plane Grafana dashboard now worth enabling. If nothing is deferred, return followUpIssueUrl as empty string.',
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
    masters: [
      { host: 'k3s-master-51', ssh: 'ssh ubuntu@192.168.10.51' },
      { host: 'k3s-master-52', ssh: 'ssh ubuntu@192.168.10.52' },
      { host: 'k3s-master-53', ssh: 'ssh ubuntu@192.168.10.53' },
    ],
    masterSsh: 'ssh ubuntu@192.168.10.51',
    ns: 'kube-system',
    appName: 'observability',
    appPath: '2-k3s/10.observability',
    valuesFile: '2-k3s/10.observability/prometheus-values.yaml',
    k3sInstructions: '.github/instructions/k3s.instructions.md',
    issue: '121',
    repo: 'SpyrosPsarras/epaflix',
    branch: 'observability-controlplane-metrics-121',
    ...inputs,
  };

  ctx.log('info', '#121 control-plane metrics — analyze → prepare git → owner gate (deploy+merge) → live rollout (1 master at a time) → verify ports → PR+merge → post-verify (up=1) → closeout');

  // PHASE 1 — analyze (read-only).
  const analysis = await ctx.task(analyzeTask, {
    repoRoot: cfg.repoRoot, masterSsh: cfg.masterSsh, ns: cfg.ns, appName: cfg.appName,
    appPath: cfg.appPath, valuesFile: cfg.valuesFile, k3sInstructions: cfg.k3sInstructions, issue: cfg.issue,
  });
  ctx.log('info', `Analysis: endpointsEmpty=${analysis.endpointsEmpty}; loopbackBind=${analysis.metricsBoundLoopback}; IPs=${JSON.stringify(analysis.endpointIPs)}; ports cm/sch/etcd=${analysis.cmPort}/${analysis.schedulerPort}/${analysis.etcdPort}; liveMethod=${analysis.liveMethod}`);

  // PHASE 2 — prepare git change locally (branch + commit, no push). Refine loop on gate rejection.
  let change = await ctx.task(prepareGitTask, {
    repoRoot: cfg.repoRoot, appPath: cfg.appPath, valuesFile: cfg.valuesFile, k3sInstructions: cfg.k3sInstructions,
    endpointIPs: analysis.endpointIPs, cmPort: analysis.cmPort, schedulerPort: analysis.schedulerPort, etcdPort: analysis.etcdPort,
    branch: cfg.branch, issue: cfg.issue,
  });
  ctx.log('info', `Prepared git: branch=${change.branch} commit=${change.commitSha} files=${JSON.stringify(change.changedFiles)} renderOk=${change.renderOk}`);

  // GATE (deploy + destructive-git) — ONE mandatory owner approval covering BOTH the live
  // control-plane rollout AND the subsequent PR merge. Retry/refine loop on rejection.
  let approved = false;
  let lastFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (lastFeedback) {
      change = await ctx.task(prepareGitTask, {
        repoRoot: cfg.repoRoot, appPath: cfg.appPath, valuesFile: cfg.valuesFile, k3sInstructions: cfg.k3sInstructions,
        endpointIPs: analysis.endpointIPs, cmPort: analysis.cmPort, schedulerPort: analysis.schedulerPort, etcdPort: analysis.etcdPort,
        branch: cfg.branch, issue: cfg.issue, feedback: lastFeedback, attempt: attempt + 1,
      });
    }
    const gate = await ctx.breakpoint({
      question:
        'Issue #' + cfg.issue + ' — expose k3s control-plane metrics (kube-controller-manager / kube-scheduler / kube-etcd).\n\n' +
        'ANALYSIS: endpoints empty=' + analysis.endpointsEmpty + '; metrics bound to loopback=' + analysis.metricsBoundLoopback + '\n' +
        'up before: ' + JSON.stringify(analysis.jobsUpBefore) + '\n' +
        'Endpoint IPs to scrape: ' + JSON.stringify(analysis.endpointIPs) + ' (cm:' + analysis.cmPort + ' https, scheduler:' + analysis.schedulerPort + ' https, etcd:' + analysis.etcdPort + ' http)\n' +
        'Live method: ' + analysis.liveMethod + ' (' + analysis.rationale + ')\n\n' +
        'THIS GATE AUTHORIZES TWO THINGS:\n' +
        '1) LIVE (deploy): on masters 51/52/53, ONE AT A TIME, add bind-address=0.0.0.0 (cm+scheduler) + etcd-expose-metrics=true and `systemctl restart k3s`, health-gating etcd quorum between each. Control-plane restart — quorum risk if mishandled.\n' +
        '2) GIT (merge): push + PR + merge the endpoints/instructions change. Files: ' + JSON.stringify(change.changedFiles) + '\n\n' +
        '--- git diff ---\n' + (change.diff || '(no diff captured)').slice(0, 4000) + '\n\nProceed with BOTH?',
      options: ['Approve (live rollout + merge)', 'Request changes', 'Abort'],
      expert: 'owner',
      tags: ['deploy', 'destructive-git', 'approval-gate'],
      previousFeedback: lastFeedback || undefined,
      attempt: attempt > 0 ? attempt + 1 : undefined,
    });
    const r = (gate.response || '').toLowerCase();
    if (gate.approved && r.includes('approve')) { approved = true; break; }
    if (!gate.approved || r.includes('abort')) {
      ctx.log('warn', 'Gate not approved / aborted — no mutation performed.');
      return { success: false, decision: 'aborted', liveApplied: false, merged: false, reason: 'not-approved', feedback: gate.response || gate.feedback || '', analysis };
    }
    lastFeedback = gate.response || gate.feedback || 'Changes requested';
    ctx.log('info', `Gate requested changes (attempt ${attempt + 1}); refining git change.`);
  }
  if (!approved) {
    return { success: false, decision: 'aborted', liveApplied: false, merged: false, reason: 'not-approved-after-retries', analysis };
  }

  // PHASE 3 — LIVE rollout FIRST (so the ports answer before Prometheus scrapes them).
  let live = await ctx.task(liveRolloutTask, {
    masters: cfg.masters, liveMethod: analysis.liveMethod, issue: cfg.issue,
    cmPort: analysis.cmPort, schedulerPort: analysis.schedulerPort, etcdPort: analysis.etcdPort,
  });
  ctx.log('info', `Live rollout: success=${live.success}; done=${JSON.stringify(live.mastersDone)}; healthy=${live.clusterHealthy}`);

  if (!live.success || !live.clusterHealthy) {
    const recover = await ctx.breakpoint({
      question:
        'LIVE control-plane rollout did NOT complete cleanly (deploy).\n' +
        'Masters done: ' + JSON.stringify(live.mastersDone) + '\n' +
        'Cluster healthy: ' + live.clusterHealthy + '\n' +
        'Summary: ' + live.summary + '\n\n' +
        'The git change has NOT been merged yet. How to proceed?',
      options: ['Retry live rollout', 'Stop here (do not merge)'],
      expert: 'owner',
      tags: ['deploy', 'verification-gate'],
    });
    const rr = (recover.response || '').toLowerCase();
    if (recover.approved && rr.includes('retry')) {
      live = await ctx.task(liveRolloutTask, {
        masters: cfg.masters, liveMethod: analysis.liveMethod, issue: cfg.issue,
        cmPort: analysis.cmPort, schedulerPort: analysis.schedulerPort, etcdPort: analysis.etcdPort, attempt: 2,
      });
    }
    if (!live.success || !live.clusterHealthy) {
      return { success: false, decision: 'live-failed', liveApplied: false, merged: false, reason: 'live-rollout-incomplete', live };
    }
  }

  // PHASE 4 — push + PR + merge (ports already listening, so scrapes succeed once endpoints fill).
  const pub = await ctx.task(publishMergeTask, {
    repoRoot: cfg.repoRoot, branch: change.branch, issue: cfg.issue, repo: cfg.repo,
    approvedPrTitle: change.prTitle, approvedPrBody: change.prBody,
  });
  ctx.log('info', `Merged: ${pub.merged}; PR=${pub.prUrl}; sha=${pub.mergeSha}`);

  // PHASE 5 — post-merge verify, with an owner recovery gate on failure.
  let post = await ctx.task(postVerifyTask, {
    ns: cfg.ns, appName: cfg.appName, masterSsh: cfg.masterSsh, issue: cfg.issue,
  });
  if (!pub.merged || !post.verified) {
    const recover = await ctx.breakpoint({
      question:
        'Post-merge verification incomplete.\n' +
        'Merged: ' + pub.merged + '\n' +
        'Endpoints populated: ' + post.endpointsPopulated + '\n' +
        'Jobs up: ' + JSON.stringify(post.jobsUp) + '\n' +
        'App: ' + post.appSync + '/' + post.appHealth + '\n' +
        'Anomalies: ' + JSON.stringify(post.anomalies) + '\n' +
        'Summary: ' + post.summary + '\n\nHow to proceed?',
      options: ['Re-verify (transient/allow more sync time)', 'Continue to closeout (accept state)', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (recover.response || '').toLowerCase();
    if (recover.approved && r.includes('re-verify')) {
      post = await ctx.task(postVerifyTask, {
        ns: cfg.ns, appName: cfg.appName, masterSsh: cfg.masterSsh, issue: cfg.issue, attempt: 2,
      });
    } else if (!recover.approved || r.includes('stop')) {
      return { success: false, decision: 'verify-stop', liveApplied: true, merged: pub.merged, prUrl: pub.prUrl, reason: 'verification-stop', post };
    }
  }

  // PHASE 6 — closeout.
  const close = await ctx.task(closeoutTask, {
    repoRoot: cfg.repoRoot, issue: cfg.issue, repo: cfg.repo, prUrl: pub.prUrl,
    masters: cfg.masters.map((m) => m.host), liveMethod: analysis.liveMethod,
  });
  ctx.log('info', `Closeout: #${cfg.issue}=${close.issueState}; follow-up=${close.followUpIssueUrl}`);

  return {
    success: true,
    decision: 'applied',
    liveApplied: true,
    mastersDone: live.mastersDone,
    merged: pub.merged,
    prUrl: pub.prUrl,
    endpointsPopulated: post.endpointsPopulated,
    jobsUp: post.jobsUp,
    issueState: close.issueState,
    followUpIssue: close.followUpIssueUrl,
  };
}
