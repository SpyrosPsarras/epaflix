# Traefik Reverse Proxy for *.epaflix.com

This deployment configures Traefik as a reverse proxy with automatic TLS certificates via Let's Encrypt and Cloudflare DNS challenge.

## Architecture

- **Static IP**: `192.168.10.101` (via kube-vip LoadBalancer)
- **TLS**: Let's Encrypt with Cloudflare DNS-01 challenge (supports wildcard `*.epaflix.com`)
- **Namespace**: `traefik-system`
- **Replicas**: 1
- **Storage**: Local k3s storage (`local-path` StorageClass)
- **Router**: Forward ports 80/443 → `192.168.10.101`
- **DNS**: Pi-hole points `*.epaflix.com` → router public IP or `192.168.10.101` for LAN

## Client source IPs and `externalTrafficPolicy` (#560)

Traefik logs `10.42.0.0` as the client for every request that comes from outside the
cluster. That is a recorded constraint, not an open bug. `#560` is closed as not planned
on the measurements below, all taken 2026-08-10 against the live cluster.

### What Traefik actually logs, both paths

Two probes, same second, same origin workstation `192.168.10.177` (public IP
`81.167.233.67`), against `sonarr.epaflix.com`:

```
10.42.0.0 - - [10/Aug/2026:13:05:43 +0000] "GET /probe560-lan-1786367143 HTTP/2.0" 302 ...
10.42.0.0 - - [10/Aug/2026:13:05:44 +0000] "GET /probe560-cf-1786367143 HTTP/2.0"  302 ...
```

- **LAN path** - Pi-hole answers `sonarr.epaflix.com` with `192.168.10.101`, so the client
  hits the kube-vip VIP directly. Logged client: `10.42.0.0`. **Mechanism: kube-proxy SNAT
  on a cross-node LoadBalancer hop.** `svc/traefik` runs `externalTrafficPolicy: Cluster`,
  the VIP is on `k3s-master-51` and the only Traefik pod is on `k3s-worker-62`, so
  kube-proxy rewrites the source before the packet leaves master-51.
- **Cloudflare path** - forced with `curl --resolve sonarr.epaflix.com:443:104.21.59.155`,
  so the request goes out to a Cloudflare edge and back in through the router port forward.
  Logged client: `10.42.0.0`, the same value. **Mechanism: two rewrites stacked.** Cloudflare
  terminates and re-originates the connection from its own edge IP, the router DNATs that to
  `192.168.10.101`, then the same kube-proxy SNAT applies. The client IP is gone twice over.

`10.42.0.0` is not a guess and not ambiguous. It is `k3s-master-51`'s flannel VXLAN
address, read on the node itself:

```
4: flannel.1    inet 10.42.0.0/32 scope global flannel.1
5: cni0         inet 10.42.0.1/24 brd 10.42.0.255 scope global cni0
```

`cni0` holds `10.42.0.1` as the pod gateway, so host-local IPAM hands out `10.42.0.2` and
up. **No pod can ever have `10.42.0.0`** - live count of pods cluster-wide with that IP is
`0`, and the two non-hostNetwork pods on master-51 today are `10.42.0.104` and
`10.42.0.105`. So a logged `10.42.0.0` means exactly one thing: SNAT'd through master-51,
which is the VIP path. It does **not** mean "or a pod on master-51". Earlier `#296` and
`#560` notes claimed that ambiguity. It is not real, and no out-of-band premise is needed
to read the log.

### Cloudflare already gives the real client IP on the public path

This is the part that shrinks the issue. Traefik forwards `CF-Connecting-IP` untouched.
Measured with a throwaway `traefik/whoami` route on both paths, headers as received by the
backend:

```
LAN path:
  X-Forwarded-For: 10.42.0.0
  X-Real-Ip: 10.42.0.0
  (no header carries the client IP)

Cloudflare path:
  X-Forwarded-For: 10.42.0.0
  X-Real-Ip: 10.42.0.0
  Cf-Connecting-Ip: 81.167.233.67      <- the real client IP
  Cf-Ipcountry: NO
  Cf-Ray: a28f389d9f74b51d-OSL
```

