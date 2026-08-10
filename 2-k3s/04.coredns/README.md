# CoreDNS Custom Configuration

## Overview

This directory contains the CoreDNS custom configuration for the K3s cluster.

- **External Domain Resolution** (`coredns-epaflix-domains.yaml`) — `coredns-custom` ConfigMap with an `epaflix.com:53` server block so pods can resolve `https://<svc>.epaflix.com` (e.g. `https://sonarr.epaflix.com`).
- **GitOps**: managed by the ArgoCD `coredns` Application (`2-k3s/11.argocd/apps/app-coredns.yaml`, scope: `coredns-custom` ConfigMap only). The k3s-addon-owned main `coredns` ConfigMap is **not** under ArgoCD — k3s's helm/addon controller owns it.
- **One-shot bootstrap** (`configure-dns.sh`) — systemd-resolved listener config on each node; runs once per new node, not GitOps-managed.
- **Disruption budget** (`coredns-pdb.yaml`) — `minAvailable: 1` so a node drain can never take DNS to zero. GitOps-managed; new object, no addon conflict.

## Replica count (HA)

CoreDNS runs **2 replicas**. One is not enough: the `k3s-server` / `k3s-agent`
system-upgrade Plans drain every node in the fleet, and a single CoreDNS pod
means cluster-wide DNS goes dark for as long as it takes to reschedule.

**The replica count is set live, not in git. That is deliberate.**

- `spec.replicas` is **not** in k3s's bundled `/var/lib/rancher/k3s/server/manifests/coredns.yaml`. The file has no `replicas:` key at all - Kubernetes just defaults it to 1 on create.
- The k3s deploy controller applies that manifest through a wrangler three-way merge. A field that appears in neither the desired manifest nor the recorded `objectset.rio.cattle.io/applied` annotation is **left alone**. So a live `replicas: 2` survives an addon re-apply, a `systemctl restart k3s`, and a k3s version upgrade.
- Verified on 2026-08-03: forcing a full addon re-apply (change the manifest checksum, watch `addon/coredns` `spec.checksum` update) left `spec.replicas=2` and `readyReplicas=2` untouched.
- It is **not** an ArgoCD-managed field because `spec.replicas` is already owned by the `deploy@k3s-master-51` and `k3s` field managers. An ArgoCD server-side apply hits a hard conflict:

  ```
  error: Apply failed with 1 conflict: conflict with "deploy@k3s-master-51" using apps/v1: .spec.replicas
  ```

  Forcing that conflict would put the whole `coredns` Application - including the critical `coredns-custom` ConfigMap - into a permanent fight with the k3s addon controller. Not worth it for an integer.

**Anti-affinity is already handled by k3s.** The bundled Deployment carries:

```yaml
topologySpreadConstraints:
  - topologyKey: kubernetes.io/hostname
    maxSkew: 1
    whenUnsatisfiable: DoNotSchedule
    labelSelector: {matchLabels: {k8s-app: kube-dns}}
```

`maxSkew: 1` + `DoNotSchedule` on `kubernetes.io/hostname` is a hard guarantee
that two CoreDNS pods cannot land on the same node. Nothing to add.

**To re-assert after a cluster rebuild** (the only case where this is lost):

```bash
kubectl --context epaflix scale deployment/coredns -n kube-system --replicas=2
kubectl --context epaflix -n kube-system get pods -l k8s-app=kube-dns -o wide   # expect 2, different nodes
```

**Known gap:** the second `topologySpreadConstraint` spreads on
`topology.kubernetes.io/zone`, but no node carries that label, so it is a no-op.
Labelling nodes by Proxmox host (`takaros` / `evanthoulaki`) would activate
host-level spreading for free - it is `whenUnsatisfiable: ScheduleAnyway`, so a
soft preference. Not done here: it changes scheduling inputs for every workload
with a zone constraint, which is too wide a blast radius to land next to a fleet
upgrade.

## DNS Configuration Fix (Required)

**Problem:** Pods cannot resolve DNS queries because the DNS server at 192.168.10.30 (Pi-hole) only accepts queries from the 192.168.10.0/24 network, not from the pod network (10.42.x.x).

