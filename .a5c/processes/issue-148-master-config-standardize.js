/**
 * @process specializations/devops-sre-platform/iac-implementation
 * @description Resolve Epaflix issue #148: k3s control-plane config drift. master-52 carries its
 *   k3s server/kubelet args via the k3sup-generated systemd unit `ExecStart` (inline flags), while
 *   masters 51/53 carry their args in the durable drop-in `/etc/rancher/k3s/config.yaml`. As part of
 *   the #121 fix, 52 also gained a partial config.yaml (metrics args + resolv-conf pin) — so 52 now
 *   has TWO sources of truth (config.yaml + inline ExecStart), and CLI flags override config.yaml.
 *
 *   GOAL: make `/etc/rancher/k3s/config.yaml` the SINGLE source of truth per master. For every master
 *   that still carries inline server/kubelet flags in its `ExecStart`, MERGE those flags into its
 *   config.yaml (union of effective args, no behaviour change) and reduce `ExecStart` to a bare
 *   `k3s server`, so server/kubelet config is consistent across 51/52/53 and upgrade-durable.
 *
 *   This is a REFACTOR of WHERE args live, NOT a change to WHAT args are in effect. The effective
 *   running k3s server+kubelet args MUST be identical before and after (metrics binds from #121 still
 *   on 0.0.0.0, kubelet resolv-conf=/etc/k3s-resolv.conf pin intact, node-ip/advertise/tls-sans/
 *   etcd-args/disable/node-taint/write-kubeconfig-mode preserved, and the join server-URL/token
 *   references preserved). Getting the join config wrong could stop a server rejoining etcd — so the
 *   live op is per-master, one at a time, health-gated (etcd quorum 2/3 — never restart two masters
 *   at once), fully backed up (.bak) and rollback-on-failure.
 *
 *   Two coordinated outputs:
 *   (A) LIVE / imperative (deploy, control-plane restart, quorum risk): per master needing it, write
 *       the consolidated config.yaml, rewrite the systemd unit ExecStart to bare `server`,
 *       daemon-reload + restart k3s, health-gate node-Ready + etcd quorum + apiserver + prove the
 *       effective args are unchanged. Out-of-band (k3s host config is not ArgoCD-managed).
 *   (B) GIT / docs: update .github/instructions/k3s.instructions.md to state all masters standardize
 *       on the config.yaml drop-in, document the canonical consolidated config.yaml, and remove the
 *       "52 has no config.yaml / args in ExecStart" divergence framing. PR per Epaflix merge policy.
 *
 *   Flow: inspect (read-only, all 3 masters: dump config.yaml + ExecStart + k3s version + node Ready +
 *   etcd health; derive per-master reconciliation need + the consolidated config.yaml + target
 *   ExecStart) → prepare docs change + per-master runbook locally (branch + commit, NO push) → ONE
 *   owner gate (deploy + destructive-git): show per-master config.yaml/ExecStart before→after + docs
 *   diff + merge plan → live rollout (masters one-at-a-time, health-gated, hard-stop + rollback on
 *   failure; recovery gate on partial failure) → push + PR + merge per policy → post-verify (all
 *   masters config.yaml-only, no inline ExecStart args, nodes Ready, etcd healthy, #121 metrics still
 *   up, resolv-conf pin intact, CoreDNS happy) → closeout (#148, PR test plan, follow-up #44 upgrade
 *   durability).
 *
 * @inputs { repoRoot, masters, masterSsh, ns, appName, k3sInstructions, issue, repo, branch, relatedIssues }
 * @outputs { success, decision, liveApplied, mastersReconciled, merged, prUrl, parityConfirmed, issueState, followUpIssue }
 *
 * Local render caveat: the orchestrator host has no cluster context; kubectl runs over SSH to a
 * healthy master (masterSsh). Drift/health is read live over SSH, never a local render.
 *
 * @agent general-purpose ssh/k3s-config/systemd + kubectl + git + gh executor; classification & verification
 */

import { defineTask } from '@a5c-ai/babysitter-sdk';

const AGENT = 'general-purpose';
const MODEL = 'claude-opus-4-8';