So the gap is **half the size the issue describes**:

- **Public / internet traffic: not a gap.** The real client IP arrives in
  `CF-Connecting-IP`, plus country in `CF-IPCountry`. Anything that needs to identify an
  external caller can read that header today. `X-Forwarded-For` is useless because Traefik
  overwrites it with the SNAT peer - `forwardedHeaders.trustedIPs` is unset, which is
  correct, since trusting an untrusted peer would let any LAN host spoof the header.
- **LAN traffic: a real gap.** A request that hits `192.168.10.101` directly never touches
  Cloudflare, so there is no `CF-Connecting-IP` and nothing else carries
  `192.168.10.177`. LAN clients are indistinguishable from each other at Traefik.

### What `externalTrafficPolicy: Local` would cost

It would not degrade availability. It would take the ingress down completely.

- **VIP-capable nodes and Traefik-capable nodes are disjoint sets.** `ds/kube-vip` carries
  `nodeSelector: node-role.kubernetes.io/control-plane: "true"`
  (`2-k3s/01.kube-vip/kube-vip-daemonset.yaml:59-60`), desired 3, ready 3, running only on
  `k3s-master-51/52/53`. All three masters carry
  `node-role.kubernetes.io/control-plane:NoSchedule`, and `deploy/traefik` has empty
  `tolerations`, empty `nodeSelector` and empty `affinity`, so Traefik can only land on a
  worker. The VIP can only live where Traefik cannot run.
- **So under `Local` the VIP node has no local endpoint, on every possible node.** That is a
  permanent 100% black hole across the **20** Pi-hole names that resolve to
  `192.168.10.101`, not a partial outage.
- **kube-vip service election does not rescue it.** Election can only move a service VIP to a
  node that has an endpoint; the only endpoint is `10.42.5.105` on `k3s-worker-62`; kube-vip
  does not run on workers. There is no eligible node to move to. For the record election is
  off anyway: `svc_election` and `enableServicesElection` are unset in the DaemonSet env,
  only the cluster-wide `vip_leaderelection=true` is set, and there is no per-service
  `kubevip-*` lease - just one global `plndr-svcs-lock` held by `k3s-master-51`. All four
  VIPs sit on that one node, confirmed by reading `eth0` on each master:
  `192.168.10.100`, `.101`, `.102` and `.110` are all secondaries on `k3s-master-51`;
  `k3s-master-52` and `k3s-master-53` hold none.
- **More Traefik replicas is blocked separately.**
  `values/traefik-values.yaml:122` pins `replicas: 1` with the reason inline (`ACME requires
  single replica for certificate management`), and `values/traefik-values.yaml:114-118` keeps
  the ACME store on a `local-path` PVC with `accessMode: ReadWriteOnce`. A second replica on
  another node cannot mount it. So "add replicas until every node has an endpoint" is not a
  small change either.

### Nothing consumes the client IP today

There are 4 middlewares cluster-wide - `servarr/jellyseerr-to-seerr` (`redirectRegex`),
`traefik-system/authentik-forwardauth` (`forwardAuth`), `traefik-system/redirect-https`
(`redirectScheme`) and `traefik-system/security-headers` (`headers`). Count using
`ipAllowList` or `ipWhiteList`: **0**. The LAN-versus-internet split that would otherwise
want an IP check is done with a separate entry point instead - `traefik-internal` on
`192.168.10.102`, used by `searxng`, `qbittorrent`, `remote-pi` and `cliproxy`.

### The condition that reopens this

Reopen `#560` when **a LAN client needs to be identified by IP at Traefik**. Concretely, the
first time any of these is true:

- an `ipAllowList` or `ipWhiteList` middleware is wanted on a route reachable from the LAN,
  or
