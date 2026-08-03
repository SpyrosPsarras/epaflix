# Process: observability-controlplane-metrics-121

Resolve Epaflix issue **#121** — kube-prometheus-stack control-plane exporters
(`kube-controller-manager`, `kube-scheduler`, `kube-etcd`) have empty Endpoints, so Prometheus
scrapes zero targets for those jobs. Only `coredns` is up.

## Root cause (two parts, both must be fixed)
1. **k3s binds those metrics to 127.0.0.1** — the server args lack
   `--kube-controller-manager-arg=bind-address=0.0.0.0`,
   `--kube-scheduler-arg=bind-address=0.0.0.0`, `--etcd-expose-metrics=true`.
2. **The chart has no `endpoints:`** for those three components, so the manually-managed
   Endpoints stay empty.

## Approach
- **LIVE (deploy, control-plane restart, etcd-quorum risk):** add the args via the durable k3s
  drop-in `/etc/rancher/k3s/config.yaml` and `systemctl restart k3s`, **one master at a time**
  (51 → 52 → 53), health-gating node-Ready + etcd quorum between each. Hard-stop + restore from
  `.bak` on any failure.
- **GIT (GitOps):** populate `kubeControllerManager.endpoints` (port 10257 https),
  `kubeScheduler.endpoints` (10259 https), `kubeEtcd.endpoints` (2381 http) with master node
  InternalIPs in `2-k3s/10.observability/prometheus-values.yaml`; add the three args to the
  k3sup master commands in `.github/instructions/k3s.instructions.md` so rebuilds stay correct.

**Order:** apply LIVE first (ports answer), then merge GIT (Prometheus only scrapes listening ports).

## Phases
1. **analyze** (read-only) — prove empty endpoints, confirm loopback bind, derive reachable
   master IPs + ports/scheme, recommend live method.
2. **prepare-git** — author both edits, branch + local commit (no push). Refines on gate feedback.
3. **GATE** (deploy + destructive-git) — ONE owner approval covering live rollout **and** merge.
4. **live-rollout** — masters one-at-a-time, health-gated; recovery gate on partial failure.
5. **publish-merge** — push + PR + rebase-merge per Epaflix policy.
6. **post-verify** — Endpoints populated, `up{job=cm|scheduler|etcd}=1`, observability Synced/Healthy;
   recovery gate on failure.
7. **closeout** — close #121, tick PR test plan (edit body), open follow-ups (e.g. #44 durability).

## Breakpoints (low tolerance / expert; alwaysBreakOn deploy + destructive-git)
- One mandatory combined gate (live + merge). Conditional recovery gates only on failure.

## Refs
Surfaced by #53 (prune-enable). Related #25 (pve-exporter), #13 (observability follow-ups),
#44 (system-upgrade-controller — config.yaml durability follow-up).
