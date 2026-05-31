# K3s Automated Node Upgrades

Automated rolling upgrades for all K3s masters and workers using the official [system-upgrade-controller](https://github.com/rancher/system-upgrade-controller) by Rancher.

> **GitOps-managed.** Both the controller (`controller/`) and the upgrade Plans (`plans/`) are reconciled by automated ArgoCD Applications — `system-upgrade-controller` and `system-upgrade-plans` (Issue #74) — under the app-of-apps. There is no manual `kubectl apply -f` step; merging to `main` applies them via selfHeal. The `kubectl` recipes below are for monitoring, manual pause/pin, and uninstall only.

---

## How It Works

The system-upgrade-controller watches the K3s stable release channel. When a new K3s version is published:

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
| `system-upgrade-plans`       | `2-k3s/maintenance/system-upgrade/plans`     | The `k3s-server` + `k3s-agent` upgrade Plans (stable channel) |

Both Applications run `automated` with `selfHeal: true, prune: false` and `ServerSideApply=true`. Merging changes to `main` is the install/update mechanism — ArgoCD applies them automatically. The controller creates the `system-upgrade` namespace and its RBAC; the Plans App then populates the Plans.

Verify the controller is running:

```bash
kubectl -n system-upgrade rollout status deployment system-upgrade-controller
kubectl -n system-upgrade get pods
```

Verify the plans were accepted:

```bash
kubectl -n system-upgrade get plans
# Expected output:
# NAME          IMAGE                    CHANNEL                                               LATEST
# k3s-agent     rancher/k3s-upgrade      https://update.k3s.io/v1-release/channels/stable      v1.xx.x+k3s1
# k3s-server    rancher/k3s-upgrade      https://update.k3s.io/v1-release/channels/stable      v1.xx.x+k3s1
```

Once the Plans are applied, the controller checks the channel immediately and then on an ongoing basis. If the cluster is already on the latest stable version, no upgrade jobs will run.

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

### Force an upgrade to a specific version (instead of stable channel)

Edit the plan to pin a version instead of using the channel:

```bash
kubectl -n system-upgrade edit plan k3s-server
```

Replace the `channel:` field with a `version:` field:

```yaml
# Remove this:
channel: https://update.k3s.io/v1-release/channels/stable

# Add this (use exact K3s release tag):
version: v1.32.1+k3s1
```

Do the same for `k3s-agent`. Find available versions at: https://github.com/k3s-io/k3s/releases

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

### Check the current stable K3s channel version

```bash
curl -sL https://update.k3s.io/v1-release/channels/stable | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('latest','not found'))"
```

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

## References

- [system-upgrade-controller GitHub](https://github.com/rancher/system-upgrade-controller)
- [K3s Automated Upgrades Documentation](https://docs.k3s.io/upgrades/automated)
- [K3s Release Channels](https://update.k3s.io/v1-release/channels)
- [K3s GitHub Releases](https://github.com/k3s-io/k3s/releases)