- a LAN-origin request has to be attributed to a specific host in the access log, or
- the entry-point split (`traefik-internal` on `192.168.10.102`) stops being enough to
  separate LAN from internet.

Do **not** reopen it for public traffic - use `CF-Connecting-IP`. And do not write an
`ipAllowList` before this is fixed: it would silently match `10.42.0.0` and look like it
works.

Fixing it then is an owner-level ingress topology change, all three parts together:

1. Make the VIP-capable and Traefik-capable node sets overlap - either run kube-vip on
   workers, or give Traefik a control-plane toleration.
2. Move ACME state off the `ReadWriteOnce` `local-path` PVC onto something a multi-replica
   Traefik can share, or take cert issuance out of the proxy entirely.
3. Only then flip `externalTrafficPolicy` to `Local` and retest all 20 hostnames.

A cheaper route worth pricing at that point is PROXY protocol from kube-vip, which delivers
the real IP without moving any pods. It was not priced here because nothing needs it yet.

### Re-check all of it, read-only

```bash
kubectl --context epaflix -n traefik-system get svc traefik traefik-internal \
  -o custom-columns='NAME:.metadata.name,ETP:.spec.externalTrafficPolicy,LBIP:.status.loadBalancer.ingress[0].ip'
kubectl --context epaflix get nodes \
  -o custom-columns='NODE:.metadata.name,TAINTS:.spec.taints[*].key,PODCIDR:.spec.podCIDR'
kubectl --context epaflix -n traefik-system get deploy traefik \
  -o jsonpath='replicas={.spec.replicas} tolerations={.spec.template.spec.tolerations} nodeSelector={.spec.template.spec.nodeSelector} affinity={.spec.template.spec.affinity}{"\n"}'
kubectl --context epaflix -n kube-system get ds kube-vip \
  -o jsonpath='{.spec.template.spec.nodeSelector}{"\n"}'
kubectl --context epaflix -n kube-system get leases | grep plndr
kubectl --context epaflix get middleware -A -o json \
  | jq -r '.items[] | "\(.metadata.namespace)/\(.metadata.name) keys=\(.spec|keys|join(","))"'

# What Traefik logs for a LAN request. Expect 10.42.0.0.
curl -sk -o /dev/null "https://sonarr.epaflix.com/probe560-$(date +%s)"
kubectl --context epaflix -n traefik-system logs deploy/traefik --since=60s | grep probe560
```

## Prerequisites

1. Cloudflare API token with DNS edit permissions for `epaflix.com`
2. Router configured to forward TCP 80/443 to `192.168.10.101`
3. Pi-hole DNS records: `*.epaflix.com` → router public IP (or `192.168.10.101` for LAN)
4. **k3s DNS configuration** without search domains (see step 0 below)

## Deployment Steps

### Preferred: adopt with ArgoCD

For an already-running cluster with ArgoCD installed, Traefik is reconciled
from this directory through `2-k3s/11.argocd/apps/app-traefik.yaml`. The
`kustomization.yaml` renders the upstream Traefik Helm chart with
`values/traefik-values.yaml` and includes the git-tracked middleware,
dashboard route, and external service proxy manifests.

Keep runtime secrets and certificate state outside git:
- `cloudflare-api-token` is created imperatively from
  the credential store `.github/instructions/secrets.enc.yaml` (key
  `cloudflare-api-token`).
- ACME state remains in the chart-managed persistence volume at
  `/data/acme.json`.
- `certificates/cloudflare-origin-cert.yaml` is a placeholder/template and is
  not included in the ArgoCD kustomization.

Safe adoption flow:
```bash
kubectl --context epaflix kustomize --enable-helm 2-k3s/05.traefik-deployment >/tmp/traefik-rendered.yaml
kubectl --context epaflix -n traefik-system get secret cloudflare-api-token
kubectl --context epaflix -n traefik-system get pvc
kubectl --context epaflix -n traefik-system get svc traefik -o wide
kubectl --context epaflix apply -f 2-k3s/11.argocd/apps/app-traefik.yaml
argocd app diff traefik
argocd app sync traefik
```

