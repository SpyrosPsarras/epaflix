# Comprehensive Observability Stack

Complete monitoring, logging, and service mesh observability for the K3s cluster with Proxmox host monitoring.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Data Flow                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  Proxmox Hosts ──► pve-exporter ──────┐                         │
│  K8s Nodes ─────► node-exporter ──────┤                         │
│  Pods ──────────► kube-state-metrics ─┤                         │
│  Cilium ────────► cilium-agent ───────┼──► Prometheus ──┐       │
│  Hubble ────────► hubble-relay ───────┘                 │       │
│  Istio ─────────► envoy sidecars ─────────────────────  │       │
│                                                          │       │
│  Pod Logs ───► Promtail ───► Loki ─────────────────────┼──┐    │
│                                                          │  │    │
│                                            ┌─────────────┘  │    │
│                                            ▼                ▼    │
│                                         Grafana ◄───── AlertMgr │
│                                            │                     │
│                                            │                     │
│  Service Mesh Visualization:               │                     │
│  Istio Services ──► Kiali ─────────────────┘                    │
│  Cilium Flows ──► Hubble UI                                     │
└─────────────────────────────────────────────────────────────────┘
```

## Stack Components

### Currently Deployed ✅
- **Prometheus** (1 replica): Time-series metrics database, 8d retention (the 20GB size cap binds before any longer time window — see "Prometheus Storage")
- **Grafana** (2 replicas): Unified dashboards with folder organization, OAuth via Authentik
- **AlertManager** (3 replicas): alerts delivered to ntfy on topic `k8s-alertmanager` (the SMTP leg was deleted, #921)
- **node-exporter** (DaemonSet): Node-level CPU, memory, disk, network metrics
- **kube-state-metrics** (2 replicas): Kubernetes cluster state metrics
- **pve-exporter** (1 replica): Proxmox VE host and VM metrics from 192.168.10.10 and .11
- **ntfy** (1 replica): the load-bearing notification receiver for the whole platform. Moved in from the `odysseus` namespace under the #914 decision. See "ntfy" under Alerting

### Optional Components (Not Installed)
- **Cilium CNI**: eBPF-based networking (currently using Flannel)
- **Hubble**: L3-L7 network observability (requires Cilium)
- **Istio**: Service mesh with mTLS, traffic management, telemetry (not currently deployed)
- **Kiali**: Service mesh topology visualization (not currently deployed)
- **Loki**: Log aggregation with 31d retention
- **Promtail**: Log collection from pods

## Access URLs

| Component | URL | Credentials | Status |
|-----------|-----|-------------|--------|
| **Grafana** | https://grafana.epaflix.com | Authentik SSO (or admin / <POSTGRES_PASSWORD>) | ✅ Running |
| **Prometheus** | Port-forward only | N/A | ✅ Running |
| **AlertManager** | Port-forward only | N/A | ✅ Running |
| **ntfy** | https://ntfy.epaflix.com (Traefik `internal`, 192.168.10.102) | None (un-gated on the LAN, by design) | ✅ Running. Sole entry point since the 192.168.10.112 LoadBalancer was retired (#920) |

## Installation
   # - k3s-master-51: 10GB (192.168.10.51, Proxmox takaros)
   # - k3s-master-52: 10GB (192.168.10.52, Proxmox takaros)
   # - k3s-master-53: 10GB (192.168.10.53, Proxmox evanthoulaki)
   # Workers: 22GB RAM each (workload pods)
   # - k3s-worker-61: 22GB (192.168.10.61, Proxmox takaros)
   # - k3s-worker-62: 22GB (192.168.10.62, Proxmox takaros)
   # - k3s-worker-63: 22GB (192.168.10.63, Proxmox evanthoulaki)
   # - k3s-worker-65: 22GB (192.168.10.65, Proxmox evanthoulaki)
   # Total Cluster RAM: 118GB (30GB control-plane + 88GB workload capacity)
   ```

2. **Proxmox API Tokens** (CONFIGURED ✅):
   - Token created: `root@pam!grafana`
   - Configured for both hosts: 192.168.10.10 and 192.168.10.11
   - pve-exporter deployed and running
   ```bash
   # Backup etcd
   kubectl --context epaflix exec -n kube-system $(kubectl --context epaflix get pod -n kube-system -l component=etcd -o jsonpath='{.items[0].metadata.name}') -- \
     etcdctl --cacert=/var/lib/rancher/k3s/server/tls/etcd/server-ca.crt \
     --cert=/var/lib/rancher/k3s/server/tls/etcd/server-client.crt \
     --key=/var/lib/rancher/k3s/server/tls/etcd/server-client.key \
     snapshot save /tmp/etcd-backup.db

   # Install Cilium CLI
   CILIUM_CLI_VERSION=$(curl -s https://raw.githubusercontent.com/cilium/cilium-cli/main/stable.txt)
   curl -L --remote-name-all https://github.com/cilium/cilium-cli/releases/download/${CILIUM_CLI_VERSION}/cilium-linux-amd64.tar.gz
   tar xzvfC cilium-linux-amd64.tar.gz .
   sudo mv cilium /usr/local/bin/

   # Install Cilium with Hubble
   cilium install \
     --set ipam.operator.clusterPoolIPv4PodCIDR=10.42.0.0/16 \
     --set k8sServiceHost=192.168.10.100 \
     --set k8sServicePort=6443 \
     --set kubeProxyReplacement=false \
     --set hubble.relay.enabled=true \
     --set hubble.ui.enabled=true \
     --set hubble.metrics.enabled="{dns,drop,tcp,flow,icmp,http}" \
     --set prometheus.enabled=true \
     --set operator.prometheus.enabled=true

   # Restart all pods
   kubectl --context epaflix get pods -A -o custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name --no-headers | \
     while read ns pod; do kubectl --context epaflix delete pod -n $ns $pod --wait=false; done

   # Verify
   cilium status --wait
   cilium connectivity test

   # Remove Flannel
   kubectl --context epaflix -n kube-system delete ds kube-flannel-ds || true

   # Test for 48 hours, then enable kube-proxy replacement:
   cilium upgrade --set kubeProxyReplacement=true --set bpf.masquerade=true
   # Monitor for 24 hours post-upgrade
   ```

   </details>

4. **Authentik OAuth Provider** (CONFIGURED ✅):
   - Login to https://auth.epaflix.com
   - Create OAuth2/OIDC Provider
   - Name: Grafana Monitor
   - Redirect URI: https://graf (CONFIGURED ✅):
   - Grafana OAuth configured with Authentik
   - Application: "Grafana Monitor"
   - URL: https://grafana.epaflix.com
   - Group-based role assignment: "Grafana Admins" → Admin, "Grafana Editors" → Editor, default → Viewer
cd /workspaces/01-manual\ installation/manifests/10.observability/

# Make deploy script executable
chmod +x deploy.sh