**Solution:** Configure systemd-resolved on all K3s nodes to listen on the node IPs, then forward CoreDNS queries to the nodes instead of directly to the DNS server.

## Using External Domains from Inside Pods

### What This Enables

Pods can now use external domain names to communicate with other services via HTTPS, instead of using internal Kubernetes service names via HTTP:

**Before:**
```bash
# Inside a pod (e.g., seerr)
curl http://sonarr:8989
curl http://radarr:7878
```

**After:**
```bash
# Inside a pod - now works with HTTPS and proper certificates!
curl https://sonarr.epaflix.com
curl https://radarr.epaflix.com
curl https://auth.epaflix.com
```

### Benefits

- ✅ **HTTPS with valid certificates** - Secure communication between pods
- ✅ **Consistent URLs** - Same URLs work inside and outside the cluster
- ✅ **Simplified configuration** - Applications can use the same API URLs
- ✅ **Better compatibility** - Some applications expect HTTPS URLs
- ✅ **SSO integration** - Services can use Authentik middleware via ingress

### How It Works

1. CoreDNS intercepts DNS queries for `*.epaflix.com`
2. Forwards these queries to Pi-hole (192.168.10.30) via node IPs
3. Pi-hole returns Traefik's LoadBalancer IP (192.168.10.101)
4. Pod connects to Traefik via HTTPS
5. Traefik routes based on Host header to the appropriate service
6. Request goes through ingress middleware (auth, headers, etc.)

### Installation (GitOps)

Managed by the `coredns` ArgoCD Application — `2-k3s/11.argocd/apps/app-coredns.yaml`. Bootstrap once per cluster:

```bash
kubectl --context epaflix apply -f 2-k3s/11.argocd/apps/app-coredns.yaml
argocd app sync coredns --core --prune=false
```

After the first sync, edits to `coredns-epaflix-domains.yaml` reconcile via Argo. CoreDNS hot-reloads the `coredns-custom` ConfigMap from `/etc/coredns/custom/`; no Deployment rollout is required (a `kubectl --context epaflix rollout restart deployment/coredns -n kube-system` is only needed if a reload misfires).

### Verification

```bash
# Test DNS resolution
kubectl --context epaflix run test-dns --image=busybox:latest --restart=Never --rm -it -- \
  nslookup sonarr.epaflix.com
# Should return: 192.168.10.101

# Test HTTPS connectivity
kubectl --context epaflix run test-curl --image=curlimages/curl:latest --restart=Never --rm -it -- \
  curl -I https://sonarr.epaflix.com
# Should return HTTP 200/401 (depending on auth requirements)
```

### Available Domains

All the following domains now work from inside pods:

**Public Services** (`*.epaflix.com`, resolved by Pi-hole - almost all to
`192.168.10.101`, the exceptions are noted inline):
- sonarr.epaflix.com
- sonarr2.epaflix.com
- radarr.epaflix.com
- prowlarr.epaflix.com
- bazarr.epaflix.com
- qbittorrent.epaflix.com (`192.168.10.102`, Traefik `internal` entry point)
- homarr.epaflix.com
- newtarr.epaflix.com
- cleanuparr.epaflix.com
- jellyfin.epaflix.com
- seerr.epaflix.com
- jellyseerr.epaflix.com
- auth.epaflix.com
- traefik.epaflix.com
- grafana.epaflix.com

Check Pi-hole's `/etc/dnsmasq.d/10-epaflix.conf` for the authoritative list.

## DNS Configuration Steps

### 1. Configure systemd-resolved on All Nodes

Configure systemd-resolved to listen on node IPs (required for pods to query DNS via nodes):

```bash
# On all K3s nodes (masters and workers)
for ip in 51 52 53 61 62 63 65; do
  echo "=== Configuring DNS on 192.168.10.$ip ==="
  ssh ubuntu@192.168.10.$ip "sudo mkdir -p /etc/systemd/resolved.conf.d/ && sudo tee /etc/systemd/resolved.conf.d/listen.conf > /dev/null << 'EOL'
[Resolve]
DNSStubListenerExtra=192.168.10.$ip
DNSStubListenerExtra=10.0.0.$ip
EOL
sudo systemctl restart systemd-resolved"
done
```