Only sync after confirming the diff preserves `192.168.10.101`, one Traefik
replica, the existing ACME volume, and the Cloudflare token Secret reference.

### 0. Configure k3s to use custom DNS resolv.conf (REQUIRED for Let's Encrypt)

This prevents pods from inheriting the host's DNS search domain (which would cause ACME DNS queries to fail) and pins pod-side resolv.conf to the main Pi-hole at `192.168.10.30`.

**Why every node (not just masters):** kubelet writes the pod sandbox `/etc/resolv.conf` from this file on the node where the pod runs. Pods scheduled to workers use the worker's `/etc/k3s-resolv.conf`, not any master's. Skipping workers leaves their pods on whatever DNS was in the file at install time — a real incident we hit on 2026-05-16 when `192.168.10.200` lingered on every node and caused CoreDNS to forward all `.` queries to TrueNAS instead of `192.168.10.30`.

**On all k3s nodes — masters and workers:**

```bash
# Masters (51, 52, 53) — service is k3s.service
for ip in 192.168.10.51 192.168.10.52 192.168.10.53; do
  echo "Configuring k3s master on $ip..."
  ssh ubuntu@$ip "sudo mkdir -p /etc/rancher/k3s && \
    echo 'kubelet-arg:
  - \"resolv-conf=/etc/k3s-resolv.conf\"' | sudo tee /etc/rancher/k3s/config.yaml && \
    echo 'nameserver 192.168.10.30' | sudo tee /etc/k3s-resolv.conf && \
    sudo systemctl restart k3s"
done

# Workers (61, 62, 63, 65) — service is k3s-agent.service
for ip in 192.168.10.61 192.168.10.62 192.168.10.63 192.168.10.65; do
  echo "Configuring k3s worker on $ip..."
  ssh ubuntu@$ip "sudo mkdir -p /etc/rancher/k3s && \
    echo 'kubelet-arg:
  - \"resolv-conf=/etc/k3s-resolv.conf\"' | sudo tee /etc/rancher/k3s/config.yaml && \
    echo 'nameserver 192.168.10.30' | sudo tee /etc/k3s-resolv.conf && \
    sudo systemctl restart k3s-agent"
done
```

Wait for cluster to stabilize:
```bash
kubectl --context epaflix get nodes
```

If you only need to update an already-deployed cluster (without restarting k3s), edit `/etc/k3s-resolv.conf` on every node then bounce CoreDNS so new pod sandboxes pick up the change:
```bash
kubectl --context epaflix rollout restart -n kube-system deployment/coredns
```

Verify the new CoreDNS sandbox `/etc/resolv.conf` only lists `192.168.10.30`:
```bash
ssh ubuntu@<node-running-coredns> \
  "sudo cat /var/lib/rancher/k3s/agent/containerd/io.containerd.grpc.v1.cri/sandboxes/\$(sudo crictl pods --name coredns -q | head -1)/resolv.conf"
```

### 1. Create namespace
```bash
kubectl --context epaflix apply -f namespace.yaml
```

### 2. Bootstrap/manual deployment script
```bash
./01.deploy.sh
```

This script will:
1. Create the namespace
2. Create the Cloudflare API token secret
3. Deploy Traefik with Helm (automatically creates local-path PVC)
4. Wait for LoadBalancer IP assignment
5. Apply middleware

Use this path for initial bootstrap before ArgoCD exists, or for emergency
manual recovery. Do not run `helm uninstall traefik` during ArgoCD adoption.

### Manual deployment (Alternative)

### 3. Create Cloudflare API token secret
```bash
kubectl --context epaflix create secret generic cloudflare-api-token \
  --namespace=traefik-system \
  --from-literal=api-token=<CLOUDFLARE_API_TOKEN>
```

### 4. Deploy Traefik via Helm
```bash
helm repo add traefik https://traefik.github.io/charts
helm repo update
helm install traefik traefik/traefik \
  -n traefik-system \
  -f values/traefik-values.yaml
```

