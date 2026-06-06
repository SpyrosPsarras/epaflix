# K3s Automated Node Upgrades

Automated rolling upgrades for all K3s masters and workers using the official [system-upgrade-controller](https://github.com/rancher/system-upgrade-controller) by Rancher.

> **GitOps-managed.** Both the controller (`controller/`) and the upgrade Plans (`plans/`) are reconciled by automated ArgoCD Applications — `system-upgrade-controller` and `system-upgrade-plans` (Issue #74) — under the app-of-apps. There is no manual `kubectl apply -f` step; merging to `main` applies them via selfHeal. The `kubectl` recipes below are for monitoring, manual pause/pin, and uninstall only.

---

## How It Works

The system-upgrade-controller applies an explicitly **pinned** K3s version (`spec.version`) declared on each Plan — the cluster currently pins `v1.35.5+k3s1`. It does **not** track the moving `stable` channel; every version bump is a reviewed git change (see the [Future-Upgrade SOP](#future-upgrade-sop) and issue #130). When the committed `spec.version` is bumped and merged:

1. **Masters are upgraded first**, one at a time (`concurrency: 1`), to preserve etcd quorum
2. **Workers wait** until every master has completed its upgrade (`prepare: k3s-server`)
3. **Workers are upgraded two at a time** (`concurrency: 2`), keeping at least 2 workers running for workloads

Before each node is upgraded, it is **cordoned** (no new pods scheduled) and **drained** (existing pods evicted gracefully). After the upgrade, the node is automatically uncordoned.

```
Upgrade order:

[Master] k3s-master-51  ──►  [Master] k3s-master-52  ──►  [Master] k3s-master-53
                                                                       │
                                                                       ▼
                                                             All masters done
                                                                       │
                                                   ┌───────────────────┘
                                                   ▼
                            [Worker] k3s-worker-61 + k3s-worker-62  (in parallel)
                                                   │
                                                   ▼
                            [Worker] k3s-worker-63 + k3s-worker-65  (in parallel)
```

---

## Prerequisites

- `kubectl` configured and pointing at the cluster
- All nodes `Ready` before starting (`kubectl get nodes`)

---

## Installation (GitOps-Managed)

This stack is **not** installed with `kubectl apply -f`. It is reconciled by two automated ArgoCD Applications under the app-of-apps:

| Application                  | Path                                         | What it manages                                  |
|------------------------------|----------------------------------------------|--------------------------------------------------|
| `system-upgrade-controller`  | `2-k3s/maintenance/system-upgrade/controller`| Vendored upstream CRD + controller Deployment + RBAC (v0.19.2) |
| `system-upgrade-plans`       | `2-k3s/maintenance/system-upgrade/plans`     | The `k3s-server` + `k3s-agent` upgrade Plans (pinned `spec.version`) |

Both Applications run `automated` with `selfHeal: true, prune: false` and `ServerSideApply=true`. Merging changes to `main` is the install/update mechanism — ArgoCD applies them automatically. The controller creates the `system-upgrade` namespace and its RBAC; the Plans App then populates the Plans.

Verify the controller is running:

```bash
kubectl -n system-upgrade rollout status deployment system-upgrade-controller
kubectl -n system-upgrade get pods
```

Verify the plans were accepted:

```bash
kubectl -n system-upgrade get plans
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
kubectl -n system-upgrade get jobs -w

# View all upgrade-related pods
kubectl -n system-upgrade get pods -o wide
```

### Check which nodes have been upgraded

```bash
# Shows current K3s version per node
kubectl get nodes -o wide

# Show only version column
kubectl get nodes -o custom-columns='NAME:.metadata.name,VERSION:.status.nodeInfo.kubeletVersion,STATUS:.status.conditions[-1].type'
```

### Stream logs from an active upgrade job

```bash
# Replace <job-name> with the job name shown in `kubectl -n system-upgrade get jobs`
kubectl -n system-upgrade logs -f job/<job-name>
```

### Check plan status

```bash
kubectl -n system-upgrade describe plan k3s-server
kubectl -n system-upgrade describe plan k3s-agent
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

A live `kubectl -n system-upgrade edit plan ...` is a stop-gap only — `selfHeal`
will revert it to the in-git version within minutes.

### Pause upgrades temporarily

> **Note:** the `system-upgrade-plans` App runs `selfHeal: true`, so a live
> `kubectl patch` will be reverted by ArgoCD within minutes. To pause durably,
> change the Plan in git (e.g. set `concurrency: 0` in `plans/`), or temporarily
> suspend the `system-upgrade-plans` Application in ArgoCD. The patch below is a
> stop-gap only.

```bash
# Suspend both plans to stop the controller from triggering new upgrades
kubectl -n system-upgrade patch plan k3s-server --type=merge -p '{"spec":{"concurrency":0}}'
kubectl -n system-upgrade patch plan k3s-agent  --type=merge -p '{"spec":{"concurrency":0}}'

# Resume — let ArgoCD selfHeal restore the in-git concurrency, or force a sync:
argocd app sync system-upgrade-plans
```

### Manually trigger an upgrade check now

```bash
# Restart the controller — it will re-evaluate both plans immediately
kubectl -n system-upgrade rollout restart deployment system-upgrade-controller
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
kubectl get nodes -o wide

# 2. Confirm all system pods are healthy
kubectl get pods -A | grep -v Running | grep -v Completed

# 3. Check that servarr workloads recovered
kubectl get pods -n servarr

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

---

## Troubleshooting

### Upgrade job stuck or not starting

```bash
# Describe the upgrade plan to see controller events
kubectl -n system-upgrade describe plan k3s-server

# Check for failed jobs
kubectl -n system-upgrade get jobs
kubectl -n system-upgrade describe job <job-name>

# Check controller logs for errors
kubectl -n system-upgrade logs deployment/system-upgrade-controller
```

### Node is stuck cordoned after a failed upgrade

```bash
# Manually uncordon the node
kubectl uncordon <node-name>

# Example:
kubectl uncordon k3s-master-51
```

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
kubectl -n system-upgrade get plan k3s-agent -o yaml | grep -A5 prepare
# Should show: args: [prepare, k3s-server]
# Canonical source: 2-k3s/maintenance/system-upgrade/plans/system-upgrade-plans.yaml
```

### Check etcd health during/after master upgrade

```bash
# On any master node
ssh ubuntu@192.168.10.51
sudo k3s etcd-snapshot ls  # Verify etcd is responsive

# From kubectl
kubectl get --raw='/healthz/etcd'
# Expected: ok
```

---

## Uninstalling

To stop auto-upgrades entirely (GitOps-managed — delete in git, not the cluster):

```bash
# 1. Remove the `system-upgrade-plans` App from
#    2-k3s/11.argocd/apps/kustomization.yaml (and delete its app file).
#    With prune: false, the live Plans persist, so also delete them:
kubectl -n system-upgrade delete plan k3s-server k3s-agent

# 2. (Optional) Remove the `system-upgrade-controller` App the same way, then:
kubectl -n system-upgrade delete deployment system-upgrade-controller
```

Because both Apps run `selfHeal: true`, deleting live objects without first
removing the App from git will be reverted by ArgoCD. Always remove from git
first.

---

## Future-Upgrade SOP

The K3s upgrade target is **explicitly pinned** in git (issue #130). The cluster
currently pins `v1.35.5+k3s1`. There is no unsupervised channel-driven roll —
every bump is a reviewed PR.

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
   workers (two at a time).

### At-a-glance status

```bash
# Canonical status check — Plans (LATEST/applied version) + any in-flight Jobs
kubectl -n system-upgrade get plans,jobs -o wide

# Per-node running K3s/kubelet version
kubectl get nodes -o wide
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
kubectl get cluster -A
kubectl cnpg status <cluster> -n <ns>
```

**NEVER force-manage or delete postgres pods during the roll** — let CNPG fail
over on its own. (Cross-ref issue #74.)

### Rollback notes

To **halt an in-flight unintended roll**:

1. **Stop the controller first** so it does not re-trigger:
   ```bash
   kubectl -n system-upgrade scale deployment system-upgrade-controller --replicas=0
   ```
2. Pin both Plans back to the **last-good version** in git (and/or set
   `concurrency: 0` on both Plans), and/or **suspend the `system-upgrade-plans`
   App** in ArgoCD so `selfHeal` does not fight you.

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