# K3s Automated Node Upgrades

Automated rolling upgrades for all K3s masters and workers using the official [system-upgrade-controller](https://github.com/rancher/system-upgrade-controller) by Rancher.

> **GitOps-managed.** Both the controller (`controller/`) and the upgrade Plans (`plans/`) are reconciled by automated ArgoCD Applications — `system-upgrade-controller` and `system-upgrade-plans` (Issue #74) — under the app-of-apps. There is no manual `kubectl --context epaflix apply -f` step; merging to `main` applies them via selfHeal. The `kubectl` recipes below are for monitoring, manual pause/pin, and uninstall only.

---

## How It Works

The system-upgrade-controller applies an explicitly **pinned** K3s version (`spec.version`) declared on each Plan — the cluster currently pins `v1.35.5+k3s1`. It does **not** track the moving `stable` channel; every version bump is a reviewed git change (see the [Future-Upgrade SOP](#future-upgrade-sop) and issue #130). When the committed `spec.version` is bumped and merged:

1. **Masters are upgraded first**, one at a time (`concurrency: 1`), to preserve etcd quorum
2. **Workers wait** until every master has completed its upgrade (`prepare: k3s-server`)
3. **Workers are upgraded one at a time** (`concurrency: 1`), keeping 3 of the 4 workers running for workloads

Before each node is upgraded, it is **cordoned** (no new pods scheduled) and **drained** (existing pods evicted gracefully). On success the node is automatically uncordoned. On a **failed** drain it is not - see [Drain timeout](#a-drain-blew-its-timeout-and-the-node-is-still-cordoned).

```
Upgrade order:

[Master] k3s-master-51  ──►  [Master] k3s-master-52  ──►  [Master] k3s-master-53
                                                                       │
                                                                       ▼
                                                             All masters done
                                                                       │
                                                   ┌───────────────────┘
                                                   ▼
                                     [Worker] k3s-worker-61
                                                   │
                                                   ▼
                                     [Worker] k3s-worker-62
                                                   │
                                                   ▼
                                     [Worker] k3s-worker-63
                                                   │
                                                   ▼
                                     [Worker] k3s-worker-65
```

---

## Prerequisites

- `kubectl` configured and pointing at the cluster
- All nodes `Ready` before starting (`kubectl --context epaflix get nodes`)

---

## Installation (GitOps-Managed)

This stack is **not** installed with `kubectl --context epaflix apply -f`. It is reconciled by two automated ArgoCD Applications under the app-of-apps:

| Application                  | Path                                         | What it manages                                  |
|------------------------------|----------------------------------------------|--------------------------------------------------|
| `system-upgrade-controller`  | `2-k3s/maintenance/system-upgrade/controller`| Vendored upstream CRD + controller Deployment + RBAC (v0.20.1) |
| `system-upgrade-plans`       | `2-k3s/maintenance/system-upgrade/plans`     | The `k3s-server` + `k3s-agent` upgrade Plans (pinned `spec.version`) |

Both Applications run `automated` with `selfHeal: true, prune: false` and `ServerSideApply=true`. Merging changes to `main` is the install/update mechanism — ArgoCD applies them automatically. The controller creates the `system-upgrade` namespace and its RBAC; the Plans App then populates the Plans.

Verify the controller is running:

```bash
kubectl --context epaflix -n system-upgrade rollout status deployment system-upgrade-controller
kubectl --context epaflix -n system-upgrade get pods
```

Verify the plans were accepted:

```bash
kubectl --context epaflix -n system-upgrade get plans
# Expected output (VERSION column carries the pinned tag, CHANNEL is empty):
# NAME          IMAGE                    CHANNEL   VERSION          LATEST
# k3s-agent     rancher/k3s-upgrade                v1.35.5+k3s1     v1.35.5+k3s1
# k3s-server    rancher/k3s-upgrade                v1.35.5+k3s1     v1.35.5+k3s1
```

Once the Plans are applied, the controller compares the pinned `spec.version` to each node's running version. If the cluster already matches the pinned version, no upgrade jobs will run (this is the normal steady state).

---

## Monitoring an Upgrade in Progress

### Watch upgrade jobs as they run

```bash
# Watch all upgrade jobs across master and worker plans
kubectl --context epaflix -n system-upgrade get jobs -w

# View all upgrade-related pods
kubectl --context epaflix -n system-upgrade get pods -o wide
```

### Check which nodes have been upgraded

```bash
# Shows current K3s version per node
kubectl --context epaflix get nodes -o wide

# Show only version column
kubectl --context epaflix get nodes -o custom-columns='NAME:.metadata.name,VERSION:.status.nodeInfo.kubeletVersion,STATUS:.status.conditions[-1].type'
```

### Stream logs from an active upgrade job

```bash
# Replace <job-name> with the job name shown in `kubectl --context epaflix -n system-upgrade get jobs`
kubectl --context epaflix -n system-upgrade logs -f job/<job-name>
```

### Check plan status

```bash
kubectl --context epaflix -n system-upgrade describe plan k3s-server
kubectl --context epaflix -n system-upgrade describe plan k3s-agent
```

---

## Cluster Node Reference

| Role   | Hostname      | IP             | Upgrade Order |
|--------|---------------|----------------|---------------|
| Master | k3s-master-51 | 192.168.10.51  | 1st           |
| Master | k3s-master-52 | 192.168.10.52  | 2nd           |
| Master | k3s-master-53 | 192.168.10.53  | 3rd           |
| Worker | k3s-worker-61 | 192.168.10.61  | Batch 1       |
| Worker | k3s-worker-62 | 192.168.10.62  | Batch 1       |
| Worker | k3s-worker-63 | 192.168.10.63  | Batch 2       |
| Worker | k3s-worker-65 | 192.168.10.65  | Batch 2       |

---

## Manual Operations

### Bump to a new K3s version (the normal upgrade workflow)

Upgrades are driven by the pinned `spec.version` in git, **not** by editing the
live cluster. The canonical bump is a reviewed PR (see the
[Future-Upgrade SOP](#future-upgrade-sop)):

1. Discover the next stable tag — `curl` the stable channel and resolve where it
   redirects (see [Check the latest stable K3s tag](#check-the-latest-stable-k3s-tag-to-discover-the-next-pin)),
   or browse https://github.com/k3s-io/k3s/releases.
2. Edit **both** Plans in `2-k3s/maintenance/system-upgrade/plans/system-upgrade-plans.yaml`
   — set `spec.version` to the new tag on `k3s-server` AND `k3s-agent` (they must match).
3. Open a PR, soak. On merge, ArgoCD selfHeal applies the committed version and
   the controller rolls masters then workers.

A live `kubectl --context epaflix -n system-upgrade edit plan ...` is a stop-gap only — `selfHeal`
will revert it to the in-git version within minutes.

### Pause upgrades temporarily

> **Note:** the `system-upgrade-plans` App runs `selfHeal: true`, so a live
> `kubectl patch` will be reverted by ArgoCD within minutes. To pause durably,
> change the Plan in git (e.g. set `concurrency: 0` in `plans/`), or temporarily
> suspend the `system-upgrade-plans` Application in ArgoCD. The patch below is a
> stop-gap only.

```bash
# Suspend both plans to stop the controller from triggering new upgrades
kubectl --context epaflix -n system-upgrade patch plan k3s-server --type=merge -p '{"spec":{"concurrency":0}}'
kubectl --context epaflix -n system-upgrade patch plan k3s-agent  --type=merge -p '{"spec":{"concurrency":0}}'

# Resume — let ArgoCD selfHeal restore the in-git concurrency, or force a sync:
argocd app sync system-upgrade-plans
```

### Manually trigger an upgrade check now

```bash
# Restart the controller — it will re-evaluate both plans immediately
kubectl --context epaflix -n system-upgrade rollout restart deployment system-upgrade-controller
```

### Check the latest stable K3s tag (to discover the next pin)

The cluster no longer tracks this channel — use it only to **discover** the tag
to pin in git. The stable channel endpoint redirects to the GitHub release tag:

```bash
# Resolve the redirect target (the exact release tag to pin)
curl -sI -o /dev/null -w '%{redirect_url}\n' https://update.k3s.io/v1-release/channels/stable

# Or read the JSON "latest" field
curl -sL https://update.k3s.io/v1-release/channels/stable | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('latest','not found'))"
```

Take the resulting tag, pin it on both Plans, and open a PR (see the bump
workflow above and the [Future-Upgrade SOP](#future-upgrade-sop)).

---

## Verification After Upgrade

```bash
# 1. Confirm all nodes are Ready and on the new version
kubectl --context epaflix get nodes -o wide

# 2. Confirm all system pods are healthy
kubectl --context epaflix get pods -A | grep -v Running | grep -v Completed

# 3. Check that servarr workloads recovered
kubectl --context epaflix get pods -n servarr

# 4. Verify K3s service is active on each node
for ip in 51 52 53; do
  echo "=== master-$ip ==="
  ssh ubuntu@192.168.10.$ip 'systemctl is-active k3s && k3s --version'
done

for ip in 61 62 63 65; do
  echo "=== worker-$ip ==="
  ssh ubuntu@192.168.10.$ip 'systemctl is-active k3s-agent && k3s --version'
done
```

### #148/#121 invariant checklist (run on all 3 masters after any k3s bump)

> **Why this exists (issue #201).** The #148 reconcile standardized all 3 masters on
> `/etc/rancher/k3s/config.yaml` as the single source of truth, reduced each systemd
> unit's `ExecStart` to a bare `/usr/local/bin/k3s server`, pinned pod-side DNS via
> `/etc/k3s-resolv.conf`, kept the #121 control-plane metrics binds in the config file,
> and relocated the master-53 (and 52) join token out of the unit into a `0600`
> `K3S_TOKEN` env-file. **All of this is host-local state applied out-of-band — it is
> NOT reconciled by ArgoCD.** A system-upgrade-controller k3s upgrade is a **binary
> swap** (see the [Future-Upgrade SOP](#future-upgrade-sop) note), so by construction it
> does NOT re-run `install.sh` and does NOT regenerate the systemd unit — the invariants
> are *expected* to survive. This checklist is the post-upgrade **confirmation** of that
> expectation; run it at the next real k3s version bump (cross-link #169). If any check
> fails, follow the [Remediation](#remediation--if-the-upgrade-regenerated-the-systemd-unit)
> recipe below.

For **each** master (51, 52, 53), assert the five invariants:

```bash
for ip in 51 52 53; do
  echo "======================= master-$ip ======================="

  # 1. ExecStart is the BARE binary — NO inline server/kubelet flags reintroduced.
  #    Expect exactly: ExecStart=/usr/local/bin/k3s server   (no trailing args)
  ssh ubuntu@192.168.10.$ip 'systemctl cat k3s.service | grep -n ExecStart'

  # 2. config.yaml present and the effective source of truth.
  #    Expect: cluster-init: true on 51; server: <endpoint> on 52/53;
  #    kubelet-arg resolv-conf pin; #121 metrics binds; etcd-expose-metrics: true;
  #    and NO token/secret line in the file.
  ssh ubuntu@192.168.10.$ip 'sudo test -f /etc/rancher/k3s/config.yaml && echo "config.yaml: PRESENT" || echo "config.yaml: MISSING"'
  ssh ubuntu@192.168.10.$ip 'sudo grep -E "^(cluster-init|server):" /etc/rancher/k3s/config.yaml'
  ssh ubuntu@192.168.10.$ip 'sudo grep -E "resolv-conf=/etc/k3s-resolv.conf|bind-address=0.0.0.0|etcd-expose-metrics" /etc/rancher/k3s/config.yaml'
  ssh ubuntu@192.168.10.$ip 'sudo grep -iE "token" /etc/rancher/k3s/config.yaml && echo "WARNING: token leaked into config.yaml" || echo "config.yaml: no token (good)"'

  # 3. resolv-conf DNS pin == nameserver 192.168.10.30
  #    Expect exactly one line: nameserver 192.168.10.30
  ssh ubuntu@192.168.10.$ip 'cat /etc/k3s-resolv.conf'

  # 4. Token env-file: present, mode 0600, referenced by EnvironmentFile=; no inline --token.
  #    (51 = founding member, no join token — the env-file may be absent there; 52/53 MUST have it.)
  ssh ubuntu@192.168.10.$ip 'sudo stat -c "%a %n" /etc/systemd/system/k3s.service.env 2>/dev/null || echo "k3s.service.env: absent (expected only on 51)"'
  ssh ubuntu@192.168.10.$ip 'systemctl cat k3s.service | grep -E "EnvironmentFile=" || echo "no EnvironmentFile= (expected only on 51)"'
  ssh ubuntu@192.168.10.$ip 'systemctl cat k3s.service | grep -q -- "--token" && echo "WARNING: --token reintroduced inline in unit" || echo "unit: no inline --token (good)"'
done
```

Then assert **cluster-wide** health (etcd quorum + #121 metrics), once, from your kubectl host:

```bash
# 5a. etcd quorum 3/3 — all 3 masters Ready with control-plane,etcd roles
kubectl --context epaflix get nodes -o wide   # real ROLES (control-plane,etcd,master) + internal IPs; confirm 3 masters Ready
kubectl --context epaflix get --raw='/healthz/etcd'   # Expected: ok

# 5b. #121 control-plane metrics up=1 for all 3 components.
#     Via Prometheus/promtool (point at the in-cluster Prometheus, or run inside the pod):
#     Expect up == 1 for every target of each job.
kubectl --context epaflix -n observability exec prometheus-kube-prometheus-stack-prometheus-0 -c prometheus -- \
  promtool query instant http://localhost:9090 \
  'up{job=~"kube-controller-manager|kube-scheduler|kube-etcd"}'
# Or from any host with promtool + Prometheus reachable:
#   promtool query instant http://<prometheus>:9090 'up{job=~"kube-controller-manager|kube-scheduler|kube-etcd"}'
# Every returned series MUST be value 1. A 0 (or a missing series) means a master's
# config.yaml metrics binds were lost — remediate that master per below.
```

### Remediation — if the upgrade regenerated the systemd unit

> **Not expected.** Per the SUC binary-swap design (below), the unit is **not**
> regenerated by a k3s upgrade. This recipe is the fallback for the unexpected case
> where a check above fails — e.g. an inline `ExecStart`/`--token` reappeared, or the
> `config.yaml` / `resolv-conf` pin was clobbered.

Work **one master at a time, health-gated** — never restart the next master until the
previous one has rejoined with etcd quorum restored:

```bash
ip=51   # repeat for 52, then 53 — one at a time, in this order

# A. Inspect what the upgrade left behind, and the on-host #148 backups:
ssh ubuntu@192.168.10.$ip 'systemctl cat k3s.service | grep ExecStart'
ssh ubuntu@192.168.10.$ip 'ls -la /etc/systemd/system/k3s.service*.bak-148 /etc/rancher/k3s/config.yaml*.bak-148 2>/dev/null'

# B. Restore the trimmed unit + config from the .bak-148 backups (or re-apply by hand
#    against the canonical form in .github/instructions/k3s.instructions.md
#    — "Single source of truth: /etc/rancher/k3s/config.yaml (issue #148)"):
#    - ExecStart must be the bare:  ExecStart=/usr/local/bin/k3s server
#    - config.yaml = full single-source-of-truth (51: cluster-init: true; 52/53: server:),
#      with resolv-conf pin + #121 metrics binds + etcd-expose-metrics, NO token.
#    - 52/53 token stays ONLY in /etc/systemd/system/k3s.service.env (K3S_TOKEN, mode 0600),
#      referenced via EnvironmentFile= in the unit. NEVER inline, NEVER in config.yaml.
ssh ubuntu@192.168.10.$ip 'sudo cp /etc/systemd/system/k3s.service.bak-148 /etc/systemd/system/k3s.service'   # if a backup exists
ssh ubuntu@192.168.10.$ip 'sudo chmod 0600 /etc/systemd/system/k3s.service.env'   # 52/53 only

# C. Reload systemd and restart k3s on THIS master only:
ssh ubuntu@192.168.10.$ip 'sudo systemctl daemon-reload && sudo systemctl restart k3s'

# D. HEALTH GATE — wait for this master to be Ready and etcd quorum 3/3 BEFORE the next:
ssh ubuntu@192.168.10.$ip 'systemctl is-active k3s'
kubectl --context epaflix get nodes        # all 3 masters Ready
kubectl --context epaflix get --raw='/healthz/etcd'   # Expected: ok
# Only once etcd is ok and all masters are Ready, proceed to the next ip.
```

After all affected masters are remediated, re-run the
[invariant checklist](#148121-invariant-checklist-run-on-all-3-masters-after-any-k3s-bump)
to confirm green, then re-confirm #121 metrics `up=1`.

---

## Troubleshooting

### Upgrade job stuck or not starting

```bash
# Describe the upgrade plan to see controller events
kubectl --context epaflix -n system-upgrade describe plan k3s-server

# Check for failed jobs
kubectl --context epaflix -n system-upgrade get jobs
kubectl --context epaflix -n system-upgrade describe job <job-name>

# Check controller logs for errors
kubectl --context epaflix -n system-upgrade logs deployment/system-upgrade-controller
```

### Node is stuck cordoned after a failed upgrade

```bash
# Manually uncordon the node
kubectl --context epaflix uncordon <node-name>

# Example:
kubectl --context epaflix uncordon k3s-master-51
```

### A drain blew its timeout and the node is still cordoned

Both Plans set `drain.timeout: 300s` (issue #413). Read what that actually
buys before relying on it:

- The value is passed straight through as `kubectl --context epaflix drain --timeout`. There is
  **no controller-side timer** and no alert of its own.
- On expiry the drain container exits non-zero. `RestartPolicy` is `Never` and
  `BackoffLimit` is `2`, so the Job tries **3 times total** and then goes
  `Failed`.
- Upstream does **not** un-cordon on failure. The node is left cordoned and
  the roll does not continue past it.

So the timeout converts "hangs forever while cordoned" into "gives up after 3
attempts while cordoned". It bounds the stall; it does not self-heal.

Find it and recover:

```bash
# Which node is cordoned, and did its drain Job fail?
kubectl --context epaflix get nodes | grep SchedulingDisabled
kubectl --context epaflix -n system-upgrade get jobs \
  -o custom-columns=NAME:.metadata.name,FAILED:.status.failed,SUCCEEDED:.status.succeeded

# The reason the eviction was refused (PDB, RWO volume, unmanaged pod)
kubectl --context epaflix -n system-upgrade logs job/<job-name> -c drain
```

Fix the blocker first (that is the real bug - a PDB that can never be
satisfied, or a volume that will not detach), then un-cordon:

```bash
kubectl --context epaflix uncordon <node-name>
```

`K3sNodeVersionSkew` is what catches this from the outside: the roll stops
with the fleet on mixed kubelet versions, and that alert fires.

> Keep `drain.timeout` under the Job's `ActiveDeadlineSeconds` (default
> `600s`, settable via the `default-controller-env` ConfigMap). A larger value
> is cut short by the active deadline anyway - upstream only logs
> `drain timeout exceeds active deadline seconds` and carries on.

### Upgrade job fails with "node not found" or permission errors

```bash
# Re-sync the controller App to refresh the vendored CRD + RBAC
argocd app sync system-upgrade-controller

# Re-sync the plans App
argocd app sync system-upgrade-plans
```

### Worker upgrade starts before masters are done

This should not happen due to the `prepare` step in the agent plan. If it does, check:

```bash
# Verify the prepare step references the correct plan name
kubectl --context epaflix -n system-upgrade get plan k3s-agent -o yaml | grep -A5 prepare
# Should show: args: [prepare, k3s-server]
# Canonical source: 2-k3s/maintenance/system-upgrade/plans/system-upgrade-plans.yaml
```

### Check etcd health during/after master upgrade

```bash
# On any master node
ssh ubuntu@192.168.10.51
sudo k3s etcd-snapshot ls  # Verify etcd is responsive

# From kubectl
kubectl --context epaflix get --raw='/healthz/etcd'
# Expected: ok
```

---

## Uninstalling

To stop auto-upgrades entirely (GitOps-managed — delete in git, not the cluster):

```bash
# 1. Remove the `system-upgrade-plans` App from
#    2-k3s/11.argocd/apps/kustomization.yaml (and delete its app file).
#    With prune: false, the live Plans persist, so also delete them:
kubectl --context epaflix -n system-upgrade delete plan k3s-server k3s-agent

# 2. (Optional) Remove the `system-upgrade-controller` App the same way, then:
kubectl --context epaflix -n system-upgrade delete deployment system-upgrade-controller
```

Because both Apps run `selfHeal: true`, deleting live objects without first
removing the App from git will be reverted by ArgoCD. Always remove from git
first.

---

## Future-Upgrade SOP

The K3s upgrade target is **explicitly pinned** in git (issue #130). The cluster
currently pins `v1.35.5+k3s1`. There is no unsupervised channel-driven roll —
every bump is a reviewed PR.

### Host-local config invariants survive the roll by construction (issue #201)

The SUC k3s upgrade is a **binary swap**: the `rancher/k3s-upgrade` job replaces the
`/usr/local/bin/k3s` binary on each node and restarts the service. It does **not** re-run
`install.sh` and does **not** regenerate the systemd unit. Therefore the host-local
out-of-band state from issue #148 — the single-source-of-truth
`/etc/rancher/k3s/config.yaml`, the reduced bare-`ExecStart` unit, the
`/etc/k3s-resolv.conf` DNS pin (`192.168.10.30`), the #121 control-plane metrics binds,
and the `0600` `K3S_TOKEN` env-file (52/53) — is **expected to survive the upgrade
unchanged**. Because none of it is reconciled by ArgoCD, the survival was never
*asserted* after an upgrade; the
[#148/#121 invariant checklist](#148121-invariant-checklist-run-on-all-3-masters-after-any-k3s-bump)
in [Verification After Upgrade](#verification-after-upgrade) fills that gap and must be
run at the next real k3s bump. If, contrary to the binary-swap design, the unit *is*
regenerated, use the
[Remediation recipe](#remediation--if-the-upgrade-regenerated-the-systemd-unit).
Cross-links: #148 (config consolidation), #121 (control-plane metrics), #44 (SUC
mechanism), #130 (pinned-version SOP), #169 (next-bump review where the checklist runs).

### Where the version lives

`spec.version` on **both** the `k3s-server` and `k3s-agent` Plans in
`2-k3s/maintenance/system-upgrade/plans/system-upgrade-plans.yaml` is the single
git-reviewed source of the upgrade target. The two tags must always match.

To upgrade:

1. **Discover the next stable tag.** The stable channel endpoint redirects to the
   GitHub release tag:
   ```bash
   curl -sI -o /dev/null -w '%{redirect_url}\n' https://update.k3s.io/v1-release/channels/stable
   ```
   (or browse https://github.com/k3s-io/k3s/releases).
2. **Edit both Plans** to the new tag — `spec.version` on `k3s-server` AND `k3s-agent`.
3. **Open a PR and soak.** On merge, ArgoCD selfHeal applies the committed
   version automatically and the controller rolls masters (one at a time) then
   workers (one at a time) - 7 sequential drains, so budget the window for the
   whole fleet, not for two batches.

### At-a-glance status

```bash
# Canonical status check — Plans (LATEST/applied version) + any in-flight Jobs
kubectl --context epaflix -n system-upgrade get plans,jobs -o wide

# Per-node running K3s/kubelet version
kubectl --context epaflix get nodes -o wide
```

### Pre-upgrade etcd snapshot

Before a master roll, take a fresh on-demand snapshot so you have a clean
restore point:

```bash
ssh ubuntu@192.168.10.51 'sudo k3s etcd-snapshot save --name pre-upgrade-$(date +%F)'
```

Snapshots land **on-node** under `/var/lib/rancher/k3s/server/db/snapshots/`.
List and (if needed) restore:

```bash
# List available snapshots on a master
ssh ubuntu@192.168.10.51 'sudo k3s etcd-snapshot ls'

# Restore (DESTRUCTIVE — cluster-reset; run on a single master, others rejoin):
#   sudo systemctl stop k3s
#   sudo k3s server --cluster-reset \
#     --cluster-reset-restore-path=/var/lib/rancher/k3s/server/db/snapshots/<snapshot>
```

### CNPG switchover expectation

When the node hosting the CloudNativePG **primary** is cordoned/drained during
the roll, CNPG performs an **automatic switchover** (brief connection blip). This
is expected. Confirm a healthy new primary before letting the roll continue:

```bash
kubectl --context epaflix get cluster -A
kubectl --context epaflix cnpg status <cluster> -n <ns>
```

**NEVER force-manage or delete postgres pods during the roll** — let CNPG fail
over on its own. (Cross-ref issue #74.)

### Rollback notes

To **halt an in-flight unintended roll**:

1. **Suspend the ArgoCD App first - this is the only step that holds.**
   ```bash
   kubectl --context epaflix -n argocd patch application system-upgrade-controller \
     --type merge -p '{"spec":{"syncPolicy":{"automated":null}}}'
   kubectl --context epaflix -n argocd patch application system-upgrade-plans \
     --type merge -p '{"spec":{"syncPolicy":{"automated":null}}}'
   ```
   Both Apps run `selfHeal: true`
   (`2-k3s/11.argocd/apps/app-system-upgrade-controller.yaml`,
   `app-system-upgrade-plans.yaml`). Until automated sync is off, every cluster-side
   change below is reverted by ArgoCD within minutes.
2. **Then stop the controller** so it does not re-trigger:
   ```bash
   kubectl --context epaflix -n system-upgrade scale deployment \
     system-upgrade-controller --replicas=0
   ```
   Ordering matters: run bare, before step 1, this scale is undone by `selfHeal`
   and reads as "the stop lever does not work" (found by #413's readiness review).
3. Pin both Plans back to the **last-good version** in git (and/or set
   `concurrency: 0` on both Plans) so the state is correct once the Apps are
   un-suspended.

> **Prepare the lever before the window, not during it.** The roll drains every
> worker, and Traefik is a single replica pinned to `k3s-worker-62` by an RWO
> `local-path` PVC holding live ACME state, with no PDB. While 62 drains, every
> `*.epaflix.com` route is down - **including the ArgoCD UI you would use to
> suspend the App**. Suspend the Apps (or have a `kubectl` shell already open
> against `192.168.10.100:6443`, which does not traverse Traefik) *before*
> starting the roll.

Note that **K3s binary downgrade is unsupported**. A true rollback to an earlier
version is: restore the **pre-upgrade etcd snapshot** (above) + reinstall the
prior K3s release on affected nodes. Prefer rolling *forward* to a fixed tag
over attempting a downgrade.

### Alert cross-reference

The `k3s-system-upgrade` PrometheusRule group
(`2-k3s/10.observability/alertmanager-config/custom-alerts.yaml`) emails on a
roll so an unsupervised/unexpected jump is caught:

- **`K3sUpgradeJobActive`** — fires while a `system-upgrade` Job is running.
- **`K3sNodeVersionSkew`** — fires when nodes are on mixed kubelet versions
  (roll in progress / stuck mid-roll).

If either fires without a corresponding merged PR, treat it as an unintended
roll and follow the rollback notes above.

---

## References

- [system-upgrade-controller GitHub](https://github.com/rancher/system-upgrade-controller)
- [K3s Automated Upgrades Documentation](https://docs.k3s.io/upgrades/automated)
- [K3s Release Channels](https://update.k3s.io/v1-release/channels)
- [K3s GitHub Releases](https://github.com/k3s-io/k3s/releases)