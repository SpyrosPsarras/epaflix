/**
 * @process specializations/devops-sre-platform/system-upgrade-plans-onboard
 * @description Deliver Epaflix issue #74: onboard the parked K3s system-upgrade `Plan`
 *   manifests so the cluster performs its first (supervised) rolling upgrade.
 *
 *   Owner-revised design (2026-05-31): ALL Apps automatic — NO manual sync.
 *   Layout A: Plans live in a NEW kustomization `2-k3s/maintenance/system-upgrade/plans/`
 *   fronted by a NEW ArgoCD Application `system-upgrade-plans` with
 *   syncPolicy.automated { selfHeal:true, prune:false } (fleet-consistent), and the
 *   EXISTING `system-upgrade-controller` App is flipped manual -> automated in the same PR.
 *   Channel stays `stable`.
 *
 *   Because the Plans App is automated/selfHeal, MERGE itself is the deploy: app-of-apps
 *   creates the App, it auto-syncs, the controller applies the Plans and starts the rolling
 *   upgrade. There is therefore ONE hard deploy gate at MERGE (not a later manual sync), with
 *   the etcd snapshot + baseline taken BEFORE that gate. Future stable-channel bumps will
 *   auto-roll unsupervised — accepted consequence of "automatic".
 *
 *   homarr is disposable (its 0-disruption PDB may be evicted/killed freely). The
 *   postgres-cluster-primary 0-disruption PDB is a noted (non-blocking) risk: CNPG fails
 *   over and force-drain skips after 60s.
 *
 * @inputs { repoRoot, repo, issue, masterSsh, branch, controllerApp, controllerAppManifest,
 *           plansApp, ns, plansPath, plansAppManifest, appsKustomization, parkedPlansFile, channel }
 * @outputs { success, merged, prUrl, rollout, nodesUpgraded, issue74State, followUpIssues }
 *
 * RISK: MERGE triggers a live, fleet-wide MINOR rolling K3s upgrade (v1.34 -> stable). Each
 * node is drained (workloads reschedule); masters concurrency:1 preserve etcd quorum; workers
 * concurrency:2 keep >=2 up. Gated by ONE owner deploy breakpoint at merge after an etcd
 * snapshot + readiness baseline.
 *
 * @agent general-purpose (kustomize/git/gh executor + kubectl-over-ssh rollout watcher)
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

// ---------------------------------------------------------------------------
// Task definitions
// ---------------------------------------------------------------------------

// PHASE 1 — analyze repo + render current state, produce the concrete onboarding plan.
// (UNCHANGED — already executed; kept identical for journal replay.)
const analyzePlanTask = defineTask('analyze-plan', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Analyze system-upgrade state and produce the Plan-onboarding design (#74)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Kubernetes/ArgoCD SRE planning a supervised rolling K3s upgrade on the Epaflix cluster',
      task:
        'Produce a concrete, reviewable design for onboarding the parked K3s upgrade Plans per layout option A ' +
        '(new `plans/` kustomization + new manual-sync App `' + args.plansApp + '`), keeping channel `' + args.channel + '`. ' +
        'DO NOT modify any file or cluster state — analysis only.',
      context: { ...args },
      instructions: [
        'Run git/render commands locally from repoRoot=' + args.repoRoot + '. kubectl access is over SSH: prefix cluster reads with `' + args.masterSsh + ' \'<kubectl ...>\'`.',
        'Read the parked Plans at ' + args.parkedPlansFile + ' and the existing controller App ' + args.plansAppManifest.replace('plans', 'controller') + ' / kustomization to confirm the controller is installed (v0.19.2) and manual-sync.',
        'Confirm cluster prerequisites (READ ONLY): controller Deployment Healthy in ns ' + args.ns + '; `plans.upgrade.cattle.io` CRD installed; all 7 nodes Ready; record each node\'s current k3s version (`kubectl get nodes -o wide`).',
        'Resolve the channel target: fetch ' + args.channel + ' to learn the LATEST stable K3s version the Plans will pull, and compare to the nodes\' current versions. State explicitly whether a rolling upgrade WILL occur (versions differ) or will be a no-op (already latest).',
        'Design the file changes for option A: (1) new dir ' + args.plansPath + ' containing the two Plans (k3s-server, k3s-agent) plus a kustomization.yaml; (2) a new ArgoCD Application manifest ' + args.plansAppManifest + ' for App `' + args.plansApp + '` — MANUAL sync (syncPolicy omitted), source path ' + args.plansPath + ', destination ns ' + args.ns + '; (3) add the new App file to ' + args.appsKustomization + '; (4) decide whether to delete or retain the old parked ' + args.parkedPlansFile + ' (recommend moving its Plan content into plans/ and leaving a short pointer/README note rather than a stray unreferenced file).',
        'State the expected rollout choreography (masters concurrency:1 → workers concurrency:2 via prepare:k3s-server) and the rollback story (delete the Plans / un-sync the App stops FURTHER nodes; a node mid-upgrade completes).',
        'List any RISKS or preconditions that must hold before the live sync (e.g. all nodes Ready, no active long-running drains, PodDisruptionBudgets that could block drain).',
        'Return ONLY the structured JSON design, not prose commentary.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['controllerHealthy', 'crdInstalled', 'nodesReady', 'nodeVersions', 'channelLatest', 'upgradeWillOccur', 'fileChanges', 'rolloutPlan', 'risks', 'summary'],
      properties: {
        controllerHealthy: { type: 'boolean' },
        crdInstalled: { type: 'boolean' },
        nodesReady: { type: 'boolean' },
        nodeVersions: { type: 'array', items: { type: 'string' } },
        channelLatest: { type: 'string' },
        upgradeWillOccur: { type: 'boolean' },
        fileChanges: { type: 'array', items: { type: 'string' } },
        rolloutPlan: { type: 'string' },
        risks: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// PHASE 2 — author manifests on a branch + local commit + local validation (NO push).
// Plans App = AUTOMATED selfHeal; controller App flipped manual->automated in the same change.
const authorManifestsTask = defineTask('author-manifests', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Create plans/ kustomization + AUTOMATED App, flip controller App, validate, local commit',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer authoring Kustomize + ArgoCD Application manifests in the Epaflix repo',
      task:
        'Implement layout option A with ALL Apps AUTOMATED (no manual sync) on a branch with ONE local commit, ' +
        'then validate the render locally. Do NOT push, do NOT open a PR, do NOT touch issues, do NOT touch the cluster.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Respect CLAUDE.md: never commit secrets; encrypted Secrets use *.enc.yaml (none expected here).',
        'Create branch ' + args.branch + ' off the current main (rebase onto origin/main first; reuse the branch if it already exists).',
        'Create ' + args.plansPath + '/ with: the two Plans (k3s-server + k3s-agent) carried over verbatim from ' + args.parkedPlansFile + ' (channel ' + args.channel + ', server concurrency:1 cordon+drain, agent concurrency:2 with prepare:k3s-server), and a kustomization.yaml referencing them. Keep the explanatory header comments.',
        'Author ' + args.plansAppManifest + ' = ArgoCD Application `' + args.plansApp + '` in ns argocd: project default, source repoURL https://github.com/' + args.repo + '.git targetRevision main path ' + args.plansPath + ', destination ns ' + args.ns + '. syncPolicy.automated { selfHeal: true, prune: false } and syncOptions: [ServerSideApply=true] (fleet-consistent). Header comment: this App is AUTOMATED per owner decision (no manual sync); MERGE auto-applies the Plans via app-of-apps + selfHeal and starts the rolling upgrade; future stable-channel bumps will auto-roll unsupervised (issue #' + args.issue + ').',
        'FLIP the existing controller App ' + args.controllerAppManifest + ' from manual (syncPolicy omitted) to syncPolicy.automated { selfHeal: true, prune: false } + syncOptions: [ServerSideApply=true]. Update its header comment to reflect the settled automated decision (supersedes the prior "manual on first install / flip after 48h soak" note).',
        'Add `' + args.plansAppManifest.split('/').pop() + '` to the resources list in ' + args.appsKustomization + '.',
        'Resolve the old parked file ' + args.parkedPlansFile + ' per the approved design: remove it (its content now lives in plans/) and add a one-line pointer in 2-k3s/maintenance/system-upgrade/README.md to the new plans/ App. Also update the stale "CRD not installed" exclusion comment in 2-k3s/maintenance/kustomization.yaml if present (point it at the new plans/ App).',
        'VALIDATE locally and capture results: `kustomize build --enable-helm ' + args.plansPath + '`, `kustomize build --enable-helm 2-k3s/maintenance/system-upgrade/controller`, and `kustomize build 2-k3s/11.argocd/apps`. Run the SOPS pre-commit guard `.github/hooks/check-sops-encrypted.sh` if present. All must pass.',
        'If feedback is present in context (a prior rejection), incorporate it before committing.',
        'Stage ONLY the changed/added files. Make ONE commit referencing #' + args.issue + '. End the commit body with the Co-Authored-By trailer for Claude Opus 4.8 (1M context).',
        'Return ONLY structured JSON: branch, changedFiles, commitSha, validationPassed, validationNotes, and a proposed PR title + body. The PR body MUST include a Test Plan section with checkbox items for: app-of-apps creates the `' + args.plansApp + '` App and it auto-syncs (selfHeal), controller App now automated, Plans accepted, masters upgrade one-at-a-time, workers two-at-a-time, all 7 nodes reach the target version and Ready, no workload left stranded (homarr may be killed).',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'changedFiles', 'commitSha', 'validationPassed', 'validationNotes', 'prTitle', 'prBody'],
      properties: {
        branch: { type: 'string' },
        changedFiles: { type: 'array', items: { type: 'string' } },
        commitSha: { type: 'string' },
        validationPassed: { type: 'boolean' },
        validationNotes: { type: 'string' },
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

// PHASE 3 — pre-deploy baseline + etcd snapshot, BEFORE merge (merge triggers the upgrade).
const preDeploySnapshotTask = defineTask('pre-deploy-snapshot', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Take etcd snapshot + capture pre-upgrade baseline (nodes Ready, versions, target)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'ArgoCD/K3s SRE taking a pre-upgrade safety baseline on the Epaflix cluster',
      task:
        'Before the merge that will auto-trigger the rolling upgrade, take a FRESH etcd snapshot and capture a ' +
        'complete readiness baseline. Snapshot only — do NOT change Plans/Apps/nodes.',
      context: { ...args },
      instructions: [
        'kubectl access over SSH: prefix with `' + args.masterSsh + ' \'<kubectl ...>\'`.',
        'Take a fresh on-demand etcd snapshot on a master (k3s built-in): `' + args.masterSsh + " 'sudo k3s etcd-snapshot save --name pre-1.35-upgrade'` (or confirm the most recent scheduled snapshot is very recent). Record the snapshot name/location and confirm success (`sudo k3s etcd-snapshot ls` newest entry).",
        'Baseline: all 7 nodes Ready and SchedulingEnabled (no leftover cordon): `' + args.masterSsh + " 'kubectl get nodes -o wide'`. Record per-node k3s version.",
        'Resolve target: fetch ' + args.channel + ' for the latest stable version; state upgradeWillOccur=true/false and which nodes are behind.',
        'Confirm controller Healthy and currently running: `' + args.masterSsh + " 'kubectl -n " + args.ns + " get deploy,pods'`. Confirm NO Plans/jobs live yet: `" + args.masterSsh + " 'kubectl -n " + args.ns + " get plans,jobs'`.",
        'Note drain considerations: homarr (servarr/homarr-pdb, 0 disruptions) is DISPOSABLE — fine to evict/kill. Flag postgres-cluster-primary (0 disruptions) as a non-blocking risk (CNPG failover + force-drain skip60). List any OTHER 0-disruption PDB or critical single-replica workload.',
        'CONCURRENT-WORKER GUARD: another worker may be operating on Postgres right now. Detect any IN-FLIGHT Postgres activity that draining the primary node would clobber, and set postgresBusy accordingly. Check: CNPG Cluster status (`' + args.masterSsh + " 'kubectl -n postgres-system get cluster -o wide'` — phase should be \"Cluster in healthy state\", currentPrimary stable, NOT switchover/failover/upgrading); any in-progress CNPG backup/restore (`" + args.masterSsh + " 'kubectl -n postgres-system get backups.postgresql.cnpg.io,scheduledbackups.postgresql.cnpg.io 2>/dev/null'`); any running Jobs/CronJob runs touching postgres (`" + args.masterSsh + " 'kubectl -n postgres-system get jobs; kubectl -n servarr get jobs'` incl. the postgres-sequence-audit job); active long-running queries/locks if reachable. Set postgresBusy=true and list the busy items if ANY postgres operation is mid-flight; otherwise false.",
        'Return ONLY structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['etcdSnapshotTaken', 'etcdSnapshotName', 'nodesReady', 'nodeVersions', 'channelLatest', 'upgradeWillOccur', 'controllerHealthy', 'plansLiveYet', 'postgresBusy', 'postgresBusyDetail', 'drainRisks', 'summary'],
      properties: {
        etcdSnapshotTaken: { type: 'boolean' },
        etcdSnapshotName: { type: 'string' },
        nodesReady: { type: 'boolean' },
        nodeVersions: { type: 'array', items: { type: 'string' } },
        channelLatest: { type: 'string' },
        upgradeWillOccur: { type: 'boolean' },
        controllerHealthy: { type: 'boolean' },
        plansLiveYet: { type: 'boolean' },
        postgresBusy: { type: 'boolean' },
        postgresBusyDetail: { type: 'array', items: { type: 'string' } },
        drainRisks: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// PHASE 4 — push + PR + merge per policy. Merge AUTO-triggers the rolling upgrade (selfHeal).
const publishMergeTask = defineTask('publish-merge', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Push, open PR, rebase + merge per Epaflix policy (merge auto-triggers the upgrade)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer publishing an OWNER-APPROVED change to ' + args.repo,
      task:
        'Push the branch, open a PR, and merge per the Epaflix semi-linear policy (rebase onto origin/main + ' +
        'force-with-lease, wait for the `validate` check, then merge-commit). Merging makes app-of-apps create the ' +
        '`' + args.plansApp + '` Application which is AUTOMATED/selfHeal — so it auto-syncs and the rolling K3s ' +
        'upgrade STARTS shortly after merge. This is the owner-approved deploy.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Rebase branch ' + args.branch + ' onto origin/main, push with --force-with-lease.',
        'Open a PR to main with the approved title/body (in context as approvedPrTitle / approvedPrBody). Cross-link issue #' + args.issue + '.',
        'Wait for the required `validate` check to pass (strict up-to-date is enforced). If the branch went stale, rebase + force-with-lease again.',
        'Merge using the authorized flow: `gh pr merge --merge` (merge commit, NOT squash/rebase). Admin bypass is authorized if the 0-approval block needs it.',
        'Capture the PR URL and merge commit SHA. Confirm the PR is MERGED before returning. Do NOT manually sync — selfHeal will. (You MAY note the App will appear shortly.)',
        'Return ONLY structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'merged', 'mergeSha'],
      properties: { prUrl: { type: 'string' }, merged: { type: 'boolean' }, mergeSha: { type: 'string' } },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// PHASE 5 — watch the (selfHeal-triggered) rolling upgrade to completion.
const rolloutWatchTask = defineTask('rollout-watch', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Watch app-of-apps create+sync the App and the rolling K3s upgrade (masters → workers) to completion',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'ArgoCD/K3s SRE supervising a live, automated rolling cluster upgrade',
      task:
        'After merge, WATCH app-of-apps create the `' + args.plansApp + '` App and auto-sync it (selfHeal), the ' +
        'controller apply the Plans, and the rolling upgrade run to completion: masters one-at-a-time, then workers ' +
        'two-at-a-time, each node cordon→drain→upgrade→uncordon. Observe only — do NOT hand-edit Plans/nodes.',
      context: { ...args },
      instructions: [
        'kubectl access over SSH: prefix with `' + args.masterSsh + ' \'<kubectl ...>\'`.',
        'Wait for app-of-apps (selfHeal) to create the App: poll `' + args.masterSsh + " 'kubectl -n argocd get application " + args.plansApp + " -o json'` until it exists and reaches Synced; you MAY nudge app-of-apps with `" + args.masterSsh + " 'kubectl -n argocd annotate application app-of-apps argocd.argoproj.io/refresh=normal --overwrite'` if it is slow. Do NOT create the App by hand.",
        'Confirm the Plans landed: `' + args.masterSsh + " 'kubectl -n " + args.ns + " get plans -o wide'`. Confirm the controller App is now automated too.",
        'WATCH loop (poll periodically; allow a long total budget — a full 7-node rolling upgrade can take many minutes per node): `' + args.masterSsh + " 'kubectl -n " + args.ns + " get plans,jobs -o wide'` and `" + args.masterSsh + " 'kubectl get nodes -o wide'`. Track choreography: masters k3s-master-51/52/53 one at a time (concurrency:1, quorum), then workers 61/62 then 63/65 two at a time (gated by prepare:k3s-server).",
        'For each node: observe cordon (SchedulingDisabled) → drain → upgrade job Completes → node re-Ready on the new version → uncordon. homarr being evicted/killed is EXPECTED and fine. A node must be Ready on the new version before counting it done.',
        'CONCURRENT-WORKER GUARD (postgres): another worker may operate on Postgres during the rollout. When the node hosting the CNPG primary is cordoned/drained, watch the CNPG Cluster (`' + args.masterSsh + " 'kubectl -n postgres-system get cluster -o wide; kubectl -n postgres-system get pods -o wide'`) and confirm a clean failover (a replica is promoted, Cluster returns to healthy). Do NOT force-delete or hand-manage postgres pods/PVCs — let CNPG + the drain handle it. If you observe an in-flight CNPG backup/switchover/restore at that moment, PAUSE and report it as a problem (do not let the drain race a concurrent postgres operation) rather than pushing through.",
        'No-op case: if all nodes are already on channelLatest, the Plans apply but produce NO upgrade jobs — record rolloutOccurred=false and treat as success.',
        'STOP and report (do not loop forever) if: a drain is stuck > ~10 min on something OTHER than homarr, an upgrade job goes Error/Backoff, a node fails to rejoin, or etcd/control-plane looks unhealthy. Capture the failing object + logs.',
        'On success: confirm ALL 7 nodes Ready on the target version and no leftover SchedulingDisabled. Capture final node versions.',
        'Return ONLY structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['appSyncedByGitops', 'plansAccepted', 'rolloutOccurred', 'rolloutComplete', 'nodesUpgraded', 'finalNodeVersions', 'mastersDone', 'workersDone', 'problems', 'summary'],
      properties: {
        appSyncedByGitops: { type: 'boolean' },
        plansAccepted: { type: 'boolean' },
        rolloutOccurred: { type: 'boolean' },
        rolloutComplete: { type: 'boolean' },
        nodesUpgraded: { type: 'number' },
        finalNodeVersions: { type: 'array', items: { type: 'string' } },
        mastersDone: { type: 'boolean' },
        workersDone: { type: 'boolean' },
        problems: { type: 'array', items: { type: 'string' } },
        summary: { type: 'string' },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// PHASE 6 — post-upgrade verification (all nodes on target, Ready, workloads healthy).
const postUpgradeVerifyTask = defineTask('post-upgrade-verify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify all 7 nodes upgraded + Ready and key workloads healthy after the rollout',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE validating cluster health after a rolling K3s upgrade',
      task: 'Confirm the cluster is fully healthy on the target K3s version after the rollout. READ ONLY.',
      context: { ...args },
      instructions: [
        'kubectl access over SSH: prefix with `' + args.masterSsh + ' \'<kubectl ...>\'`.',
        'Confirm all 7 nodes Ready, SchedulingEnabled, on the SAME target version (matches channelLatest baseline=' + (args.channelLatest || 'see context') + '): `' + args.masterSsh + " 'kubectl get nodes -o wide'`.",
        'Confirm no leftover upgrade jobs Pending/Error and Plans show LATEST applied: `' + args.masterSsh + " 'kubectl -n " + args.ns + " get plans,jobs -o wide'`.",
        'Cluster health: no pods stuck Pending/CrashLoopBackOff cluster-wide that were Running pre-upgrade (homarr being down is fine): `' + args.masterSsh + " 'kubectl get pods -A | grep -Ev \"Running|Completed\" || true'`.",
        'Spot-check key ArgoCD Apps still Synced/Healthy (workloads merely rescheduled), incl. both system-upgrade Apps now automated: `' + args.masterSsh + " 'kubectl -n argocd get applications'`.",
        'Set verified=true ONLY if all nodes Ready on target version AND no new broken workloads (other than disposable homarr). List anomalies.',
        'Return ONLY structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['verified', 'allNodesTargetVersion', 'nodesReady', 'brokenWorkloads', 'argoAppsHealthy', 'anomalies', 'summary'],
      properties: {
        verified: { type: 'boolean' },
        allNodesTargetVersion: { type: 'boolean' },
        nodesReady: { type: 'boolean' },
        brokenWorkloads: { type: 'array', items: { type: 'string' } },
        argoAppsHealthy: { type: 'boolean' },
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

// PHASE 7 — closeout: close #74, tick PR test plan, open follow-ups.
const closeoutTask = defineTask('closeout', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Close #74 with outcome, update PR test plan, open future-auto-upgrade SOP follow-up',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer reconciling issues/PR after a verified supervised upgrade in ' + args.repo,
      task:
        'Record the verified outcome: close issue #' + args.issue + ', tick the PR test-plan checkboxes by EDITING ' +
        'the PR body (never a new comment), and open the follow-up issue(s).',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Repo is ' + args.repo + '.',
        'Comment on issue #' + args.issue + ' summarizing the verified result (Plans onboarded via AUTOMATED App `' + args.plansApp + '`; controller App also flipped to automated; rollout outcome: occurred? complete? nodes upgraded; final version) and CLOSE it.',
        'Edit the PR body (gh pr edit --body) to check off the Test Plan items that passed, recording observed evidence inline. Do NOT add a separate comment.',
        'Open a follow-up gh issue (repo enhancement shape: ## Finding / ## Current state / ## Desired outcome / ## Notes), cross-linking #' + args.issue + ' and #44/#18: both system-upgrade Apps are now AUTOMATED selfHeal, so any FUTURE stable-channel K3s bump will auto-roll the whole fleet UNSUPERVISED. Desired outcome = document the future-upgrade SOP / monitoring expectation, and decide whether to pin the channel or add an alert so unsupervised minor jumps are caught.',
        'If the rollout left any workload needing attention (e.g. postgres primary failover, a stuck PVC), open a SEPARATE follow-up issue for it.',
        'Return ONLY structured JSON with issue74State, prUpdated, followUpIssues (array of urls).',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['issue74State', 'prUpdated', 'followUpIssues'],
      properties: {
        issue74State: { type: 'string' },
        prUpdated: { type: 'boolean' },
        followUpIssues: { type: 'array', items: { type: 'string' } },
      },
    },
  },
  io: {
    inputJsonPath: `tasks/${taskCtx.effectId}/input.json`,
    outputJsonPath: `tasks/${taskCtx.effectId}/output.json`,
  },
}));

// PHASE 5b (conditional) — fix the Plan (add control-plane tolerations) + re-PR + merge.
const fixPlanTask = defineTask('fix-plan', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Add system-upgrade-controller tolerations to the K3s Plans + re-PR + merge',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer fixing a deadlocked K3s upgrade Plan in ' + args.repo,
      task:
        'The rolling upgrade is deadlocked: the k3s-server Plan upgrade Job pod cannot schedule onto the tainted ' +
        'control-plane nodes because the Plan has NO tolerations. Add the standard system-upgrade-controller ' +
        'tolerations to the Plans, validate, and ship via a new PR (merge auto-reapplies via selfHeal). Use an ' +
        'ISOLATED git worktree — another worker is on the main checkout (issue-93). Execute fully, return real result.',
      context: { ...args },
      instructions: [
        'Another worker uses the main working tree. Do ALL git work in an isolated worktree: `git -C ' + args.repoRoot + ' fetch origin`, `git -C ' + args.repoRoot + ' worktree add /tmp/su-plans-fix -b ' + args.fixBranch + ' origin/main`; operate inside /tmp/su-plans-fix; when done `git -C ' + args.repoRoot + ' worktree remove /tmp/su-plans-fix --force` and confirm the main tree is untouched (still on issue-93/crd-argocd-adoption).',
        'Edit the Plans file `' + args.plansFile + '` (post-merge content from the onboarding PR). Add to the k3s-server Plan spec a `tolerations:` list with the standard SUC set so the upgrade Job can run on tainted control-plane/etcd nodes:',
        '    tolerations:',
        '      - {key: "CriticalAddonsOnly", operator: "Exists"}',
        '      - {key: "node-role.kubernetes.io/control-plane", operator: "Exists", effect: "NoSchedule"}',
        '      - {key: "node-role.kubernetes.io/master", operator: "Exists", effect: "NoSchedule"}',
        '      - {key: "node-role.kubernetes.io/etcd", operator: "Exists", effect: "NoExecute"}',
        'Mirror the SAME tolerations onto the k3s-agent Plan too (harmless on untainted workers; correct if any worker is tainted). Add a short comment explaining the tolerations are required for the Job to schedule onto tainted nodes. If feedback is present in context, incorporate it.',
        'VALIDATE: `kustomize build --enable-helm ' + args.plansPath + '` must render both Plans with the tolerations. Run `.github/hooks/check-sops-encrypted.sh` if present.',
        'Commit ONE commit referencing #' + args.issue + ' (Co-Authored-By trailer for Claude Opus 4.8 (1M context)). Push the worktree branch with -u. Open a PR to main (title like "fix(system-upgrade): add control-plane tolerations to K3s upgrade Plans (#' + args.issue + ')", body explaining the scheduling deadlock + fix, end with the Claude Code trailer). Cross-link #' + args.issue + ' and PR #' + (args.onboardPr || '126') + '.',
        'Follow Epaflix policy: rebase onto origin/main if stale, force-with-lease, wait for `validate`, then `gh pr merge --merge` (admin if 0-approval blocks). Confirm MERGED.',
        'Do NOT manually sync or hand-edit the live Plan/pods — selfHeal will re-apply the fixed Plan and SUC will re-sync the upgrade Job. Return ONLY structured JSON.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['prUrl', 'merged', 'mergeSha', 'validationPassed', 'summary'],
      properties: {
        prUrl: { type: 'string' },
        merged: { type: 'boolean' },
        mergeSha: { type: 'string' },
        validationPassed: { type: 'boolean' },
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
    issue: '74',
    masterSsh: 'ssh ubuntu@192.168.10.51',
    branch: 'system-upgrade-plans-onboard',
    controllerApp: 'system-upgrade-controller',
    controllerAppManifest: '2-k3s/11.argocd/apps/app-system-upgrade-controller.yaml',
    plansApp: 'system-upgrade-plans',
    ns: 'system-upgrade',
    plansPath: '2-k3s/maintenance/system-upgrade/plans',
    plansAppManifest: '2-k3s/11.argocd/apps/app-system-upgrade-plans.yaml',
    appsKustomization: '2-k3s/11.argocd/apps/kustomization.yaml',
    parkedPlansFile: '2-k3s/maintenance/system-upgrade/system-upgrade-plans.yaml',
    channel: 'https://update.k3s.io/v1-release/channels/stable',
    ...inputs,
  };

  ctx.log('info', 'system-upgrade Plans onboard (#74, AUTOMATED) — analyze → design gate → author/validate → etcd snapshot → MERGE=deploy gate → watch → verify → closeout');

  // PHASE 1 — analyze + design (no mutation). [executed; replays from journal]
  const design = await ctx.task(analyzePlanTask, {
    repoRoot: cfg.repoRoot, masterSsh: cfg.masterSsh, ns: cfg.ns, channel: cfg.channel,
    plansPath: cfg.plansPath, plansApp: cfg.plansApp, plansAppManifest: cfg.plansAppManifest,
    appsKustomization: cfg.appsKustomization, parkedPlansFile: cfg.parkedPlansFile, issue: cfg.issue,
  });
  ctx.log('info', `Analyze: controllerHealthy=${design.controllerHealthy} nodesReady=${design.nodesReady} target=${design.channelLatest} upgradeWillOccur=${design.upgradeWillOccur}`);

  // GATE 1 (architecture/plan) — approve the REVISED design (all Apps AUTOMATED) before any file change.
  {
    let lastFeedback = null;
    let approved = false;
    let current = design;
    for (let attempt = 0; attempt < 3; attempt++) {
      if (lastFeedback) {
        current = await ctx.task(analyzePlanTask, {
          repoRoot: cfg.repoRoot, masterSsh: cfg.masterSsh, ns: cfg.ns, channel: cfg.channel,
          plansPath: cfg.plansPath, plansApp: cfg.plansApp, plansAppManifest: cfg.plansAppManifest,
          appsKustomization: cfg.appsKustomization, parkedPlansFile: cfg.parkedPlansFile, issue: cfg.issue,
          feedback: lastFeedback, attempt: attempt + 1,
        });
      }
      const g = await ctx.breakpoint({
        question:
          'Approve the REVISED design for #74 (layout A, channel stable, ALL APPS AUTOMATED)?\n\n' +
          'OWNER REVISION: the `' + cfg.plansApp + '` App will be syncPolicy.automated{selfHeal:true,prune:false} (NOT manual), ' +
          'AND the existing `' + cfg.controllerApp + '` App is flipped manual→automated in the SAME PR. homarr is disposable (its PDB may be killed).\n\n' +
          'IMPORTANT consequence: because the Plans App is automated/selfHeal, MERGE auto-applies the Plans and STARTS the rolling upgrade. ' +
          'There is ONE hard deploy gate at MERGE (after an etcd snapshot). Future stable-channel bumps will auto-roll UNSUPERVISED.\n\n' +
          'Controller healthy: ' + current.controllerHealthy + ' | CRD: ' + current.crdInstalled + ' | nodes Ready: ' + current.nodesReady + '\n' +
          'Current node versions: ' + JSON.stringify(current.nodeVersions) + '\n' +
          'Channel latest (target): ' + current.channelLatest + ' | rolling upgrade will occur: ' + current.upgradeWillOccur + ' (MINOR 1.34→1.35 jump)\n' +
          'Rollback: no in-place downgrade; levers = patch Plan concurrency:0 to pause, etcd snapshot restore. etcd snapshot taken at the merge gate.\n' +
          'Risks: ' + JSON.stringify(current.risks) + '\n\n' +
          'Summary: ' + current.summary + '\n\n' +
          'This gate approves the DESIGN + authoring manifests. The live upgrade is gated again at MERGE.',
        options: ['Approve design', 'Request changes', 'Abort'],
        expert: 'owner',
        tags: ['architecture-change', 'approval-gate'],
        previousFeedback: lastFeedback || undefined,
        attempt: attempt > 0 ? attempt + 1 : undefined,
      });
      const r = (g.response || '').toLowerCase();
      if (g.approved && !r.includes('abort')) { approved = true; break; }
      if (r.includes('abort')) {
        ctx.log('warn', 'Design aborted by owner.');
        return { success: false, merged: false, reason: 'design-aborted', feedback: g.response || g.feedback || '' };
      }
      lastFeedback = g.response || g.feedback || 'Changes requested';
    }
    if (!approved) return { success: false, merged: false, reason: 'design-not-approved-after-retries' };
  }

  // PHASE 2 — author manifests (automated Apps) + local validate + commit (reversible, no push).
  let change = await ctx.task(authorManifestsTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo, branch: cfg.branch, issue: cfg.issue, channel: cfg.channel,
    plansPath: cfg.plansPath, plansApp: cfg.plansApp, plansAppManifest: cfg.plansAppManifest,
    controllerApp: cfg.controllerApp, controllerAppManifest: cfg.controllerAppManifest,
    appsKustomization: cfg.appsKustomization, parkedPlansFile: cfg.parkedPlansFile, ns: cfg.ns,
  });
  ctx.log('info', `Authored: branch=${change.branch} commit=${change.commitSha} validationPassed=${change.validationPassed}`);

  // GATE 2a — approve the authored change before any cluster-affecting action (request-changes loop).
  {
    let lastFeedback = null;
    let approved = false;
    for (let attempt = 0; attempt < 3; attempt++) {
      if (lastFeedback) {
        change = await ctx.task(authorManifestsTask, {
          repoRoot: cfg.repoRoot, repo: cfg.repo, branch: cfg.branch, issue: cfg.issue, channel: cfg.channel,
          plansPath: cfg.plansPath, plansApp: cfg.plansApp, plansAppManifest: cfg.plansAppManifest,
          controllerApp: cfg.controllerApp, controllerAppManifest: cfg.controllerAppManifest,
          appsKustomization: cfg.appsKustomization, parkedPlansFile: cfg.parkedPlansFile, ns: cfg.ns,
          feedback: lastFeedback, attempt: attempt + 1,
        });
      }
      const g = await ctx.breakpoint({
        question:
          'Review the authored manifests (#' + cfg.issue + ') before the etcd-snapshot + merge step?\n\n' +
          'Branch: ' + change.branch + ' | commit: ' + change.commitSha + '\n' +
          'Changed files: ' + JSON.stringify(change.changedFiles) + '\n' +
          'Local validation passed: ' + change.validationPassed + ' — ' + change.validationNotes + '\n\n' +
          'Next: take an etcd snapshot, then the MERGE deploy gate (merge auto-starts the rolling upgrade).',
        options: ['Looks good, proceed', 'Request changes', 'Abort'],
        expert: 'owner',
        tags: ['approval-gate'],
        previousFeedback: lastFeedback || undefined,
        attempt: attempt > 0 ? attempt + 1 : undefined,
      });
      const r = (g.response || '').toLowerCase();
      if (g.approved && !r.includes('abort')) { approved = true; break; }
      if (r.includes('abort')) {
        ctx.log('warn', 'Authored change aborted by owner — local branch retained, nothing pushed.');
        return { success: false, merged: false, reason: 'author-aborted', branch: change.branch, feedback: g.response || g.feedback || '' };
      }
      lastFeedback = g.response || g.feedback || 'Changes requested';
    }
    if (!approved) return { success: false, merged: false, reason: 'author-not-approved-after-retries', branch: change.branch };
  }

  // PHASE 3 — pre-deploy etcd snapshot + baseline (BEFORE merge, since merge triggers the upgrade).
  const snap = await ctx.task(preDeploySnapshotTask, {
    masterSsh: cfg.masterSsh, ns: cfg.ns, channel: cfg.channel,
  });
  ctx.log('info', `Snapshot: etcd=${snap.etcdSnapshotTaken}(${snap.etcdSnapshotName}) nodesReady=${snap.nodesReady} target=${snap.channelLatest} upgradeWillOccur=${snap.upgradeWillOccur}`);

  // GATE 2 (destructive-git + deploy — THE live-upgrade gate) — approve push+PR+MERGE.
  const deployGate = await ctx.breakpoint({
    question:
      'Approve PUSH + PR + MERGE now (#' + cfg.issue + ')? MERGE AUTO-STARTS THE LIVE ROLLING K3s UPGRADE.\n\n' +
      'etcd snapshot taken: ' + snap.etcdSnapshotTaken + ' (' + snap.etcdSnapshotName + ')\n' +
      'Nodes Ready: ' + snap.nodesReady + ' | controller Healthy: ' + snap.controllerHealthy + ' | Plans live yet: ' + snap.plansLiveYet + '\n' +
      'Current versions: ' + JSON.stringify(snap.nodeVersions) + '\n' +
      'Target (channel latest): ' + snap.channelLatest + ' | rolling upgrade will occur: ' + snap.upgradeWillOccur + ' (MINOR 1.34→1.35)\n' +
      'CONCURRENT POSTGRES WORK: ' + (snap.postgresBusy ? '⚠️ BUSY — ' + JSON.stringify(snap.postgresBusyDetail) + ' (a concurrent worker is mid-operation; draining the primary now could clobber it — consider Hold)' : 'idle (no in-flight postgres operation detected)') + '\n' +
      'Drain notes: ' + JSON.stringify(snap.drainRisks) + '\n\n' +
      'Summary: ' + snap.summary + '\n\n' +
      'On merge: app-of-apps creates the automated `' + cfg.plansApp + '` App → it self-syncs → Plans land → masters upgrade one-at-a-time (etcd quorum), then workers two-at-a-time. Each node drains (homarr may be killed). Proceed?',
    options: ['Approve merge + live rollout', 'Hold (keep local branch, do not push)', 'Abort'],
    expert: 'owner',
    tags: ['destructive-git', 'deploy', 'approval-gate'],
  });
  if (!deployGate.approved || (deployGate.response || '').toLowerCase().includes('hold') || (deployGate.response || '').toLowerCase().includes('abort')) {
    ctx.log('warn', 'Merge/rollout NOT approved — local branch retained, nothing pushed.');
    return {
      success: false, merged: false, rollout: 'held', reason: 'deploy-held-by-owner',
      branch: change.branch, note: 'Manifests committed locally on branch `' + change.branch + '`; nothing pushed. Issue #' + cfg.issue + ' left OPEN.',
    };
  }

  // PHASE 4 — push + PR + merge (auto-triggers the rolling upgrade via app-of-apps + selfHeal).
  const pub = await ctx.task(publishMergeTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo, branch: change.branch, issue: cfg.issue, plansApp: cfg.plansApp,
    approvedPrTitle: change.prTitle, approvedPrBody: change.prBody,
  });
  ctx.log('info', `Merged: ${pub.merged}; PR=${pub.prUrl}; sha=${pub.mergeSha}`);
  if (!pub.merged) {
    const g = await ctx.breakpoint({
      question: 'Merge did not complete (PR=' + pub.prUrl + '). How to proceed?',
      options: ['Stop here', 'Continue anyway'],
      expert: 'owner', tags: ['deploy', 'verification-gate'],
    });
    if (!g.approved || (g.response || '').toLowerCase().includes('stop')) {
      return { success: false, merged: false, prUrl: pub.prUrl, reason: 'merge-failed' };
    }
  }

  // PHASE 5 — watch the selfHeal-triggered rolling upgrade.
  let roll = await ctx.task(rolloutWatchTask, {
    masterSsh: cfg.masterSsh, ns: cfg.ns, plansApp: cfg.plansApp, channelLatest: snap.channelLatest,
  });
  ctx.log('info', `Rollout: occurred=${roll.rolloutOccurred} complete=${roll.rolloutComplete} nodesUpgraded=${roll.nodesUpgraded} problems=${(roll.problems || []).length}`);

  // Recovery loop if the rollout did not cleanly complete (fix Plan / resume / accept / stop).
  let cycle = 0;
  while (!roll.rolloutComplete && cycle < 4) {
    const rec = await ctx.breakpoint({
      question:
        'Rolling upgrade did NOT cleanly complete.\n' +
        'App synced by gitops: ' + roll.appSyncedByGitops + ' | Plans accepted: ' + roll.plansAccepted + ' | occurred: ' + roll.rolloutOccurred + '\n' +
        'Nodes upgraded: ' + roll.nodesUpgraded + '/7 | masters done: ' + roll.mastersDone + ' | workers done: ' + roll.workersDone + '\n' +
        'Problems: ' + JSON.stringify(roll.problems) + '\n' +
        'Summary: ' + roll.summary + '\n\n' +
        'If the fault is the missing control-plane toleration on the k3s-server Plan (scheduling deadlock), pick "Fix Plan" — it adds the standard SUC tolerations via a new PR; selfHeal re-applies and the rollout proceeds. ' +
        '(rollback lever: etcd snapshot = ' + (snap.etcdSnapshotName || 'see context') + '). How to proceed?',
      options: ['Fix Plan tolerations + re-PR + resume', 'Resume watch (still progressing)', 'Continue to verify (accept state)', 'Stop here'],
      expert: 'owner',
      tags: ['deploy', 'verification-gate'],
    });
    const r = (rec.response || '').toLowerCase();
    if (!rec.approved || r.includes('stop')) {
      return { success: false, merged: pub.merged, prUrl: pub.prUrl, rollout: 'incomplete', roll, reason: 'rollout-stopped' };
    }
    if (r.includes('verify') || r.includes('accept')) break;
    if (r.includes('fix')) {
      const fix = await ctx.task(fixPlanTask, {
        repoRoot: cfg.repoRoot, repo: cfg.repo, issue: cfg.issue,
        fixBranch: 'system-upgrade-plan-tolerations', onboardPr: '126',
        plansFile: cfg.plansPath + '/system-upgrade-plans.yaml', plansPath: cfg.plansPath,
        attempt: cycle + 1,
      });
      ctx.log('info', `Plan-fix: merged=${fix.merged} PR=${fix.prUrl} validation=${fix.validationPassed}`);
    }
    // After a fix (or a plain resume), re-watch the rollout.
    roll = await ctx.task(rolloutWatchTask, {
      masterSsh: cfg.masterSsh, ns: cfg.ns, plansApp: cfg.plansApp, channelLatest: snap.channelLatest, attempt: cycle + 2,
    });
    ctx.log('info', `Rollout (cycle ${cycle + 1}): complete=${roll.rolloutComplete} nodesUpgraded=${roll.nodesUpgraded} problems=${(roll.problems || []).length}`);
    cycle++;
  }

  // PHASE 6 — post-upgrade verify.
  let post = await ctx.task(postUpgradeVerifyTask, {
    masterSsh: cfg.masterSsh, ns: cfg.ns, channelLatest: snap.channelLatest,
  });
  if (!post.verified) {
    const rec = await ctx.breakpoint({
      question:
        'Post-upgrade verification found problems.\n' +
        'All nodes on target: ' + post.allNodesTargetVersion + ' | nodes Ready: ' + post.nodesReady + '\n' +
        'Broken workloads: ' + JSON.stringify(post.brokenWorkloads) + '\n' +
        'ArgoCD apps healthy: ' + post.argoAppsHealthy + '\n' +
        'Anomalies: ' + JSON.stringify(post.anomalies) + '\n' +
        'Summary: ' + post.summary + '\n\nHow to proceed?',
      options: ['Re-verify (transient)', 'Continue to closeout (accept state)', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (rec.response || '').toLowerCase();
    if (rec.approved && r.includes('re-verify')) {
      post = await ctx.task(postUpgradeVerifyTask, { masterSsh: cfg.masterSsh, ns: cfg.ns, channelLatest: snap.channelLatest, attempt: 2 });
    } else if (!rec.approved || r.includes('stop')) {
      return { success: false, merged: pub.merged, prUrl: pub.prUrl, rollout: 'verify-stop', post, reason: 'verify-stopped' };
    }
  }

  // PHASE 7 — closeout: close #74, tick PR test plan, open future-auto-upgrade SOP follow-up.
  const close = await ctx.task(closeoutTask, {
    repoRoot: cfg.repoRoot, repo: cfg.repo, issue: cfg.issue, prUrl: pub.prUrl,
    controllerApp: cfg.controllerApp, plansApp: cfg.plansApp,
  });
  ctx.log('info', `Closeout: #74=${close.issue74State}; follow-ups=${JSON.stringify(close.followUpIssues)}`);

  return {
    success: true,
    merged: pub.merged,
    prUrl: pub.prUrl,
    rollout: roll.rolloutComplete ? (roll.rolloutOccurred ? 'completed' : 'no-op-already-latest') : 'incomplete',
    nodesUpgraded: roll.nodesUpgraded,
    finalNodeVersions: roll.finalNodeVersions,
    verified: post.verified,
    issue74State: close.issue74State,
    followUpIssues: close.followUpIssues,
  };
}