# Run deployment
./deploy.sh
```

The script will:
1. Create namespace and PostgreSQL database for Grafana
2. Create PersistentVolumeClaims (100Gi for Prometheus, 200Gi for Loki)
3. Install kube-prometheus-stack (Prometheus + Grafana)
4. Install Loki and Promtail
5. Deploy Proxmox VE Exporter
6. Apply custom alert rules
7. Create ingress routes

### Istio / Cilium / Kiali

Not currently deployed. The cluster uses Flannel CNI. Istio/Cilium can be added later for service mesh capabilities.

## Grafana Configuration

### Dashboard Folders

Dashboards are organized into folders:
- **Kubernetes Cluster**: Cluster overview, node metrics, kube-state-metrics
- **Network & Service Mesh**: Cilium/Hubble metrics, Istio dashboards
- **Infrastructure**: Proxmox VE monitoring
- **Logs**: Loki logs browser and stack monitoring

### Default Home Dashboard

The default home dashboard is "Kubernetes Cluster Overview" (Grafana ID 315).

### Pinned Favorites

Pre-configured favorite dashboards:
- Kubernetes Cluster Overview
- Node Exporter Full
- Loki Logs Browser
- Cilium Hubble Overview

### Importing Additional Dashboards

```bash
# From Grafana UI
# 1. Click "+" → "Import"
# 2. Enter dashboard ID:
#    - 315: Kubernetes Cluster Overview
#    - 1860: Node Exporter Full
#    - 16611: Cilium Hubble Overview
#    - 7645: Istio Control Plane
#    - 7636: Istio Service Dashboard
# 3. Select folder
# 4. Select Prometheus datasource
# 5. Click "Import"
**Current Dashboard Status:**
- ✅ **315** (Kubernetes Cluster Overview) - Working, some panels may be empty
- ✅ **1860** (Node Exporter Full) - Fully working
- ⏳ **10347** (Proxmox VE Cluster) - Working (wait 2-3 min for initial scrape)
- ❌ **16611** (Cilium Hubble) - Requires Cilium CNI installation
- ❌ **7645** (Istio Control Plane) - Requires Istio installation
- ❌ **7636** (Istio Service) - Requires Istio installation

To import dashboards in Grafana:
1. Navigate to https://grafana.epaflix.com/dashboard/import
2. Enter dashboard ID and click "Load"
3. Select folder and Prometheus datasource
4. Click "Import"
# Port forward for CLI access
cilium hubble port-forward &
```

### Common Commands

```bash
# Observe all flows
hubble observe --follow

# Observe specific namespace
hubble observe --namespace app-authentik --follow

# Filter by protocol
hubble observe --protocol http --follow
hubble observe --protocol dns --follow

# See dropped packets
hubble observe --verdict DROPPED --follow

# Filter by pod
hubble observe --pod authentik-server --follow

# View L7 HTTP requests
hubble observe --namespace app-authentik --protocol http -o json | jq '.l7.http'

# See DNS queries
hubble observe --protocol dns -o compact