// ---------------------------------------------------------------------------
// Phase 1 — inspect (READ-ONLY): ground truth on all 3 masters; derive per-master
// reconciliation need + the consolidated config.yaml + the target reduced ExecStart.
// ---------------------------------------------------------------------------
const inspectTask = defineTask('inspect', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Dump config.yaml + systemd ExecStart + k3s version + node/etcd health on masters 51/52/53; derive per-master reconciliation plan',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'Kubernetes/k3s SRE on the Epaflix k3s cluster (3 etcd masters 51/52/53, quorum 2/3)',
      task:
        'Establish ground truth for issue #' + args.issue + ' (control-plane config drift): for EACH master ' +
        JSON.stringify(args.masters.map((m) => m.host)) + ', capture its FULL /etc/rancher/k3s/config.yaml ' +
        '(or report absent), its systemd unit ExecStart line(s) and any EnvironmentFile, the running k3s version, ' +
        'node Ready status and etcd member health. Then determine WHICH masters still carry inline server/kubelet ' +
        'flags in ExecStart (the divergence), and for each such master derive the CONSOLIDATED config.yaml (the ' +
        'union of all effective server+kubelet args expressed in config.yaml form) and the TARGET reduced ExecStart ' +
        '(bare `k3s server`, env-file preserved). DO NOT change anything.',
      context: { ...args },
      instructions: [
        'Operate over SSH per master using its own ssh string (each master is {host, ssh, internalIp}). kubectl runs over SSH to a healthy master (' + args.masterSsh + "). Read git/files locally from repoRoot=" + args.repoRoot + '.',
        'PER MASTER, capture: (1) `<ssh> \'sudo cat /etc/rancher/k3s/config.yaml 2>/dev/null || echo __ABSENT__\'`; (2) the full systemd unit: `<ssh> \'cat /etc/systemd/system/k3s.service\'` — record the ExecStart (it may be multi-line with backslash continuations) and any `EnvironmentFile=` line; (3) `<ssh> \'cat /etc/systemd/system/k3s.service.env 2>/dev/null || echo __NONE__\'` (may hold K3S_TOKEN / K3S_URL — do NOT print secret VALUES, just record which KEYS exist); (4) `<ssh> \'k3s --version\'`; (5) node Ready + roles via `' + args.masterSsh + " 'kubectl get nodes -o wide'`.",
        'ETCD HEALTH: confirm all 3 etcd members healthy before recommending any restart — e.g. `' + args.masterSsh + " 'kubectl get --raw=/healthz'` and check the etcd pod/static manifest or `sudo k3s etcd-snapshot ls` exists; record member count = 3 healthy.",
        'CLASSIFY each master: does its ExecStart contain INLINE server/agent flags beyond a bare `server` (e.g. --node-ip, --advertise-address, --flannel-iface, --node-taint, --tls-san, --disable, --etcd-arg, --kube-controller-manager-arg, --kube-scheduler-arg, --etcd-expose-metrics, --write-kubeconfig-mode, --server, --token)? If YES -> needsReconcile=true. masters where ExecStart is already bare `server` (all args already in config.yaml) -> needsReconcile=false (parity reference).',
        'EFFECTIVE ARGS: for each master, compute the COMPLETE effective server+kubelet configuration = (config.yaml keys) UNION (inline ExecStart flags), with CLI-flag precedence noted. This is the behaviour that MUST be preserved.',
        'DERIVE consolidatedConfigYaml (per master needing reconcile): the full /etc/rancher/k3s/config.yaml content that expresses EVERY effective arg in config.yaml form. Map flags to keys: --node-ip->node-ip, --advertise-address->advertise-address, --flannel-iface->flannel-iface, --node-taint->node-taint (list), --tls-san->tls-san (list), --disable->disable (list, e.g. [servicelb, traefik]), --etcd-arg=X->etcd-arg (list), --kube-controller-manager-arg=bind-address=0.0.0.0->kube-controller-manager-arg (list), --kube-scheduler-arg->kube-scheduler-arg (list), --etcd-expose-metrics=true->etcd-expose-metrics: true, --write-kubeconfig-mode=644->write-kubeconfig-mode: "644", and PRESERVE the existing kubelet-arg resolv-conf=/etc/k3s-resolv.conf already in 52\'s config.yaml. For the JOIN identity (--server / --token), PREFER keeping it in the systemd env-file (EnvironmentFile K3S_URL/K3S_TOKEN) if present, or as `server:`/`token-file:` in config.yaml — choose the LOWEST-RISK option that keeps 52 able to rejoin etcd, and state which.',
        'DERIVE targetExecStart (per master needing reconcile): the ExecStart reduced to bare `/usr/local/bin/k3s server \\\\` (no inline flags), keeping the exact binary path observed and any EnvironmentFile reference. Note whether the join server-URL/token must remain in ExecStart or move to env-file/config.yaml so 52 still joins.',
        'Compare 52 against 51/53: state explicitly whether 51/53 are already config.yaml-only (so only 52 needs reconcile) OR whether 51/53 ALSO carry inline ExecStart args (so the standardization must cover them too). Adapt mastersToReconcile to ground truth — do not assume only 52.',
        'Return ONLY the structured JSON result, not a plan narrative.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['perMaster', 'mastersToReconcile', 'etcdHealthy', 'k3sVersion', 'fiftyOneFiftyThreeAlreadyClean', 'summary'],
      properties: {
        perMaster: {
          type: 'array',
          items: {
            type: 'object',
            required: ['host', 'hasConfigYaml', 'execStartHasInlineArgs', 'needsReconcile'],
            properties: {
              host: { type: 'string' },
              hasConfigYaml: { type: 'boolean' },
              configYaml: { type: 'string' },
              execStart: { type: 'string' },
              execStartHasInlineArgs: { type: 'boolean' },
              envFileKeys: { type: 'array', items: { type: 'string' } },
              effectiveArgsSummary: { type: 'string' },
              consolidatedConfigYaml: { type: 'string' },
              targetExecStart: { type: 'string' },
              joinIdentityPlan: { type: 'string' },
              nodeReady: { type: 'boolean' },
              needsReconcile: { type: 'boolean' },
            },
          },
        },
        mastersToReconcile: { type: 'array', items: { type: 'string' } },
        etcdHealthy: { type: 'boolean' },
        k3sVersion: { type: 'string' },
        fiftyOneFiftyThreeAlreadyClean: { type: 'boolean' },
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

// ---------------------------------------------------------------------------
// Phase 2 — prepare docs change + per-master runbook locally (branch + commit, NO push).
// Read-only on the cluster.
// ---------------------------------------------------------------------------
const prepareDocsTask = defineTask('prepare-docs', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Author k3s.instructions.md standardization note + canonical config.yaml; branch + local commit (no push)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer documenting the k3s control-plane config standardization in the Epaflix repo',
      task:
        'Make ONE git docs edit and commit it to a branch locally (NO push, NO PR yet): update ' +
        args.k3sInstructions + ' to record that ALL masters standardize on the /etc/rancher/k3s/config.yaml ' +
        'drop-in as the single source of truth, document the canonical consolidated config.yaml, and remove/fix ' +
        'the "master-52 has no config.yaml / args in systemd ExecStart" divergence framing (issue #' + args.issue + ').',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Respect CLAUDE.md (no secrets — do NOT write any token/URL values; use placeholders). Docs only — do NOT touch live config here, do NOT edit YAML manifests under 2-k3s.',
        'EDIT ' + args.k3sInstructions + ': in/near the existing "Control-plane metrics exposure (issue #121)" section that already shows the config.yaml drop-in, add a short subsection (reference issue #' + args.issue + ') stating: all 3 masters carry their server+kubelet args in /etc/rancher/k3s/config.yaml (single source of truth, upgrade-durable); the k3sup-generated systemd unit ExecStart is reduced to a bare `k3s server` (join identity preserved via env-file/config.yaml); CLI flags override config.yaml so inline ExecStart args must NOT coexist with config.yaml. Document the canonical CONSOLIDATED config.yaml for a master using the consolidatedConfigYaml derived in inspect (sanitise: tls-sans/node-ip etc are fine; redact any token). Keep markdown style consistent with the file.',
        'If the inspect phase found 51/53 ALSO carried inline args (not just 52), reflect that the standardization covered all affected masters; otherwise state 51/53 were already config.yaml-only and only 52 was reconciled.',
        'Add a one-line forward note: confirm under #44 (system-upgrade-controller bring-up) that the config.yaml drop-in survives controller-driven k3s upgrades (which may regenerate the systemd unit).',
        'If feedback is present in context (a prior breakpoint rejection), incorporate it before committing.',
        'VALIDATE: markdown lint / `git diff --check` for whitespace; note what was run. No helm/kustomize needed (docs-only).',
        'Create branch ' + args.branch + ' off origin/main (fetch first; reuse if it exists). Stage ONLY ' + args.k3sInstructions + '. ONE commit referencing #' + args.issue + ' (suggested subject: `docs(k3s): standardize masters on /etc/rancher/k3s/config.yaml single source of truth (#' + args.issue + ')`). End the commit body with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.',
        'The prBody MUST include a Test Plan section with checkbox items for post-merge verification: all 3 masters config.yaml-only (no inline ExecStart args); nodes Ready + etcd quorum 3/3; #121 metrics still up (cm/scheduler/etcd up=1); kubelet resolv-conf pin intact + CoreDNS healthy.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['branch', 'changedFiles', 'commitSha', 'diff', 'lintOk', 'prTitle', 'prBody'],
      properties: {
        branch: { type: 'string' },
        changedFiles: { type: 'array', items: { type: 'string' } },
        commitSha: { type: 'string' },
        diff: { type: 'string' },
        lintOk: { type: 'boolean' },
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
// Phase 3 — LIVE rollout: per master needing it, consolidate config.yaml + reduce ExecStart,
// ONE master at a time, health-gated, hard-stop + rollback on failure. Owner-approved.
// ---------------------------------------------------------------------------
const liveRolloutTask = defineTask('live-rollout', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Consolidate config.yaml + reduce systemd ExecStart on each diverging master (one at a time, health-gated, rollback on failure)',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'SRE executing an OWNER-APPROVED control-plane config refactor on the Epaflix k3s cluster (etcd quorum 2/3 — NEVER restart two masters at once)',
      task:
        'For each master in ' + JSON.stringify(args.mastersToReconcile) + ', make /etc/rancher/k3s/config.yaml the ' +
        'single source of truth by MERGING its effective server+kubelet args into config.yaml and REDUCING the ' +
        'systemd unit ExecStart to a bare `k3s server`, then daemon-reload + restart k3s — ONE MASTER AT A TIME, ' +
        'verifying full health before moving on. This was explicitly approved at the deploy gate. The effective ' +
        'running config MUST be UNCHANGED (pure refactor). STOP + ROLLBACK immediately if any master does not return healthy.',
      context: { ...args },
      instructions: [
        'Use the per-master plan from inspect (perMaster[].consolidatedConfigYaml, perMaster[].targetExecStart, perMaster[].joinIdentityPlan). Operate via EACH master\'s own ssh. NEVER touch the next master until the current one is fully Ready and etcd quorum is intact (re-check 3/3 healthy).',
        'PRE-CHECK (abort if not): confirm etcd quorum 3/3 healthy and all masters Ready BEFORE starting, and again before EACH master. If quorum is not 3/3, STOP and return success=false.',
        'BACKUP (reversible): `<ssh> \'sudo cp -a /etc/rancher/k3s/config.yaml /etc/rancher/k3s/config.yaml.bak-' + args.issue + ' 2>/dev/null || echo no-existing-config\'` and `<ssh> \'sudo cp -a /etc/systemd/system/k3s.service /etc/systemd/system/k3s.service.bak-' + args.issue + "'`. Capture both original contents into the per-master result.",
        'WRITE config.yaml: write the consolidatedConfigYaml for that master via a sudo tee from a heredoc to /etc/rancher/k3s/config.yaml. It MUST contain every effective arg (node-ip, advertise-address, flannel-iface, node-taint, tls-san list, disable list, etcd-arg list, kube-controller-manager-arg/kube-scheduler-arg bind-address=0.0.0.0, etcd-expose-metrics:true, write-kubeconfig-mode, AND the existing kubelet-arg resolv-conf=/etc/k3s-resolv.conf). Preserve the join identity per joinIdentityPlan (keep K3S_URL/K3S_TOKEN in the env-file, or server:/token-file: in config.yaml — whichever inspect chose). Do NOT write secret token VALUES into git/logs.',
        'REDUCE ExecStart: edit /etc/systemd/system/k3s.service so ExecStart is the bare `server` form (targetExecStart) with NO inline server/agent flags, keeping the binary path + any EnvironmentFile line. Then `<ssh> \'sudo systemctl daemon-reload\'`.',
        'RESTART: `<ssh> \'sudo systemctl restart k3s\'`. Then WAIT for readiness (poll up to ~3 min): from a DIFFERENT healthy master `' + args.masterSsh + " 'kubectl get nodes'` shows THIS master Ready; `kubectl get --raw=/healthz` ok; etcd back to 3/3 healthy; apiserver on the VIP answers.",
        'PROVE NO BEHAVIOUR CHANGE (per master): after restart confirm the effective config is unchanged — metrics ports still bound on 0.0.0.0 (`<ssh> \'sudo ss -ltnp | grep -E ":10257|:10259|:2381"\'`), kubelet still using resolv-conf (`<ssh> \'sudo grep -c resolv-conf /etc/rancher/k3s/config.yaml\'` >=1 and the kubelet process shows --resolv-conf=/etc/k3s-resolv.conf), node-ip/flannel-iface unchanged (node still on its 10.0.0.x InternalIP via `kubectl get node <host> -o wide`), and the control-plane taint still present.',
        'HARD-STOP + ROLLBACK: if a master fails to return Ready / etcd not 3/3 / metrics or resolv-conf regressed within the window, RESTORE that master from the .bak files (config.yaml + k3s.service), `daemon-reload`, `systemctl restart k3s`, wait for Ready, and return success=false with the failing host + what was restored. Do NOT proceed to the next master.',
        'Do NOT change anything else. Do NOT git-commit here. Return ONLY the structured JSON result with per-master status + before/after ExecStart + the .bak paths.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['success', 'mastersReconciled', 'perMaster', 'clusterHealthy', 'behaviourUnchanged', 'summary'],
      properties: {
        success: { type: 'boolean' },
        mastersReconciled: { type: 'array', items: { type: 'string' } },
        perMaster: { type: 'array', items: { type: 'object' } },
        clusterHealthy: { type: 'boolean' },
        behaviourUnchanged: { type: 'boolean' },
        rolledBackHost: { type: 'string' },
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
// Phase 4 — push + PR + merge per Epaflix merge policy (docs change).
// ---------------------------------------------------------------------------
const publishMergeTask = defineTask('publish-merge', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Push branch, open PR, rebase + merge per Epaflix policy',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer publishing an OWNER-APPROVED docs change to SpyrosPsarras/epaflix',
      task:
        'Push the branch, open a PR, and merge it per the Epaflix policy (merge-commit + mandatory rebase / ' +
        'semi-linear, PR required, 0 approvals).',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Rebase branch ' + args.branch + ' onto origin/main and `git push --force-with-lease` (strict up-to-date + required `validate` check block stale branches — see feedback_epaflix_merge_policy).',
        'Open a PR to main with the approved title/body (context: approvedPrTitle / approvedPrBody). Cross-link issue #' + args.issue + ' and refs #121 (surfaced this) and #44 (upgrade durability).',
        'Wait for the required `validate` check to pass, then merge with `gh pr merge --merge` (merge commit — never squash/rebase-merge). If `validate` flakes (unpinned kustomize rate-limit, see project_ci_kustomize_flake), `gh run rerun --failed` and re-wait.',
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
// Phase 5 — post-merge / post-rollout verification: parity + no regression.
// ---------------------------------------------------------------------------
const postVerifyTask = defineTask('post-verify', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Verify all masters config.yaml-only, no inline ExecStart args, nodes Ready, etcd 3/3, #121 metrics up, resolv-conf pin intact',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'k3s/Prometheus SRE verifying issue #' + args.issue + ' is resolved with NO regression',
      task:
        'Confirm the control-plane config is standardized and nothing regressed: every master\'s ExecStart is now ' +
        'a bare `server` (no inline server/kubelet flags) with all args in /etc/rancher/k3s/config.yaml, all masters ' +
        'Ready with etcd quorum 3/3, the #121 control-plane metrics are still up, and the kubelet resolv-conf pin + ' +
        'CoreDNS are healthy.',
      context: { ...args },
      instructions: [
        'Operate over SSH per master + kubectl via ' + args.masterSsh + '.',
        'PARITY: for EACH master, `<ssh> \'grep -E "server|ExecStart" /etc/systemd/system/k3s.service\'` and confirm the ExecStart has NO inline `--` server/agent flags (bare `server`). `<ssh> \'sudo test -f /etc/rancher/k3s/config.yaml && echo present\'` for all. Set parityConfirmed=true only if ALL masters are config.yaml-only.',
        'HEALTH: `' + args.masterSsh + " 'kubectl get nodes -o wide'` all masters Ready; etcd quorum 3/3 healthy (`kubectl get --raw=/healthz`).",
        '#121 METRICS (no regression): confirm metrics ports answer on 0.0.0.0 on each master (`<ssh> \'sudo ss -ltnp | grep -E ":10257|:10259|:2381"\'`) and, if reachable, up{job="kube-controller-manager"|"kube-scheduler"|"kube-etcd"}=1 via the Prometheus API (same method as #121). Capture values.',
        'DNS (no regression): kubelet resolv-conf pin intact (`<ssh> \'grep resolv-conf /etc/rancher/k3s/config.yaml\'` on each) and CoreDNS pods Running (`' + args.masterSsh + " 'kubectl -n kube-system get pods -l k8s-app=kube-dns'`).",
        'Set verified=true ONLY if: parityConfirmed AND all nodes Ready AND etcd 3/3 AND #121 metrics not regressed AND resolv-conf pin intact + CoreDNS healthy.',
        'Return ONLY the structured JSON result.',
      ],
      outputFormat: 'JSON',
    },
    outputSchema: {
      type: 'object',
      required: ['verified', 'parityConfirmed', 'nodesReady', 'etcdHealthy', 'metricsUp', 'resolvConfIntact', 'anomalies', 'summary'],
      properties: {
        verified: { type: 'boolean' },
        parityConfirmed: { type: 'boolean' },
        nodesReady: { type: 'boolean' },
        etcdHealthy: { type: 'boolean' },
        metricsUp: { type: 'object' },
        resolvConfIntact: { type: 'boolean' },
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
// Phase 6 — closeout: close #148, tick PR test plan (edit body), open follow-up (#44 durability).
// ---------------------------------------------------------------------------
const closeoutTask = defineTask('closeout', (args, taskCtx) => ({
  kind: 'agent',
  title: 'Close #148 with outcome, update PR test plan, open #44 upgrade-durability follow-up',
  execution: { model: MODEL },
  agent: {
    name: AGENT,
    prompt: {
      role: 'IaC engineer reconciling issues/PR after a verified change in SpyrosPsarras/epaflix',
      task:
        'Record the verified outcome: close issue #' + args.issue + ', tick the PR test-plan checkboxes by ' +
        'EDITING the PR body (never a new comment), and open a follow-up issue for the deferred #44 upgrade-durability check.',
      context: { ...args },
      instructions: [
        'Run from repoRoot=' + args.repoRoot + '. Repo is ' + args.repo + '.',
        'Comment on issue #' + args.issue + ' summarizing the verified result (masters reconciled: context.mastersReconciled; all masters now config.yaml-only single source of truth; nodes Ready + etcd 3/3; #121 metrics still up; resolv-conf pin intact) and CLOSE it. Cross-link #121 and #44.',
        'Edit the PR body (gh pr edit --body) to check off the Test Plan items that passed, recording observed evidence inline. Do NOT add a separate comment for the test plan (see feedback_pr_test_plans).',
        'Follow-up (CLAUDE.md policy): open a `gh issue` on ' + args.repo + ' (enhancement shape: ## Finding / ## Current state / ## Desired outcome / ## Notes, cross-linking #' + args.issue + ' and #44) to VERIFY the /etc/rancher/k3s/config.yaml drop-in + the reduced systemd unit SURVIVE a system-upgrade-controller-driven k3s upgrade (which may regenerate/replace the unit). If a suitable follow-up issue already exists, reference it instead and return its URL.',
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
      { host: 'k3s-master-51', ssh: 'ssh ubuntu@192.168.10.51', internalIp: '10.0.0.51' },
      { host: 'k3s-master-52', ssh: 'ssh ubuntu@192.168.10.52', internalIp: '10.0.0.52' },
      { host: 'k3s-master-53', ssh: 'ssh ubuntu@192.168.10.53', internalIp: '10.0.0.53' },
    ],
    masterSsh: 'ssh ubuntu@192.168.10.51',
    ns: 'kube-system',
    appName: 'observability',
    k3sInstructions: '.github/instructions/k3s.instructions.md',
    issue: '148',
    repo: 'SpyrosPsarras/epaflix',
    branch: 'issue-148-k3s-master-config-yaml-standardize',
    relatedIssues: ['121', '44'],
    ...inputs,
  };

  ctx.log('info', '#148 master config standardization — inspect (all 3) → prepare docs → owner gate (deploy+merge) → live rollout (1 master at a time, rollback-safe) → PR+merge → post-verify (parity + no regression) → closeout');

  // PHASE 1 — inspect (read-only).
  const inspect = await ctx.task(inspectTask, {
    repoRoot: cfg.repoRoot, masters: cfg.masters, masterSsh: cfg.masterSsh, ns: cfg.ns,
    k3sInstructions: cfg.k3sInstructions, issue: cfg.issue,
  });
  ctx.log('info', `Inspect: toReconcile=${JSON.stringify(inspect.mastersToReconcile)}; etcdHealthy=${inspect.etcdHealthy}; k3s=${inspect.k3sVersion}; 51/53 clean=${inspect.fiftyOneFiftyThreeAlreadyClean}`);

  // Guard: nothing to do (already standardized) — skip straight to docs/verify? If no master needs
  // reconcile, the live op is a no-op; still publish the docs note + verify parity, then closeout.
  const nothingLive = !inspect.mastersToReconcile || inspect.mastersToReconcile.length === 0;
  if (nothingLive) {
    ctx.log('info', 'No master carries inline ExecStart args — cluster already standardized on config.yaml. Docs-only path.');
  }
  if (!inspect.etcdHealthy && !nothingLive) {
    // Do not attempt a control-plane restart on an unhealthy quorum.
    const proceed = await ctx.breakpoint({
      question:
        'Issue #' + cfg.issue + ': inspect reports etcd is NOT confirmed 3/3 healthy. A control-plane restart on an ' +
        'unhealthy quorum is unsafe. Summary: ' + inspect.summary + '\n\nHow to proceed?',
      options: ['Abort (do not touch live)', 'Proceed anyway (I confirmed quorum is fine)'],
      expert: 'owner',
      tags: ['deploy', 'approval-gate'],
    });
    const pr = (proceed.response || '').toLowerCase();
    if (!proceed.approved || pr.includes('abort')) {
      return { success: false, decision: 'aborted-unhealthy', liveApplied: false, merged: false, reason: 'etcd-not-healthy', inspect };
    }
  }

  // PHASE 2 — prepare docs change locally (branch + commit, no push). Refine loop on gate rejection.
  let change = await ctx.task(prepareDocsTask, {
    repoRoot: cfg.repoRoot, k3sInstructions: cfg.k3sInstructions, branch: cfg.branch, issue: cfg.issue,
    perMaster: inspect.perMaster, mastersToReconcile: inspect.mastersToReconcile,
    fiftyOneFiftyThreeAlreadyClean: inspect.fiftyOneFiftyThreeAlreadyClean,
  });
  ctx.log('info', `Prepared docs: branch=${change.branch} commit=${change.commitSha} files=${JSON.stringify(change.changedFiles)} lintOk=${change.lintOk}`);

  // Build a compact per-master before→after preview for the gate.
  const planPreview = (inspect.perMaster || [])
    .filter((m) => m.needsReconcile)
    .map((m) => '• ' + m.host + ': ExecStart inline args -> config.yaml; join=' + (m.joinIdentityPlan || 'preserved'))
    .join('\n') || '(none — already standardized)';

  // GATE (deploy + destructive-git) — ONE mandatory owner approval covering BOTH the live
  // control-plane reconciliation AND the subsequent PR merge. Retry/refine loop on rejection.
  let approved = false;
  let lastFeedback = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (lastFeedback) {
      change = await ctx.task(prepareDocsTask, {
        repoRoot: cfg.repoRoot, k3sInstructions: cfg.k3sInstructions, branch: cfg.branch, issue: cfg.issue,
        perMaster: inspect.perMaster, mastersToReconcile: inspect.mastersToReconcile,
        fiftyOneFiftyThreeAlreadyClean: inspect.fiftyOneFiftyThreeAlreadyClean,
        feedback: lastFeedback, attempt: attempt + 1,
      });
    }
    const gate = await ctx.breakpoint({
      question:
        'Issue #' + cfg.issue + ' — standardize k3s masters on /etc/rancher/k3s/config.yaml (single source of truth).\n\n' +
        'INSPECT: etcd healthy=' + inspect.etcdHealthy + '; k3s=' + inspect.k3sVersion + '; 51/53 already clean=' + inspect.fiftyOneFiftyThreeAlreadyClean + '\n' +
        'Masters to reconcile: ' + JSON.stringify(inspect.mastersToReconcile) + '\n' +
        (inspect.risks && inspect.risks.length ? 'Risks: ' + JSON.stringify(inspect.risks) + '\n' : '') +
        '\nPER-MASTER PLAN (pure refactor — effective args UNCHANGED):\n' + planPreview + '\n\n' +
        'THIS GATE AUTHORIZES TWO THINGS:\n' +
        '1) LIVE (deploy): for each master above, ONE AT A TIME, write the consolidated config.yaml + reduce the systemd unit ExecStart to bare `server`, daemon-reload + `systemctl restart k3s`, health-gating etcd quorum 3/3 + node Ready + #121 metrics + resolv-conf between each. Backed up to .bak-' + cfg.issue + ' and rolled back on failure. Control-plane restart — quorum risk if mishandled.\n' +
        '2) GIT (merge): push + PR + merge the docs change. Files: ' + JSON.stringify(change.changedFiles) + '\n\n' +
        '--- docs diff ---\n' + (change.diff || '(no diff captured)').slice(0, 4000) + '\n\nProceed with BOTH?',
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
      return { success: false, decision: 'aborted', liveApplied: false, merged: false, reason: 'not-approved', feedback: gate.response || gate.feedback || '', inspect };
    }
    lastFeedback = gate.response || gate.feedback || 'Changes requested';
    ctx.log('info', `Gate requested changes (attempt ${attempt + 1}); refining docs change.`);
  }
  if (!approved) {
    return { success: false, decision: 'aborted', liveApplied: false, merged: false, reason: 'not-approved-after-retries', inspect };
  }

  // PHASE 3 — LIVE rollout (skip if nothing to reconcile).
  let live = { success: true, mastersReconciled: [], clusterHealthy: true, behaviourUnchanged: true, summary: 'no-op (already standardized)' };
  if (!nothingLive) {
    live = await ctx.task(liveRolloutTask, {
      masters: cfg.masters, mastersToReconcile: inspect.mastersToReconcile, perMaster: inspect.perMaster,
      masterSsh: cfg.masterSsh, issue: cfg.issue,
    });
    ctx.log('info', `Live rollout: success=${live.success}; reconciled=${JSON.stringify(live.mastersReconciled)}; healthy=${live.clusterHealthy}; unchanged=${live.behaviourUnchanged}`);

    if (!live.success || !live.clusterHealthy || !live.behaviourUnchanged) {
      const recover = await ctx.breakpoint({
        question:
          'LIVE control-plane reconciliation did NOT complete cleanly (deploy).\n' +
          'Masters reconciled: ' + JSON.stringify(live.mastersReconciled) + '\n' +
          'Cluster healthy: ' + live.clusterHealthy + '; behaviour unchanged: ' + live.behaviourUnchanged + '\n' +
          (live.rolledBackHost ? 'Rolled back: ' + live.rolledBackHost + '\n' : '') +
          'Summary: ' + live.summary + '\n\n' +
          'The docs change has NOT been merged yet. How to proceed?',
        options: ['Retry live rollout', 'Stop here (do not merge)'],
        expert: 'owner',
        tags: ['deploy', 'verification-gate'],
      });
      const rr = (recover.response || '').toLowerCase();
      if (recover.approved && rr.includes('retry')) {
        live = await ctx.task(liveRolloutTask, {
          masters: cfg.masters, mastersToReconcile: inspect.mastersToReconcile, perMaster: inspect.perMaster,
          masterSsh: cfg.masterSsh, issue: cfg.issue, attempt: 2,
        });
      }
      if (!live.success || !live.clusterHealthy || !live.behaviourUnchanged) {
        return { success: false, decision: 'live-failed', liveApplied: false, merged: false, reason: 'live-rollout-incomplete', live };
      }
    }
  }

  // PHASE 4 — push + PR + merge (docs).
  const pub = await ctx.task(publishMergeTask, {
    repoRoot: cfg.repoRoot, branch: change.branch, issue: cfg.issue, repo: cfg.repo,
    approvedPrTitle: change.prTitle, approvedPrBody: change.prBody,
  });
  ctx.log('info', `Merged: ${pub.merged}; PR=${pub.prUrl}; sha=${pub.mergeSha}`);

  // PHASE 5 — post verify, with an owner recovery gate on failure.
  let post = await ctx.task(postVerifyTask, {
    masters: cfg.masters, masterSsh: cfg.masterSsh, ns: cfg.ns, issue: cfg.issue,
  });
  if (!pub.merged || !post.verified) {
    const recover = await ctx.breakpoint({
      question:
        'Post verification incomplete.\n' +
        'Merged: ' + pub.merged + '\n' +
        'Parity (config.yaml-only): ' + post.parityConfirmed + '\n' +
        'Nodes Ready: ' + post.nodesReady + '; etcd healthy: ' + post.etcdHealthy + '\n' +
        '#121 metrics: ' + JSON.stringify(post.metricsUp) + '; resolv-conf intact: ' + post.resolvConfIntact + '\n' +
        'Anomalies: ' + JSON.stringify(post.anomalies) + '\n' +
        'Summary: ' + post.summary + '\n\nHow to proceed?',
      options: ['Re-verify (allow more time)', 'Continue to closeout (accept state)', 'Stop here'],
      expert: 'owner',
      tags: ['verification-gate'],
    });
    const r = (recover.response || '').toLowerCase();
    if (recover.approved && r.includes('re-verify')) {
      post = await ctx.task(postVerifyTask, {
        masters: cfg.masters, masterSsh: cfg.masterSsh, ns: cfg.ns, issue: cfg.issue, attempt: 2,
      });
    } else if (!recover.approved || r.includes('stop')) {
      return { success: false, decision: 'verify-stop', liveApplied: !nothingLive, merged: pub.merged, prUrl: pub.prUrl, reason: 'verification-stop', post };
    }
  }

  // PHASE 6 — closeout.
  const close = await ctx.task(closeoutTask, {
    repoRoot: cfg.repoRoot, issue: cfg.issue, repo: cfg.repo, prUrl: pub.prUrl,
    mastersReconciled: live.mastersReconciled,
  });
  ctx.log('info', `Closeout: #${cfg.issue}=${close.issueState}; follow-up=${close.followUpIssueUrl}`);

  return {
    success: true,
    decision: nothingLive ? 'already-standardized-docs-only' : 'applied',
    liveApplied: !nothingLive,
    mastersReconciled: live.mastersReconciled,
    merged: pub.merged,
    prUrl: pub.prUrl,
    parityConfirmed: post.parityConfirmed,
    issueState: close.issueState,
    followUpIssue: close.followUpIssueUrl,
  };
}