### 5. Verify LoadBalancer IP
```bash
kubectl --context epaflix -n traefik-system get svc traefik
# Should show EXTERNAL-IP: 192.168.10.101
```

### 6. Verify NFS storage
```bash
kubectl --context epaflix -n traefik-system get pv,pvc
# Should show traefik-nfs-pv and traefik-acme-storage as Bound
```

### 7. Apply middleware
```bash
kubectl --context epaflix apply -f middleware/
```

### 8. Deploy test application (whoami)
```bash
kubectl --context epaflix apply -f examples/whoami-demo.yaml
```

### 9. Wait for certificate issuance (~2 minutes)
```bash
# Check Traefik logs
kubectl --context epaflix -n traefik-system logs -l app.kubernetes.io/name=traefik -f

# Verify both replicas are running
kubectl --context epaflix -n traefik-system get pods -o wide
```

## Testing

### Internal (LAN) Test
```bash
curl https://whoami.epaflix.com
```

### External Test (if router is configured)
```bash
curl https://whoami.epaflix.com
# From outside your network
```

### Access Traefik Dashboard
```bash
# Navigate to: https://traefik.epaflix.com/dashboard/
```

## Adding New Applications

For each new app, create:

1. **Namespace** (optional, can reuse existing)
2. **Deployment** (your app)
3. **Service** (ClusterIP)
4. **IngressRoute** with `certResolver: cloudflare`

Example:
```yaml
apiVersion: traefik.containo.us/v1alpha1
kind: IngressRoute
metadata:
  name: myapp-https
  namespace: myapp
spec:
  entryPoints:
    - websecure
  routes:
    - match: Host(`myapp.epaflix.com`)
      kind: Rule
      services:
        - name: myapp
          port: 80
  tls:
    certResolver: cloudflare
    domains:
      - main: epaflix.com
        sans:
          - "*.epaflix.com"
```

## Troubleshooting

### Certificate not issued
```bash
# Check Traefik logs
kubectl --context epaflix -n traefik-system logs -l app.kubernetes.io/name=traefik | grep -i acme

# Verify Cloudflare token
kubectl --context epaflix -n traefik-system get secret cloudflare-api-token -o yaml
```

### LoadBalancer pending
```bash
# Check kube-vip cloud provider
kubectl --context epaflix -n kube-system get configmap kubevip -o yaml
kubectl --context epaflix -n kube-system logs -l app=kube-vip-cloud-provider
```

### DNS not resolving
- Verify Pi-hole has `*.epaflix.com` → `192.168.10.101` (LAN) or router public IP
- Check router port forwarding: 80/443 → `192.168.10.101`

## Router Configuration

**Port Forwarding Rules:**
```
External Port 80  (TCP) → 192.168.10.101:80
External Port 443 (TCP) → 192.168.10.101:443
```

## Pi-hole DNS Configuration

**For LAN-only access:**
```
DNS Record: *.epaflix.com → 192.168.10.101
```

**For external access through router:**
```
DNS Record: *.epaflix.com → <your-router-public-ip>
```

## Cloudflare Settings (if using Cloudflare proxy)

If you want to use Cloudflare's proxy (orange cloud):
1. Set DNS to proxy through Cloudflare
2. SSL/TLS mode: "Full" or "Full (strict)" with origin certificate
3. Points to your router's public IP

## Security Notes

- API token is stored as Kubernetes secret
- Traefik dashboard is exposed at `traefik.epaflix.com` - consider adding authentication
- All HTTP traffic is redirected to HTTPS via middleware
- Security headers middleware is available in `middleware/`
- Adjust the configurations in `values/traefik-values.yaml` as needed to fit your specific requirements.

## Additional Information

For more details on Traefik and its configuration options, refer to the [Traefik documentation](https://doc.traefik.io/traefik/).

This README serves as a guide to set up and manage the Traefik reverse proxy for the Epaflix project.