# TrueNAS host observability and ntfy's home - design

> Date: 2026-08-09 (dated for the decision set it records; ntfy component written 2026-08-22)
> Status: **complete** (2026-08-23). All sections are written. Component 2 records the
> #916/#917/#918/#919 decisions (decided 2026-08-21, executed 2026-08-22 via PRs #1110
> and #1111); `## Observability` records the accepted single-delivery-path posture and
> the guards tracked against it.
> Repo: `SpyrosPsarras/epaflix` (this repo)
> Decisions recorded: #904 (entry point), #914 (owning namespace and ArgoCD Application),
> #915 (message cache on a PVC). All three carry an owner decision comment dated 2026-08-21
> and each of them requires "a line in the spec", which is why this file exists now.
> Related: #922 (TrueNAS host observability) is not written here. #920 (PVE reroute),
> #921 (delete the email receiver) and #1076 (PVE's notification target returned 400)
> landed 2026-08-22 and are recorded in `## Component 3` below.

## Purpose

Say where the platform's notification receiver lives, how it is reached, and what
survives a restart - and, separately, how the TrueNAS host gets into the same
observability stack as everything else.

The ntfy half and the PVE notification split are settled and written. TrueNAS host
observability is #922's.

## What triggered this

Two silences. The RTX 2070 SUPER was wedged for about 23 hours on 2026-08-09 and nothing
told anyone; the owner found out from a chat UI. Backup failures went unnoticed for
twelve days. SMTP has been dead since #461, so the only alert leg that actually delivers
is the Alertmanager -> ntfy webhook - and that receiver was a convenience sidecar in a
consumer's namespace, on a plaintext LoadBalancer, with an `emptyDir` cache.

The owner's 2026-08-21 decision on #904 settles the frame: **ntfy becomes the
load-bearing receiver** and email drops to secondary or goes entirely. Sequencing from
the same comment: entry point and TLS, then move the namespace, then reroute PVE, then
remove the email leg.

## Architecture

```
Alertmanager (observability) ──┐
                               │  http://ntfy.observability.svc.cluster.local:8091
Odysseus (odysseus)  ──────────┤  topic k8s-alertmanager / app notices
                               ▼
Proxmox VE hosts ───────────► ntfy (observability) ──► subscribers
  https://ntfy.epaflix.com       │  cache: ntfy-cache PVC, 12h retention
  topic pve-backups              │
                                 └─ https://ntfy.epaflix.com
TrueNAS GPU cron ──────────────►    Traefik `internal` entry point, 192.168.10.102
  https://ntfy.epaflix.com
  topic truenas-alerts
```

Three publishers, three topics. Two of them repeat on their own - Alertmanager every
`12h`, the TrueNAS GPU cron every 15 minutes - so they self-heal. Proxmox VE is
event-driven with no repeat, and its only other leg (`mail-to-root`) does not deliver.
That asymmetry is what the cache decision turns on.

## Component 1 - ntfy

