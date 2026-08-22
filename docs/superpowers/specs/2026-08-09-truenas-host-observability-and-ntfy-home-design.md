# TrueNAS host observability and ntfy's home - design

> Date: 2026-08-09 (dated for the decision set it records; ntfy component written 2026-08-22)
> Status: **partial**. `## Component 1 - ntfy` and `## Component 3 - what PVE notifies
> natively` are written. The TrueNAS host observability half is #922's to author and is
> deliberately left as headings below.
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

**The LoadBalancer at 192.168.10.112 is retired** (#904 / #920, 2026-08-22). Proxmox VE
posted to the hardcoded `http://192.168.10.112:8091/pve-backups` (#597,
`1-proxmox/pbs/notifications.cfg`) and the TrueNAS GPU cron did the same for
`truenas-alerts`, so the Service outlived the entry point by one PR: retiring it before
those two publishers moved would have left PVE with no path at all and the symptom would
have been silence. Both now publish to `https://ntfy.epaflix.com` and the Service is
`ClusterIP`. The owner's phrase on #904 was that the LoadBalancer "stops being the
interface"; it has now also stopped existing.

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
  new pod on the old pod's volume.
- Retention is ntfy's built-in **12h** default, because `NTFY_CACHE_DURATION` is set
  nowhere. The volume can never hold more than 12h of backlog. Longer catch-up would be
  a retention decision, explicitly out of scope of #915.

ntfy is therefore no longer disposable: capacity, storage class and volume lifecycle are
part of the alert path.

## Component 2 - TrueNAS host observability

**Not written here. #922 owns this.** The GPU health signal, `node_exporter` on
192.168.10.200, and the ARC/memory metrics belong in this section and none of them are
decided by #904, #914 or #915. Do not read this file as a finished spec.

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

**Not written here. #922 owns this**, together with Component 2 - what alerts on ntfy
itself being down, and what alerts on the TrueNAS host, are the same question and it has
not been answered. Today nothing watches the watcher: if the ntfy pod is unschedulable
because its RWO volume's node is drained, the failure mode is silence.
