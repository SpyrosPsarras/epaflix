# CoreDNS Custom Configuration

## Overview

This directory contains CoreDNS custom configurations for the K3s cluster:

1. **External Domain Resolution** (`coredns-epaflix-domains.yaml`) - Allows pods to access services using external domain names (e.g., `https://sonarr.epaflix.com`)
2. **Search Domain Fix** (`coredns-custom.yaml`) - DEPRECATED: Fixes search domain issues (see Alternative section below)

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

### Installation

```bash
# Apply the configuration
kubectl apply -f coredns-epaflix-domains.yaml

# Restart CoreDNS to load the new configuration
kubectl rollout restart deployment/coredns -n kube-system
kubectl rollout status deployment/coredns -n kube-system
```

### Verification

```bash
# Test DNS resolution
kubectl run test-dns --image=busybox:latest --restart=Never --rm -it -- \
  nslookup sonarr.epaflix.com
# Should return: 192.168.10.101

# Test HTTPS connectivity
kubectl run test-curl --image=curlimages/curl:latest --restart=Never --rm -it -- \
  curl -I https://sonarr.epaflix.com
# Should return HTTP 200/401 (depending on auth requirements)
```

### Available Domains

All the following domains now work from inside pods:

**Public Services** (*.epaflix.com — all resolve to 192.168.10.101 via Pi-hole):
- sonarr.epaflix.com
- sonarr2.epaflix.com
- radarr.epaflix.com
- prowlarr.epaflix.com
- bazarr.epaflix.com
- qbittorrent.epaflix.com
- homarr.epaflix.com
- huntarr.epaflix.com
- cleanuparr.epaflix.com
- jellyfin.epaflix.com
- seerr.epaflix.com
- jellyseerr.epaflix.com
- auth.epaflix.com
- traefik.epaflix.com
- grafana.epaflix.com
- filebrowser.epaflix.com

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
kubectl patch configmap coredns -n kube-system --type=json -p='[{"op":"replace","path":"/data/Corefile","value":".:53 {\n    errors\n    health\n    ready\n    kubernetes cluster.local in-addr.arpa ip6.arpa {\n      pods insecure\n      fallthrough in-addr.arpa ip6.arpa\n    }\n    hosts /etc/coredns/NodeHosts {\n      ttl 60\n      reload 15s\n      fallthrough\n    }\n    prometheus :9153\n    cache 30\n    loop\n    reload\n    loadbalance\n    import /etc/coredns/custom/*.override\n    forward . 192.168.10.51 192.168.10.52 192.168.10.53\n}\nimport /etc/coredns/custom/*.server\n"}]'
```

### 3. Update Custom epaflix Domains Configuration

Update the custom ConfigMap to also forward to node IPs:

```bash
kubectl patch configmap coredns-custom -n kube-system --type=json -p='[{"op":"replace","path":"/data/epaflix.server","value":"epaflix.com:53 {\n    errors\n    cache 30\n    forward . 192.168.10.51 192.168.10.52 192.168.10.53\n    log\n}\n"}]'
```

### 4. Restart CoreDNS

```bash
kubectl rollout restart deployment/coredns -n kube-system
kubectl rollout status deployment/coredns -n kube-system
```

### 5. Verification

Test DNS resolution from pods:
```bash
# Test public domain
kubectl run test-dns --image=busybox --restart=Never --rm -it -- \
  nslookup auth.epaflix.com
# Should return: 192.168.10.101

# Test another domain
kubectl run test-dns2 --image=busybox --restart=Never --rm -it -- \
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
kubectl logs -n kube-system -l k8s-app=kube-dns --tail=50

# Verify custom config is loaded
kubectl get configmap coredns-custom -n kube-system -o yaml

# Check main CoreDNS config
kubectl get configmap coredns -n kube-system -o yaml | grep -A 20 "Corefile:"

# Verify systemd-resolved is listening on node IPs
ssh ubuntu@192.168.10.51 "sudo ss -tulpn | grep 53"

# Test from a specific namespace
kubectl run test-dns -n servarr --image=busybox:latest --restart=Never --rm -it -- \
  nslookup sonarr.epaflix.com
```

### DNS queries getting REFUSED

This happens when the DNS server (192.168.10.30) refuses queries from the pod network. Solution:
1. Configure systemd-resolved to listen on node IPs (see DNS Configuration Steps above)
2. Update CoreDNS to forward to node IPs instead of directly to the DNS server

### HTTPS connection fails

```bash
# Check if Traefik is running
kubectl get svc -n traefik-system traefik

# Verify ingress routes
kubectl get ingressroute -A

# Check certificates
kubectl get certificate -A
```