**Owning namespace and ArgoCD Application: `observability`** (#914). ntfy used to be a
`pod/ntfy` plus a LoadBalancer Service in `odysseus`, which was right while it was a
convenience app for Odysseus. As alerting infrastructure it belongs with the stack that
produces the alerts, so an Odysseus sync, prune or rollback can no longer take out
alerting for everything. Manifests are `2-k3s/10.observability/ntfy.yaml`,
`ntfy-cache-pvc.yaml` and `ingress/ntfy-ingressroute.yaml`; each states
`namespace: observability` explicitly because that kustomization has no namespace
transformer, by design. Odysseus is now a cross-namespace consumer and its
`NTFY_BASE_URL` carries the full FQDN. The old resources are pruned, not deleted by
hand: the `odysseus` Application carries `prune: true` + `selfHeal: true`, so its next
sync removes the tracking-ids `odysseus:apps/Deployment:odysseus/ntfy` and
`odysseus:/Service:odysseus/ntfy`, and the post-sync check is that both are `NotFound`
in `odysseus` while the observability copies carry
`observability:apps/Deployment:observability/ntfy` (deploy gate:
`2-k3s/10.observability/README.md`).

**Entry point: Traefik's `internal` entry point at 192.168.10.102**, hostname
`ntfy.epaflix.com`, TLS from the `cloudflare` certResolver on the `*.epaflix.com`
wildcard (#904). Not the public `websecure` entry point on 192.168.10.101: that is the
address the router forwards 443 to and the one the Cloudflare-proxied wildcard answers
for, and a route there would publish an unauthenticated publish/subscribe endpoint to
the internet. `traefik-internal` on .102 has no router port forward, and the name gets a
LAN-only dnsmasq A record plus an Unbound `local-zone` AAAA guard, same as `searxng`,
`qbittorrent`, `remote-pi` and `cliproxy`. The IngressRoute carries **no Authentik
forward-auth middleware** on purpose: the publishers are machines posting with no
interactive login.

**The LoadBalancer at 192.168.10.112 is retired by this change** (#904 / #920), with the
retirement applied at phase 4 of the deploy gate in `2-k3s/10.observability/README.md`,
not at merge. Proxmox VE posted to the hardcoded `http://192.168.10.112:8091/pve-backups`
(#597, `1-proxmox/pbs/notifications.cfg`) and the TrueNAS GPU cron did the same for
`truenas-alerts`, so the Service outlives the entry point by exactly the gate phases that
repoint those two publishers: retiring it first would have left PVE with no path at all
and the symptom would have been silence. Once phase 4 runs, both publish to
`https://ntfy.epaflix.com` and the Service is `ClusterIP`. The owner's phrase on #904 was
that the LoadBalancer "stops being the interface"; do not treat .112 as free to reassign
until the gate evidence (connection-refused probe, `ip addr` on the former vipHost) is
pasted on #920.

Because the Service moves namespace while keeping the same explicit `loadBalancerIP`,
the cutover has an order: **sync `observability` first, then `odysseus`**. kube-vip's
`deleteService` keeps the address plumbed when another Service instance still holds it,
and removes it when none does, so pruning first would take .112 down until the other
sync caught up. The evidence and the post-cutover check that .112 answers again are in
the deploy-gate section of `2-k3s/10.observability/README.md`.

**Cache: a PVC, not an `emptyDir`** (#915, owner: "Move to a PVC, as #903 proposed").
`ntfy-cache`, `local-path`, `ReadWriteOnce`, 1Gi. The cache only ever matters to a
subscriber that was offline when a message was published, and PVE is the one publisher
with a single chance, so a `pve-backups` notice has to survive a restart. Two
consequences carried deliberately:

- local-path is RWO with `WaitForFirstConsumer`, so the claim **pins ntfy to one node**
  and forces `strategy: Recreate` on the Deployment - a RollingUpdate would deadlock the
  new pod on the old pod's volume. The pin is an **accepted single point of failure**
  (owner ruling 2026-08-23, #1089); the lost-node operator step is documented in
  `2-k3s/10.observability/README.md` under the ntfy table.
- Retention is ntfy's built-in **12h** default, and that is now **deliberate** (owner
  ruling 2026-08-23, #1091): `NTFY_CACHE_DURATION` stays unset on purpose. The PVC is
  restart persistence for the one-shot `pve-backups` publisher, not multi-day
  subscriber catch-up.

ntfy is therefore no longer disposable: capacity, storage class and volume lifecycle are
part of the alert path.

## Component 2 - TrueNAS host observability

Written 2026-08-23 from the decisions on #916, #917, #918 and #919 (all decided
2026-08-21, executed 2026-08-22 through PRs #1110/#1111 and the #919 deploy gate in
`2-k3s/10.observability/README.md`).

**Collection: two exporters on the TrueNAS box, both installed as TrueNAS custom apps.**
`nvidia_gpu_exporter` (utkuozdemir) watches the RTX 2070 SUPER; `node_exporter` supplies
host memory and ZFS ARC series. node_exporter was a first landing - nothing listened on
9100 before (#918's measurement). Both apps must be re-verified after a SCALE update
(#1115).

**Scrape: the selector-less Service plus EndpointSlice pattern, no second convention**
(#917). Two Kubernetes objects per target, and 192.168.10.200 stays hand-maintained
inside a manifest rather than discovered - the precedent (`servarr/jellyfin-truenas`,
`traefik-system/truenas-ui`) was already decisive. One execution caveat is load-bearing
(PR #1111): Prometheus's default `Endpoints` discovery role cannot see hand-authored
EndpointSlices, so `serviceDiscoveryRole: EndpointSlice` in `prometheus-values.yaml` is
what makes the whole pattern visible. Removing it silently blanks every off-cluster
target.

**Alert expressions: this repo owns them** (#916, owner: "Repo owns, both conditions,
delete cron"). The four upstream expressions are vendored into
`alertmanager-config/custom-alerts.yaml`. Coverage is both Xid events and collection
health, and **collection health is the condition that must never be silenced**: the
2026-08-09 incident produced no Xid to catch - the driver stayed loaded, `/dev/nvidia*`
stayed present, `nvidia-smi` exited 6. The recorded cost: this is a fork, upstream
PromQL fixes no longer arrive automatically, and the drift is ours to notice (#1114).

**Leading memory indicator: ARC measured against its own target/cap** (#918) -
`node_zfs_arc_size` vs `node_zfs_arc_c_max`, plus forced shrink under pressure. Not a
free-memory floor: a healthy ZFS host fills RAM with ARC by design, so a floor alert
fires constantly and gets silenced. Threshold tuning waits on the #1113 baseline
capture.

**The untracked cron is gone** (#919). `/root/gpu-health-check.sh`'s cron was deleted
2026-08-22 only after the full gate ran: rules vendored, exporters live, a real alert
observed fire-and-resolve end to end on the phone. The script is retired in place on the
host, the tracked copy lives at `0-truenas/scripts/gpu-health-check.sh`, and the
`truenas-alerts` topic retired with the cron - GPU alerts now arrive on
`k8s-alertmanager` like everything else.

## Component 3 - what PVE notifies natively, and what alerts from Prometheus (#920)

The split is decided by one measurement, not by preference: `pve-exporter` exposes **zero
metrics matching `^pve_(backup|vzdump|task)`** (re-verified with a negative control — the
same query shape against `^pve_up` returns 32 series). Backup and task outcomes cannot be
expressed as a Prometheus alert at all, so they are exactly the set that has to stay on
PVE's native targets.

- **Native, on PVE.** Backup and task outcomes. `matcher: ntfy-failures`
  (`match-severity warning,error`, `mode all`) routes them to `webhook: ntfy-pve`, which
  posts to `https://ntfy.epaflix.com/pve-backups`. That target depends on a server-side
  ntfy setting: `NTFY_MESSAGE_SIZE_LIMIT=32k`, because the 4096-byte default rejected
  every real 16311-byte failure body with `400` / `40014` (#1076).
- **Alertmanager, from metrics.** Everything metric-backed: `pve_up` (32 series), storage
  and guest state. Those get grouping, silencing and `repeat_interval`, which the native
  path has none of.
- **PVE's mail leg is off.** `matcher: default-matcher` carries `disable 1`. It targeted
  `sendmail: mail-to-root`, which reported success and delivered nothing (#461, #720), so
  leaving it enabled was worse than having no second leg: it made a failed notification
  look notified.
- **Alertmanager's receiver is `ntfy` and carries `webhook_configs` only.**
  `email_configs` and every `global.smtp_*` key were deleted with #921. The
  `severity: critical` child route keeps `continue: true`, so **no sibling route may be
  added beside it** without accepting double delivery of every critical alert — which is
  why the PVE half of this split is not written as an Alertmanager route.

## Observability

Written 2026-08-23. The question this section owed - what watches the watcher - now has
an answered half and a tracked half.

Answered: Alertmanager -> ntfy on `k8s-alertmanager` is the **single delivery path**, by
decision (#920/#921), and it runs inside the very cluster whose problems it reports.
`TargetDown` and `NvidiaGpuExporterCollectionFailing` were proven live on 2026-08-22,
FIRING and RESOLVED, delivered to the topic and the phone. ntfy's two structural
weaknesses are accepted and written down rather than fixed: the node pin (#1089 ruling)
and the 12h cache ceiling (#1091 ruling).

Tracked, not yet guarded: silence still cannot be distinguished from delivery failure
without the guards in #1108 (a PVE notification-target failure must be noticed within a
day; absorbed #1116's Alertmanager->ntfy leg, where
`alertmanager_notifications_failed_total` per integration is the candidate signal) and
#1100 (one demonstrated end-to-end `KubeJobFailedLastRun` failure). Until those close,
the honest statement is: a broken delivery leg looks identical to a quiet day, for at
most one day's worth of alerts.