# Network policy denials
hubble observe --verdict DENIED --follow
```

## Performance Metrics

After Cilium kube-proxy replacement, expected improvements:
- **Latency**: 40-50% reduction in p50/p95/p99
- **Throughput**: 10-20% increase
- **CPU overhead**: 20-30% reduction vs iptables

See [PERFORMANCE-METRICS.md](PERFORMANCE-METRICS.md) for detailed before/after comparison.

## Alerting

### ntfy

ntfy is **the only leg**. SMTP was dead from #461 on, so the Alertmanager
`webhook_configs` leg on topic `k8s-alertmanager` (#568/PR #574) was already the
thing that reached a human; the owner's 2026-08-21 decision on #904/#914/#915
made it official and #921 deleted the email leg on 2026-08-22.

| Aspect | Value |
|---|---|
| Manifests | `ntfy.yaml` (Deployment + ClusterIP Service), `ntfy-cache-pvc.yaml`, `ingress/ntfy-ingressroute.yaml` |
| Namespace / owning app | `observability` / ArgoCD Application `observability` (moved from `odysseus`, #914) |
| Entry point | `https://ntfy.epaflix.com` on Traefik's `internal` entry point at **192.168.10.102**, TLS from the `cloudflare` certResolver (#904). NOT the public `websecure` entry point on .101 — see the header of `ingress/ntfy-ingressroute.yaml` |
| Auth | none. Publishers are machines (PVE, the TrueNAS GPU cron) posting with no interactive login, so a forward-auth middleware would break them. Same posture as `searxng-internal` and `qbittorrent-internal` |
| Cache | `ntfy-cache` PVC, local-path, RWO, 1Gi (#915). Was an `emptyDir`. The Deployment is `strategy: Recreate` because local-path is RWO + WaitForFirstConsumer |
| Node pin | **Accepted single point of failure** (owner ruling 2026-08-23, #1089): local-path RWO pins ntfy to the node the volume was provisioned on (k3s-worker-65). If that node is lost: `kubectl --context epaflix -n observability delete pvc ntfy-cache` and delete the pod; ntfy reschedules with an empty cache and the backlog loss is accepted |
| Retention | ntfy's built-in **12h** default, **deliberate** (owner ruling 2026-08-23, #1091): `NTFY_CACHE_DURATION` stays unset on purpose. The PVC exists for restart persistence, not multi-day subscriber catch-up |
| In-cluster address | `http://ntfy.observability.svc.cluster.local:8091` (Alertmanager's webhook url, and `NTFY_BASE_URL` in `odysseus-config`, which is Odysseus's dial address) |
| ntfy's own `base-url` | `https://ntfy.epaflix.com`, set via the `NTFY_BASE_URL` env in `ntfy.yaml`. That is a *server-side* setting — the public-facing URL ntfy writes into attachment/click links and push payloads — not an address anything dials, so it follows the entry point and not the namespace |
| Topics | `k8s-alertmanager` (Alertmanager), `pve-backups` (Proxmox VE), `truenas-alerts` (the TrueNAS GPU cron, retired at the #919 gate, see "TrueNAS GPU + ARC monitoring" below, after which GPU alerts arrive on `k8s-alertmanager` like everything else) |
| Message size limit | **32k**, via `NTFY_MESSAGE_SIZE_LIMIT` on the Deployment. The default is 4096 bytes, and ntfy treats anything larger as an attachment upload; attachments are off, so the POST comes back `400` with body code `40014` "attachments not allowed". That is the whole of #1076: a real failed-vzdump body is 16311 bytes, so every genuine backup failure from 2026-08-10 on was rejected while every clean night looked healthy. Residual: a body over 32k 400s the same silent way, which the notification-target failure guard follow-up covers |

**The LoadBalancer at 192.168.10.112 is retired** (#904 / #920, deploy gate of
this PR). It was the plaintext front door for two LAN publishers, the Proxmox
VE hosts on topic `pve-backups` (#597, `1-proxmox/pbs/notifications.cfg`) and
the TrueNAS GPU cron on `truenas-alerts`. Both now publish to
`https://ntfy.epaflix.com`. Retirement is phase 4 of the gate sequence below,
deliberately after the PVE leg is proven with a real error-severity event:
retiring the address while PVE still dialled it would leave PVE with no path,
and the symptom of that is silence (#1076).

Evidence to paste when the phase runs, per #50/#551:

```bash
kubectl --context epaflix -n observability get svc ntfy -o jsonpath='{.spec.type}{"\n"}'
# want the literal: ClusterIP
curl -m5 -s -o /dev/null -w '%{http_code}\n' http://192.168.10.112:8091/v1/health
# want: connection failure, not 200 (the same probe returned 200 before the sync)
curl -s -o /dev/null -w '%{http_code}\n' https://ntfy.epaflix.com/v1/health   # want: 200, same minute
ssh ubuntu@192.168.10.53 'ip -4 -o addr show eth0' | grep 192.168.10.112       # want: no hit
```

#### Two DNS changes happened on the Pi-hole box, not through ArgoCD

`ntfy.epaflix.com` needs a LAN-only record, and no sync applies it. Both changes
were hand-applied on the Pi-hole LXC (192.168.10.30) at PR #1088's deploy gate;
the LAN resolver now answers `192.168.10.102` for the name, which is the
precondition every publisher in this PR depends on. `10-epaflix.conf` has no
representation in this repo at all — exactly the live-only-fix trap `CLAUDE.md`
warns about — so it is recorded here and in
`.github/instructions/pihole.instructions.md`. The Unbound file does have a
tracked copy. Both steps are kept below because they are the rebuild recipe:

1. `/etc/dnsmasq.d/10-epaflix.conf` on 192.168.10.30 gets
   `address=/ntfy.epaflix.com/192.168.10.102` (back the file up first — the box
   already carries dated `.bak` copies, which is the local convention), then
   restart `pihole-FTL`. Without it the name resolves to the Cloudflare
   wildcard and the IngressRoute is unreachable.
2. `/etc/unbound/unbound.conf.d/no-aaaa-leak.conf` gets
   `local-zone: "ntfy.epaflix.com." static` next to the existing
   searxng / qbittorrent / remote-pi entries, then `unbound-checkconf` and
   `unbound-control reload`. dnsmasq answers the A record authoritatively while
   Unbound suppresses the AAAA, so an IPv6 LAN client is not sent to the gated
   public route on .101. The git source of truth for that file is
   `1-proxmox/pihole/unbound-no-aaaa-leak.conf` and it is updated in the same
   PR; `1-proxmox/pihole/aaaa-tripwire.sh` is the mechanical check.

`unbound-checkconf` before the reload is not optional: a malformed file takes
LAN DNS down entirely.

#### Cutover: sync order, the prune, and the 192.168.10.112 handover (#914, done)

One cost paid here, recorded so "where did that message go" has an answer (#1090):
moving a pod off an `emptyDir` discards whatever the emptyDir held. The pre-move
cache in `odysseus` held a live 61KB `cache.db` and no copy was taken before the
prune, by choice - anything buffered for an offline subscriber at cutover time is
gone. The alternatives were copy-first or accept the loss; the loss was accepted.

Completed history, kept because it is the only written record of how kube-vip
behaves when two Services claim one address. This ran at PR #1088's deploy gate;
.112 itself is gone as of #920, so nothing here is a live instruction any more.

The move crossed two ArgoCD Applications, so for a moment two Services in two
namespaces asked for the same `loadBalancerIP` — and .112 was the only path PVE
had, since its native target was broken (#1076). The two syncs were ordered by
hand at the deploy gate rather than letting the 120s reconciliation timer
(`argocd-cm` `timeout.reconciliation=120s`) pick an order:

```bash
argocd app sync observability   # FIRST: creates observability/ntfy, which claims .112
argocd app sync odysseus        # THEN: prunes odysseus/ntfy
```

Why that order and not the reverse — read out of the pinned sources and the live
cluster on 2026-08-22, not assumed:

- **Allocation does not deduplicate.** kube-vip-cloud-provider v0.0.12
  (`2-k3s/03.kube-vip-cloud-provider/cloud-controller.yaml`) short-circuits any
  Service that already carries `spec.loadBalancerIP`:
  `checkLegacyLoadBalancerIPAnnotation` in `pkg/provider/loadBalancer.go` copies
  the value into the `kube-vip.io/loadbalancerIPs` annotation and returns. The
  pool in `ip-pool-configmap.yaml` and its in-use set (`mapImplementedServices`)
  are only read on the auto-allocation path, which an explicit IP never reaches.
  So `range-global` being namespace-agnostic is beside the point: both Services
  are simply granted .112, with no error and no Event.
- **Both claims land on one node, not two.** The kube-vip DaemonSet (v0.8.7,
  `2-k3s/01.kube-vip/kube-vip-daemonset.yaml`) sets `cp_enable=true` +
  `vip_leaderelection=true` and does NOT set `svc_election`, so one leader — the
  holder of the `plndr-svcs-lock` lease — advertises every service VIP. Live:
  `plndr-svcs-lock` and `plndr-cp-lock` are both held by `k3s-master-53`, every
  service VIP including .112 is on that node's `eth0`, and `odysseus/ntfy`
  carries `kube-vip.io/vipHost: k3s-master-53`. The duplicate therefore never
  becomes two nodes ARPing one address.
- **Claiming an address that is already plumbed is a no-op.** `AddIP` in
  `pkg/vip/address.go` prechecks and then uses `netlink.AddrReplace`, so the
  second claim does not fail.
- **Deleting the first claim while a second holds the same VIP does NOT tear the
  address down.** `deleteService` in `pkg/manager/services.go` intersects the
  departing instance's VIPs with those of every remaining instance and skips
  `cluster.Stop()` when they overlap. That is the entire reason for the order:
  with `observability/ntfy` already up, the odysseus prune leaves .112 plumbed.
  Prune first and there is no other holder, so kube-vip *does* remove .112 from
  eth0 and PVE's only working target is dark until the observability sync
  re-adds it.
- **Not established:** which of the two Services kube-proxy's DNAT for
  192.168.10.112:8091 wins during the overlap. Both backends are working ntfy
  pods, so a publish gets its 200 either way, but a message published in that
  window may be cached in the pod that is about to go away. The overlap lasts
  one manual sync; run the cutover outside the nightly vzdump window and the
  question does not arise. Measuring it properly would mean applying a duplicate
  Service to the live cluster, which was out of scope for the PR.

**The prune is automatic, and these are the tracking-ids that have to
disappear.** Both Applications carry
`{"automated":{"prune":true,"selfHeal":true}}` (verified live 2026-08-22) and
their headers agree — `app-odysseus.yaml` "prune ENABLED 2026-08-01",
`app-observability.yaml` "prune enabled 2026-05-31 after soak (#53)" — so this
is not one of the nine "prune NEVER turns on" Applications and no manual
`kubectl delete` is wanted. The odysseus sync must remove exactly two resources,
whose live tracking-ids are:

- `odysseus:apps/Deployment:odysseus/ntfy`
- `odysseus:/Service:odysseus/ntfy`

Post-sync checks. Run all of them and paste the literal values, per #50/#551:

```bash
# 1. the old objects are gone, not merely reported out of sync
kubectl --context epaflix -n odysseus get deploy ntfy    # want: NotFound
kubectl --context epaflix -n odysseus get svc ntfy       # want: NotFound

# 2. the new objects carry the new tracking-id
kubectl --context epaflix -n observability get deploy ntfy \
  -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/tracking-id}{"\n"}'
# want: observability:apps/Deployment:observability/ntfy
kubectl --context epaflix -n observability get svc ntfy \
  -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/tracking-id}{"\n"}'
# want: observability:/Service:observability/ntfy

# 3. .112 STILL ANSWERS. This is PVE's only working path (#1076) - the one
#    check that must not be skipped.
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.10.112:8091/v1/health   # want: 200
kubectl --context epaflix -n observability get svc ntfy \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip} {.metadata.annotations.kube-vip\.io/vipHost}{"\n"}'
# want: 192.168.10.112 <the plndr-svcs-lock holder>
ssh ubuntu@192.168.10.53 'ip -4 -o addr show eth0' | grep 192.168.10.112        # want: one hit

# 4. the new entry point answers, and the cache is bound
curl -sk -o /dev/null -w '%{http_code}\n' https://ntfy.epaflix.com/v1/health    # want: 200
kubectl --context epaflix -n observability get pvc ntfy-cache \
  -o jsonpath='{.status.phase}{"\n"}'                                          # want: Bound

# 5. both Applications settled
argocd app get observability --refresh -o json | jq '.status.sync.status, .status.health.status'
argocd app get odysseus      --refresh -o json | jq '.status.sync.status, .status.health.status'
```

If check 3 is not 200, kube-vip lost the address — the order was inverted, or
the services leader moved mid-cutover. Remedy without touching git: delete the
kube-vip pod on the `plndr-svcs-lock` holder so it rebuilds its service
instances, `kubectl --context epaflix -n kube-system delete pod -l
name=kube-vip --field-selector spec.nodeName=<that node>`. Do not "fix" it by
editing the Service.

#### Deploy gate for #1076 / #920 / #921: fix, prove, repoint, retire, unwire mail

One PR, five phases, and the order is the owner's from #920/#921: "Do not delete
the email receiver before the ntfy entry point exists and is proven." ArgoCD
syncs a whole Application at once, so the phases are bought by turning automated
sync off first and syncing single resources by hand. Run it in daylight, outside
the ~01:00 vzdump window.

Preconditions, from both PVE hosts and from TrueNAS: `getent hosts
ntfy.epaflix.com` answers `192.168.10.102` (control: `grafana.epaflix.com` must
NOT, it is a .101 record), and `curl --fail https://ntfy.epaflix.com/v1/health`
returns 200 with no `-k` (control: `curl https://192.168.10.102/v1/health` must
fail certificate verification, which is what proves verification is on). Back up
`/etc/pve/notifications.cfg` to `/root/notifications.cfg.bak-$(date +%F)` on
BOTH hosts and the TrueNAS `gpu-health-check.sh` to a `.bak` beside it. If
TrueNAS is unreachable, stop before phase 4: its cron still posts to .112.

Turn automated sync off in the only order that sticks — app-of-apps first, or it
re-adds `syncPolicy.automated` to the child:

```bash
kubectl --context epaflix -n argocd patch application app-of-apps \
  --type json -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
kubectl --context epaflix -n argocd patch application observability \
  --type json -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
```

1. **Fix.** `argocd app sync observability --resource apps:Deployment:observability/ntfy`
   (`strategy: Recreate`, so seconds of ntfy downtime), then `rollout status`.
2. **Prove.** Replay the archived 16311-byte failed-vzdump body in PVE's exact
   shape (base64 `Title`/`Tags`/`Priority` headers) against both the old .112
   URL and `https://ntfy.epaflix.com/pve-backups` — both 200 now, and the same
   POST before the sync must have returned 400 with body code `40014`. Then
   force a REAL error-severity vzdump failure on evanthoulaki: the task log must
   contain ``notified via target `ntfy-pve` `` and must NOT contain `could not
   notify via target` (control: that grep against the archived 2026-08-20/21
   task logs must hit).
3. **Repoint.** `pvesh set /cluster/notifications/endpoints/webhook/ntfy-pve
   --url 'https://ntfy.epaflix.com/pve-backups'` on one host, verify the `url`
   line on both (pmxcfs is shared), then
   `pvesh create /cluster/notifications/targets/ntfy-pve/test` from EACH host —
   that is what proves PVE's own perl HTTP client does HTTPS, SNI and the LE
   chain, which curl does not prove for it. Force a second real failure through
   the new URL. Swap `NTFY_URL` in the TrueNAS `gpu-health-check.sh` to
   `https://ntfy.epaflix.com/truenas-alerts` and watch one 15-minute cycle
   deliver.
4. **Retire .112.** `argocd app sync observability --resource :Service:observability/ntfy`,
   then the four checks in the retirement block above.
5. **Unwire mail, last.** `argocd app sync observability --resource :Secret:observability/alertmanager-config-secret`,
   wait for `config-reloader` on ALL THREE alertmanager replicas (#1053 means a
   single-replica check proves nothing), then parse the served config per
   replica: `email_configs` 0, `webhook_configs` 1 pointing at
   `ntfy.observability.svc.cluster.local:8091`, receiver `ntfy`,
   `route.receiver` `ntfy`, the critical child still `continue: true` with no
   siblings (control: the same parse before the sync must report
   `email_configs` 1 and receiver `email` on all three). Fire one synthetic
   `severity=critical` alert through a port-forward and count EXACTLY ONE
   message on topic `k8s-alertmanager` (control: two manual publishes must make
   the counter read 2). Only then
   `pvesh set /cluster/notifications/matchers/default-matcher --disable 1`, and
   diff the regenerated `/etc/pve/notifications.cfg` against the tracked
   `1-proxmox/pbs/notifications.cfg` — empty (control: the diff against the
   `/root` backup must be non-empty).

Restore: `argocd app sync app-of-apps` (git still carries `automated`), then
paste the literal
`kubectl --context epaflix -n argocd get application observability -o jsonpath='{.spec.syncPolicy.automated}'`
showing `selfHeal` and `prune` true, and a final full `argocd app sync
observability` reporting Synced/Healthy with an empty diff.

### TrueNAS GPU + ARC monitoring (#916-#919): deploy gate

Nothing in the PR that added this section touches the appliance. Everything
below happens **after** merge, in the order the #919 decision fixed: vendor the
rules → stand up the exporters → observe a real alert path → *then* remove the
cron. Delete the cron first and you reopen the window with no GPU signal at all,
which is the 23-hour condition (2026-08-08 → 09) this whole map came from.

What lands with the merge, applied by ArgoCD on its own: `truenas-exporters.yaml`
(2 Services + 2 EndpointSlices + 2 ServiceMonitors, #917) and two alert groups in
`alertmanager-config/custom-alerts.yaml`: `truenas-gpu` (5 expressions vendored
from `utkuozdemir/nvidia_gpu_exporter` v1.14.0, #916) and `truenas-memory`
(3 baseline-free ARC rules, #918). The exporters themselves are TrueNAS custom
apps installed by hand from `0-truenas/custom-apps/`.

**Subscribe the phone to `k8s-alertmanager` before step 5.** GPU alerts arrive
via Alertmanager from now on, not on `truenas-alerts`. The cron was the only
publisher of that topic. Deleting the cron before the new subscription exists
swaps one silence for another.

#### 0. Preconditions, and record the rollback recipe

```bash
# From TrueNAS: the ntfy hostname resolves and answers with a valid chain.
ssh truenas_admin@192.168.10.200 'getent hosts ntfy.epaflix.com; curl -fsS -4 -m 10 -o /dev/null -w "%{http_code}\n" https://ntfy.epaflix.com/v1/health'
# want: 192.168.10.102, then 200
ssh truenas_admin@192.168.10.200 'curl -m 5 -o /dev/null -w "%{http_code}\n" https://192.168.10.102/v1/health'
# CONTROL: must FAIL certificate verification. If it returns 200 the first
# check proved nothing about TLS.

# The cron row this gate eventually deletes. midclt is the only honest channel:
# the job is middleware-managed and `crontab -l` is a documented false negative.
ssh truenas_admin@192.168.10.200 'sudo midclt call cronjob.query | jq "[.[]|select(.command|contains(\"gpu\"))]"'
# want: the id-2 row, enabled=true, */15, /bin/bash /root/gpu-health-check.sh.
# PASTE IT VERBATIM. It is both the pre-state for step 5's control and the
# recipe to re-create the job if this gate has to be rolled back.
```

Back up the live script beside itself (`cp /root/gpu-health-check.sh
/root/gpu-health-check.sh.bak-$(date +%F)`) before anything else.

#### 1. Merge, sync, and read the pre-install state

```bash
SOPS_AGE_KEY=$(~/.pi/shared/skills/keepassxc-secrets/scripts/kpx.sh get sops-age-k3s-cluster) \
  kustomize build --enable-helm --enable-alpha-plugins --enable-exec 2-k3s/10.observability \
  | grep -cE '^ *- alert: (Nvidia|Truenas)'          # want: 8
kubectl --context epaflix -n observability get endpointslice truenas-node-exporter truenas-gpu-exporter -o wide
# want: both, addresses 192.168.10.200
kubectl --context epaflix -n observability port-forward pod/prometheus-kube-prometheus-stack-prometheus-0 19090:9090 &
curl -s localhost:19090/api/v1/rules | jq '{rules:([.data.groups[].rules[]]|length), err:([.data.groups[].rules[]|select(.health!="ok")]|length)}'
# baseline measured 2026-08-22 BEFORE this PR: {rules: 277, err: 0} over 53
# groups. want after sync: 285 and err still 0.
curl -s 'localhost:19090/api/v1/query?query=up%7Bjob%3D~%22truenas-.*%22%7D' | jq '.data.result|length'
# want: 0 HERE, and that is the point. It is the pre-install reading, and only the
# 0 → 1 flip across the install counts as install evidence (measured 2026-08-22:
# 0 results, both jobs absent).
```

#### 2. Install the two custom apps

Recipes and rollback: `0-truenas/custom-apps/node-exporter/README.md` and
`0-truenas/custom-apps/nvidia-gpu-exporter/README.md`. Then, from the host:

```bash
ssh truenas_admin@192.168.10.200 'curl -4 -m5 -s http://localhost:9100/metrics | grep -c "^node_zfs_arc_size"'   # want: >= 1
ssh truenas_admin@192.168.10.200 'curl -4 -m5 -s http://localhost:9835/metrics | grep -c "^nvidia_smi_gpu_info"'  # want: >= 1
ssh truenas_admin@192.168.10.200 'curl -4 -m5 -s http://localhost:9101/metrics'
# CONTROL: must FAIL (connection refused). A probe that cannot tell listening
# from not-listening has not earned its "yes" on 9100. The port-80 "answers"
# control is documented DEAD. Never reuse it.
ssh truenas_admin@192.168.10.200 'curl -4 -m5 -s http://localhost:9835/metrics | grep -c "^nvidia_smi_nvml_return_code"'
# want: 1, which is the proof the NVML backend is live.
```

**The NVML backend is not optional decoration.** `NvidiaGpuXidCritical` queries
`nvidia_smi_xid_last_timestamp_seconds`, which only the NVML backend produces
(upstream `docs/CONFIGURE.md` at v1.14.0), which is why the compose pins the
`1.14.0-nvml` image. If that flavor will not run on this box, fall back to the
plain `1.14.0` tag and **record the downgrade in the same breath**: the Xid rule
is then synthetic-evidence-only (step 4's promtool run), and a follow-up issue
owns getting NVML working. Do not leave a permanently-empty expression standing
without a written reason. That is the failure this design rejected the
textfile-collector route for.

If `node_zfs_arc_size` is absent, **stop before step 5**: the #918 decision's own
clause says the ARC signal collapses and the choice is forced back open. The cron
stays until that is resolved.

#### 3. Prometheus sees both targets

```bash
curl -s 'localhost:19090/api/v1/query?query=up%7Bjob%3D~%22truenas-.*%22%7D' | jq -r '.data.result[]|"\(.metric.job) \(.value[1])"'
# want: truenas-node-exporter 1, truenas-gpu-exporter 1 (was 0 results in step 1)
curl -s 'localhost:19090/api/v1/query?query=node_zfs_arc_c_max' | jq -r '.data.result[].value[1]'
# want the literal 12884901888, the 12 GiB cap from the 2026-08-09 recovery
# and the number TruenasArcCapDrift compares against. Paste it.
curl -s 'localhost:19090/api/v1/query?query=count(node_memory_MemAvailable_bytes)' | jq -r '.data.result[0].value[1]'
# positive control: 7 → 8 as the TrueNAS host joins the cluster's 7 nodes.
```

#### 4. Watch the alerts fire, and resolve

*Collection health, live, end-to-end.* Re-create the GPU app with
`--nvidia-smi-command=/bin/false` (exec backend), or stop the driver-library
injection on the NVML one, so the **exporter stays UP while collection fails**.
That is the 2026-08-09 shape: healthy-looking process, no card behind it.
`NvidiaGpuExporterCollectionFailing` must fire within its 10m window, reach
Alertmanager, and land on the phone via ntfy topic `k8s-alertmanager`. Restore
the real command and it must **RESOLVE**. An alert that cannot resolve fails
this gate exactly as hard as one that cannot fire.

*Scrape loss.* `sudo midclt call app.stop nvidia-gpu-exporter` → `TargetDown`
fires → `app.start` → resolves.

*Xid, and why it is synthetic.* An Xid cannot be provoked on a working card
without harming it. The committed unit tests are the evidence:

```bash
cd 2-k3s/10.observability/alertmanager-config
mkdir -p /tmp/promtool-rules
yq '.spec' custom-alerts.yaml > /tmp/promtool-rules/custom-alerts-rules-full.yaml
yq 'del(.spec.groups[].rules[].annotations) | .spec' custom-alerts.yaml > /tmp/promtool-rules/custom-alerts-rules.yaml
cp custom-alerts-tests.yaml /tmp/promtool-rules/
docker run --rm -v /tmp/promtool-rules:/w -w /w --entrypoint /bin/promtool \
  quay.io/prometheus/prometheus:v3.13.1 check rules custom-alerts-rules-full.yaml
docker run --rm -v /tmp/promtool-rules:/w -w /w --entrypoint /bin/promtool \
  quay.io/prometheus/prometheus:v3.13.1 test rules custom-alerts-tests.yaml
# CONTROL: break one parenthesis in a copy of the extracted rules and re-run
# `check rules`. It must exit non-zero, or the check is not evidence.
```

The two extracts exist because `promtool test rules` compares annotations too;
the behaviour run uses the annotation-stripped copy while `check rules` validates
the full one, templates included. Header of `custom-alerts-tests.yaml` has the
same recipe.

#### 5. Only now: delete the cron (#919)

```bash
ssh truenas_admin@192.168.10.200 'sudo midclt call cronjob.delete 2'
ssh truenas_admin@192.168.10.200 'sudo midclt call cronjob.query | jq "[.[]|select(.command|contains(\"gpu\"))]"'
# want: []. The SAME jq must have shown the id-2 row in step 0. A probe
# that cannot see the live row is not allowed to certify its absence.
ssh truenas_admin@192.168.10.200 'sudo mv /root/gpu-health-check.sh /root/gpu-health-check.sh.retired-$(date +%F)'
rm -rf artifacts/close-all-issues/gpu-scripts   # the stale git-ignored 2896-byte draft (#662)
```

Every probe in this section names **192.168.10.200** on purpose. The same GPU
probe pointed at 192.168.10.10/11 finds nothing and reads as "nothing to alert
on". That wrong-host answer is what falsely closed #919 once already. And never
use the `nvidia-smi -L | head || lspci` shape: a pipeline exits with `head`'s 0,
so the `||` fallback is dead code and the line prints empty either way.

#### 6. Close it out

Close #919 with the literal `cronjob.query` before/after pasted (#50/#551: never
"soak elapsed", always the value). Open the follow-up issues: the trailing PR for
the now-orphaned tracked `gpu-health-check.sh` and the guards-table row, the ARC
baseline (normal day + ollama load) that any tuned threshold waits on, the
upstream-drift watch row for `nvidia_gpu_exporter`, post-TrueNAS-update
verification of both apps, and the ntfy delivery guard for the
Alertmanager → ntfy leg.

### Alertmanager routing (the email leg is gone)

One receiver, one leg. `receivers[0]` is named **`ntfy`** and carries
`webhook_configs` only, posting to
`http://ntfy.observability.svc.cluster.local:8091` on topic `k8s-alertmanager`
with `send_resolved: true` (#568/PR #574; the url read `ntfy.odysseus...`
before the #914 namespace move). `route.receiver` is `ntfy`, the
`severity: critical` child route points at `ntfy` and keeps `continue: true`.

That `continue: true` is a live trap rather than a curiosity: it is a no-op
only because the child has no sibling. Add one route beside it and every
critical alert is delivered twice. This PR deliberately adds none — PVE backup
outcomes stay on PVE's native target because zero metrics match
`^pve_(backup|vzdump|task)` (#920), so there is nothing to route here.

**The SMTP leg was deleted on 2026-08-22 (#921).** `email_configs` and all five
`global.smtp_*` keys are gone; `global` holds `resolve_timeout` and nothing
else. The owner's decision on #920/#921 is that ntfy is the load-bearing
receiver and "the dead email receiver gets deleted, not fixed": the relay was
unroutable for months (#461) while the config read as though two paths existed.
The receiver's old name said `email` while the thing that delivered was ntfy,
so the name went with the leg. What that costs, stated plainly: alerts now have
a single delivery path, and it runs inside the very cluster whose problems it
reports.

The consequence for review: `secrets/alertmanager-config-secret.enc.yaml` now
contains **no secret at all** — `smtp_auth_password` was the only one, and
`smtp_auth_username` was `alert@epaflix.com`, already in tracked git. It stays
SOPS-encrypted regardless, because the repo-wide rule is that every
ArgoCD-reconciled Secret is a `*.enc.yaml` and because the next credential to
appear in `alertmanager.yaml` lands in this same file. Reviewability is bought
back by procedure instead: **every PR touching this file pastes the full
decrypted after-state** in its description. Read it without decrypting anything
you do not need:

```bash
sops -d --extract '["stringData"]["alertmanager.yaml"]' \
  2-k3s/10.observability/secrets/alertmanager-config-secret.enc.yaml
```

The drift check that used to live here compared `global.smtp_smarthost` against
the credential store's `alert_email_hostname` by hash — the #782 "two files
describe one relay" pair. That pair no longer exists, because the deployed half
is gone. The store still holds the relay credentials and the relay account is
still provisioned; whether to rotate or retire them is a tracked follow-up, not
something this file answers.

The Prometheus Operator regenerates
`alertmanager-kube-prometheus-stack-alertmanager-generated` when the Secret
changes and the `config-reloader` sidecar POSTs `/-/reload`, so a config change
needs no pod restart (the #299 `secretKeyRef` gap does not apply — this is a
mounted volume, not an env var). Verify it on **all three replicas**, never one:
#1053 means Loki-sourced alerts reach only one of them, and a single-replica
check cannot tell a rolled-out config from a stale one.

### Default Alerts

- **Node**: Memory >85%, Disk >90%, CPU sustained high load
- **Pods**: CrashLoopBackOff, Failed, OOMKilled
- **Storage**: PVC usage >85%
- **Prometheus**: Scrape failures, rule evaluation failures
- **Loki**: Storage >85%, ingestion failures
- **Proxmox**: Host unreachable
- **Cilium**: Agent down, high packet drop rate
- **Istio**: Sidecar crash loops

### Testing alert delivery

Delivery lands on ntfy topic `k8s-alertmanager`, not in a mailbox. Repeat the
first command against replicas `-1` and `-2` as well — #1053 means an alert can
reach one replica only.

```bash
# Trigger test alert
kubectl --context epaflix exec -n observability alertmanager-kube-prometheus-stack-alertmanager-0 -- \
  amtool alert add test_alert alertname=TestNtfyAlert

# Check AlertManager status
kubectl --context epaflix port-forward -n observability svc/kube-prometheus-stack-alertmanager 9093:9093
# Open http://localhost:9093

# Silence alert
kubectl --context epaflix exec -n observability alertmanager-kube-prometheus-stack-alertmanager-0 -- \
  amtool silence add alertname=TestNtfyAlert
```

## Storage Management

### Storage Architecture

Observability components use K3s `local-path` StorageClass, which provisions storage on the node's local filesystem. This filesystem is actually backed by TrueNAS iSCSI targets attached to each VM at the Proxmox layer.

**Storage Flow:**
```
Prometheus PVC → local-path provisioner → /var/lib/rancher/k3s/storage/ → VM disk → iSCSI target → TrueNAS Apps pool (SSD)
```

**Important Considerations:**
- **ReadWriteOnce limitation**: PVCs are bound to specific nodes, pods cannot move to other nodes
- **No automatic HA**: If a worker node fails, Prometheus/Loki data is inaccessible until the node recovers
- **Scaling constraint**: Prometheus is limited to 1 replica (ReadWriteOnce PVC). Grafana runs 2 replicas (uses database-backed sessions, not local storage).

For production environments requiring HA, consider:
- Deploying Prometheus with remote write to long-term storage (Thanos, VictoriaMetrics)
- Using Loki with S3-compatible object storage backend
- Migrating to shared storage (Longhorn, Ceph RBD with RWX support)

### Prometheus Storage

- **Size**: 25Gi PVC using local-path StorageClass
- **Retention**: 8 days (`retention: 8d`), bounded in practice by `retentionSize: "20GB"`
- **StorageClass**: local-path (provisioned from node's `/var/lib/rancher/k3s/storage/`, which resides on VM disk backed by TrueNAS iSCSI)
- **Access Mode**: ReadWriteOnce
- **Measured usage**: 18.05 GiB of blocks on 2026-08-21, i.e. at the 20GB (= 18.63 GiB) cap
- **Note**: Worker nodes have 40GB total disk space from iSCSI targets

#### Why 8 days and not 15 (#913)

15d was configured and never reached: `retentionSize: "20GB"` bound first, and nothing
reported the gap, so the documented retention and the real retention drifted apart in
silence for months.

Measured 2026-08-21: 8.25 days of history, 18.05 GiB of blocks, 515,435 head series —
2.19 GiB/day, so 15 days needs roughly 33 GiB. The PVC cannot grow to hold that:
Prometheus runs on k3s-worker-62, whose 48G root disk has 9.8G free, and #463 records
what happened last time Prometheus outgrew it (30G of blocks pushed that node to 85% and
broke kubelet image GC). So `retention: 8d` is the retention this cluster actually has,
and at 8d the time bound binds first (17.5 GiB, under the cap).

Verify against live state:

```promql
(time() - prometheus_tsdb_lowest_timestamp_seconds) / 86400   # days of history, ~8.25 on 2026-08-21
prometheus_tsdb_head_series                                    # cardinality, ~515k on 2026-08-21
```

`PrometheusRetentionBelowConfigured` (in `alertmanager-config/custom-alerts.yaml`, group
`prometheus-storage`) fires if real retention drops below 7d, so the two settings cannot
disagree silently again.

Consequence for investigations: history older than ~8 days does not exist, the `ALERTS`
series included. Questions about when an alert first started firing cannot be answered
from Prometheus beyond that window.

### Loki Storage

- **Size**: 15Gi PVC using local-path StorageClass
- **Retention**: 31 days
- **StorageClass**: local-path (provisioned from node's `/var/lib/rancher/k3s/storage/`, which resides on VM disk backed by TrueNAS iSCSI)
- **Access Mode**: ReadWriteOnce
- **Expected usage**: ~11GB (7 nodes × 500MB/day × 31d)
- **Note**: Worker nodes have 40GB total disk space from iSCSI targets

### Monitoring Storage Usage

```bash
# Check PVC usage
kubectl --context epaflix get pvc -n observability

# Detailed storage metrics in Grafana
# Dashboard: "Kubernetes / Persistent Volumes"
```

### Cleanup Old Data

This stack is ArgoCD-managed with `selfHeal: true`: an imperative `helm upgrade`
is reverted on the next reconcile, and worse, it refreshes the stale `helm`
field manager that silently pins fields against future git changes (#779/#1052).
The only supported change path is git:

1. Edit the value in the tracked file - Loki retention in `loki-values.yaml`
   (`loki.limits_config.retention_period`), Prometheus retention in
   `prometheus-values.yaml` (`prometheus.prometheusSpec.retention`).
2. Merge through a PR and let ArgoCD sync.
3. Verify with **both** the manifest literal and the live value (#50/#551), e.g.:

```bash
grep -n 'retention' 2-k3s/10.observability/prometheus-values.yaml
kubectl --context epaflix -n observability get prometheus -o jsonpath='{.items[0].spec.retention}'
```

## Troubleshooting

### Grafana Not Loading

```bash
# Check pods
kubectl --context epaflix get pods -n observability -l app.kubernetes.io/name=grafana

# Check logs
kubectl --context epaflix logs -n observability -l app.kubernetes.io/name=grafana -f

# Check database connection
kubectl --context epaflix exec -n observability deployment/kube-prometheus-stack-grafana -- \
  psql -h postgres-pooler.postgres-system.svc.cluster.local -U observability -d observability -c "SELECT 1;"
```

### Prometheus Not Scraping Targets

```bash
# Port forward to Prometheus UI
kubectl --context epaflix port-forward -n observability svc/kube-prometheus-stack-prometheus 9090:9090

# Open http://localhost:9090/targets
# Check for targets in "DOWN" state

# Check ServiceMonitor
kubectl --context epaflix get servicemonitor -n observability
kubectl --context epaflix describe servicemonitor -n observability <name>
```

### Control-plane exporter Endpoints (issue #147)

The three control-plane exporters — `kube-controller-manager` (:10257),
`kube-scheduler` (:10259) and `kube-etcd` (:2381) — are scraped via
**selector-less headless Services** that the kube-prometheus-stack chart
templates into `kube-system`. Their `endpoints:` lists (the three master
InternalIPs `10.0.0.51/52/53`) live in `prometheus-values.yaml` under the
`kubeControllerManager` / `kubeScheduler` / `kubeEtcd` blocks.

**Mechanism (Option B).** Because those Services have no pod selector,
Kubernetes does not auto-populate their backing endpoints; the chart hand-rolls
a legacy v1 `Endpoints` object (#121). ArgoCD's global `resource.exclusions`
(`2-k3s/11.argocd/helm-values.yaml`) drops core `Endpoints` cluster-wide, so
those chart Endpoints are **out-of-band**: not in any Application's managed set,
not selfHeal-recreatable, and they silently vanish if anything deletes them —
the fragility tracked in **#147**.

The fix declares the backing endpoints as **git-managed static
`discovery.k8s.io/v1` EndpointSlices** in
`control-plane-endpointslices.yaml`, scoped to the observability Application.
`EndpointSlice` is **not** in the ArgoCD exclusion list (proven by the
`jellyfin-truenas` static slice in `08.servarr`), so ArgoCD reconciles them and
selfHeal recreates them. EndpointSlice is also the v1 replacement that is
future-proof against the v1 `Endpoints` deprecation in **k8s 1.33+**.

**Selector-strip truth.** Keeping the `endpoints:` lists in
`prometheus-values.yaml` is what makes the chart render the Services
*selector-less* in the first place — so there is **no recurring manual
selector-strip step**. Leave those lists in place. The chart still renders a
now-redundant excluded v1 `Endpoints` alongside our EndpointSlices; Prometheus
deduplicates the targets, so the overlap is harmless. The
`kubernetes.io/service-name` label on each slice binds it to its headless
Service so the matching ServiceMonitor target resolves to the master IPs.

**DR / verify runbook.**

```bash
# Recreate the slices after any loss (ArgoCD owns them now):
argocd app sync observability

# Confirm the 3 static slices exist with the three master IPs:
kubectl --context epaflix -n kube-system get endpointslice | grep -Ei "controller-manager|scheduler|etcd"
kubectl --context epaflix -n kube-system get endpointslice \
  kube-prometheus-stack-kube-controller-manager-static \
  kube-prometheus-stack-kube-scheduler-static \
  kube-prometheus-stack-kube-etcd-static \
  -o custom-columns=NAME:.metadata.name,IPS:.endpoints[*].addresses,PORT:.ports[*].port

# Confirm Prometheus targets are UP for all three control-plane jobs:
kubectl --context epaflix port-forward -n observability svc/kube-prometheus-stack-prometheus 9090:9090
# Open http://localhost:9090/targets and check that
#   serviceMonitor/observability/kube-prometheus-stack-kube-controller-manager
#   serviceMonitor/observability/kube-prometheus-stack-kube-scheduler
#   serviceMonitor/observability/kube-prometheus-stack-kube-etcd
# each show 3 endpoints (10.0.0.51/52/53) in state UP.
```

### Loki Not Receiving Logs

```bash
# Check Promtail pods
kubectl --context epaflix get pods -n observability -l app.kubernetes.io/name=promtail

# Check Promtail logs
kubectl --context epaflix logs -n observability -l app.kubernetes.io/name=promtail --tail=100

# Test Loki query
kubectl --context epaflix port-forward -n observability svc/loki-gateway 3100:80
curl http://localhost:3100/ready
```

### Cilium Issues

```bash
# Check status
cilium status

# Check agent logs
kubectl --context epaflix logs -n kube-system ds/cilium -c cilium-agent --tail=100

# Restart Cilium agents
kubectl --context epaflix rollout restart ds/cilium -n kube-system

# Check connectivity
cilium connectivity test
```

### Istio Sidecar Not Injecting

```bash
# Verify namespace label
kubectl --context epaflix get namespace app-authentik --show-labels

# Check webhook
kubectl --context epaflix get mutatingwebhookconfiguration istio-sidecar-injector -o yaml

# Manual injection (if automatic fails)
kubectl --context epaflix get deployment -n app-authentik authentik-server -o yaml | istioctl kube-inject -f - | kubectl --context epaflix apply -f -
```

## Maintenance

### Updating Components

Chart versions are pinned in this directory's kustomization/HelmChart config and
bumped via PR (Renovate proposes them). Never `helm upgrade` an ArgoCD-adopted
release - it re-animates the stale `helm` field manager (#779/#1052). To update:
bump the pinned chart version in git, merge, let ArgoCD sync, verify the new
version with `kubectl --context epaflix -n observability get pods -o
jsonpath='{range .items[*]}{.spec.containers[0].image}{"\n"}{end}' | sort -u`.

```bash

# Update Cilium
cilium upgrade --version 1.15.0

# Update Istio
istioctl upgrade
```

### Backup

```bash
# Backup Prometheus data
kubectl --context epaflix exec -n observability prometheus-kube-prometheus-stack-prometheus-0 -c prometheus -- \
  tar czf /prometheus/backup-$(date +%Y%m%d).tar.gz /prometheus/data

# Backup Grafana dashboards (stored in PostgreSQL)
PGPASSWORD='<POSTGRES_PASSWORD>' pg_dump -h 192.168.10.105 -U observability observability > grafana-backup.sql

# Backup Loki data
kubectl --context epaflix exec -n observability loki-backend-0 -- tar czf /tmp/loki-backup.tar.gz /var/loki
```

## Resource Usage

### Expected Cluster Overhead

| Component | CPU | RAM | Count | Total RAM |
|-----------|-----|-----|-------|-----------|
| Prometheus | 500m-2000m | 2-4Gi | 2 | 4-8Gi |
| Grafana | 250m-1000m | 512Mi-1Gi | 2 | 1-2Gi |
| Loki (write/read/backend) | 500m-1000m | 1-2Gi | 6 | 6-12Gi |
| AlertManager | 100m-200m | 128-256Mi | 3 | 384-768Mi |
| node-exporter | 100m-200m | 100-200Mi | 7 | 700Mi-1.4Gi |
| Promtail | 100m-200m | 128-256Mi | 7 | 896Mi-1.8Gi |
| Cilium | 200m-500m | 300-500Mi | 7 | 2.1-3.5Gi |
| Istio sidecars | 100m-500m | 128Mi-512Mi | ~30 | 3.8-15Gi |
| **Total** | - | - | - | **19-45Gi** |

**Cluster Capacity After Upgrades**: 42GB total (6×6GB + 1×8GB)
**Headroom**: Comfortable for all observability components + workloads

## Future Enhancements

### GPU Worker Setup (When GPU Available)

Worker-65 is available for GPU workloads (currently 22GB RAM, same as other workers):

```bash
# When GPU physically installed:
# 1. Shutdown worker-65 VM in Proxmox
# 2. Optionally increase RAM: qm set 1065 --memory 32768  # 32GB if transcoding needs it
# 3. Add PCIe device: qm set 1065 --hostpci0 <GPU_PCI_ID>,pcie=1
# 4. Boot VM
# 5. Install NVIDIA drivers
# 6. Label node: kubectl --context epaflix label node k3s-worker-65 nvidia.com/gpu=present
# 7. Update Jellyfin/Tdarr with GPU resource requests
```

### Additional Monitoring

- **Service Mesh Tracing**: Jaeger or Zipkin integration with Istio
- **Cost Monitoring**: Kubecost for resource cost allocation
- **Security Scanning**: Falco for runtime security
- **Backup Monitoring**: Velero integration with alerts

## Support

For issues or questions:
- Check logs: `kubectl --context epaflix logs -n observability <pod-name>`
- View events: `kubectl --context epaflix get events -n observability --sort-by='.lastTimestamp'`
- Grafana forums: https://community.grafana.com/
- Cilium Slack: https://cilium.io/slack
- Istio discuss: https://discuss.istio.io/