Verify systemd-resolved is listening on node IPs:
```bash
ssh ubuntu@192.168.10.51 "sudo ss -tulpn | grep 53"
# Should show systemd-resolved listening on 192.168.10.51:53 and 10.0.0.51:53
```

### 2. Update CoreDNS Main Configuration

Update CoreDNS to forward queries to node IPs instead of directly to DNS server:

```bash
kubectl --context epaflix patch configmap coredns -n kube-system --type=json -p='[{"op":"replace","path":"/data/Corefile","value":".:53 {\n    errors\n    health\n    ready\n    kubernetes cluster.local in-addr.arpa ip6.arpa {\n      pods insecure\n      fallthrough in-addr.arpa ip6.arpa\n    }\n    hosts /etc/coredns/NodeHosts {\n      ttl 60\n      reload 15s\n      fallthrough\n    }\n    prometheus :9153\n    cache 30\n    loop\n    reload\n    loadbalance\n    import /etc/coredns/custom/*.override\n    forward . 192.168.10.51 192.168.10.52 192.168.10.53\n}\nimport /etc/coredns/custom/*.server\n"}]'
```

### 3. Update Custom epaflix Domains Configuration

`coredns-custom` is managed by the ArgoCD `coredns` Application — edit `coredns-epaflix-domains.yaml`, commit, and let Argo reconcile (or `argocd app sync coredns --core`). Avoid `kubectl --context epaflix patch configmap coredns-custom` directly; the next Argo sync would revert it.

### 4. Restart CoreDNS

```bash
kubectl --context epaflix rollout restart deployment/coredns -n kube-system
kubectl --context epaflix rollout status deployment/coredns -n kube-system
```

### 5. Verification

Test DNS resolution from pods:
```bash
# Test public domain
kubectl --context epaflix run test-dns --image=busybox --restart=Never --rm -it -- \
  nslookup auth.epaflix.com
# Should return: 192.168.10.101

# Test another domain
kubectl --context epaflix run test-dns2 --image=busybox --restart=Never --rm -it -- \
  nslookup sonarr.epaflix.com
# Should return: 192.168.10.101
```

## Alternative: Fix Search Domain at Source

Instead of custom CoreDNS configs, you can fix DNS resolution issues by removing search domains from nodes:

```bash
# On each K3s node, edit /etc/netplan/50-cloud-init.yaml
# Remove the "search:" lines under nameservers
# Then run:
sudo netplan apply
```

This prevents pods from inheriting problematic search domains that cause external DNS resolution issues.

## How K3s CoreDNS Custom Config Works

K3s CoreDNS supports custom configuration via the `coredns-custom` ConfigMap:
- `*.override` files: Imported at the end of the main server block
- `*.server` files: Added as new server blocks

The ConfigMap is automatically loaded by k3s CoreDNS from `/etc/coredns/custom/`.

## Troubleshooting

### DNS queries not resolving

```bash
# Check CoreDNS logs
kubectl --context epaflix logs -n kube-system -l k8s-app=kube-dns --tail=50

# Verify custom config is loaded
kubectl --context epaflix get configmap coredns-custom -n kube-system -o yaml

# Check main CoreDNS config
kubectl --context epaflix get configmap coredns -n kube-system -o yaml | grep -A 20 "Corefile:"

# Verify systemd-resolved is listening on node IPs
ssh ubuntu@192.168.10.51 "sudo ss -tulpn | grep 53"

# Test from a specific namespace
kubectl --context epaflix run test-dns -n servarr --image=busybox:latest --restart=Never --rm -it -- \
  nslookup sonarr.epaflix.com
```

### DNS queries getting REFUSED

This happens when the DNS server (192.168.10.30) refuses queries from the pod network. Solution:
1. Configure systemd-resolved to listen on node IPs (see DNS Configuration Steps above)
2. Update CoreDNS to forward to node IPs instead of directly to the DNS server

### HTTPS connection fails

```bash
# Check if Traefik is running
kubectl --context epaflix get svc -n traefik-system traefik

# Verify ingress routes
kubectl --context epaflix get ingressroute -A

# Check certificates
kubectl --context epaflix get certificate -A
```
