# K3s Embedded Registry Mirror - Implementation Summary

## Date
February 3, 2026

## Overview
Enabled K3s Embedded Registry Mirror (Spegel) for peer-to-peer container image sharing across all cluster nodes, eliminating image duplication and reducing internet bandwidth usage.

## What is Spegel?
Spegel is a stateless distributed OCI registry mirror built into K3s that allows nodes to share container images peer-to-peer. When one node pulls an image from the internet, all other nodes can get it directly from that node instead of re-downloading.

## Benefits
- ✅ **Zero image duplication** - images pulled once are available to all nodes
- ✅ **Faster deployments** - nodes pull images from local cluster instead of internet
- ✅ **Reduced bandwidth** - only one node downloads from upstream registry
- ✅ **No infrastructure overhead** - built into k3s, no separate registry needed
- ✅ **Automatic** - no manual pre-pulling or cache management required

## Network Configuration
- **P2P Network**: 10.0.0.0/24 (eth1 interface) - 2.5G dedicated network on HPE servers
- **P2P Port**: TCP 5001 (Distributed Hash Table communication)
- **Registry API**: TCP 6443 (same as Kubernetes API server)
- All 7 nodes can reach each other on 10.0.0.0/24 network

## Implementation Steps

### 1. Created registries.yaml
```yaml
# /etc/rancher/k3s/registries.yaml (on all 7 nodes)
mirrors:
  "*":
    # Wildcard enables embedded mirror for ALL registries
```

**Why wildcard?** Mirrors all registries (docker.io, ghcr.io, quay.io, lscr.io, etc.) without needing to list each one individually.

### 2. Updated Master Node Configuration
Added `--embedded-registry` flag to `/etc/systemd/system/k3s.service` on all 3 master nodes:

```systemd
ExecStart=/usr/local/bin/k3s \
    server \
    ... (existing flags) ...
    '--tls-san' \
    '10.0.0.53' \
    '--embedded-registry'
```

### 3. Rolling Restart (Minimal Downtime)
- **Masters**: Restarted one at a time (51 → 52 → 53) with 10s stability waits
- **Workers**: Restarted one at a time (61 → 62 → 63 → 65) with cordoning/uncordoning
- Total downtime per node: ~30 seconds
- No application interruptions due to multi-replica deployments

### 4. Verification
Confirmed Spegel is running:
```bash
# Check P2P annotations on nodes
kubectl get node k3s-master-51 -o jsonpath='{.metadata.annotations.p2p\.k3s\.cattle\.io/node-address}'
# Output: /ip4/10.0.0.51/tcp/5001/p2p/QmREESKcjLdxG2egB8GUKAry4hdBdEG1xwoVVz4Rxaec2k

# Check logs for Spegel startup
ssh ubuntu@192.168.10.51 'sudo journalctl -u k3s --since "5 minutes ago" | grep spegel'
# Output: starting p2p router, running state update
```

### 5. Removed Pre-pull CronJob
Deleted `image-prepull-weekly` CronJob from kube-system namespace since Spegel handles image distribution automatically.

**Kept**: `containerd-cleanup` CronJob (Sunday 2 AM) - still needed to remove unused images/containers.

## Configuration Files

### Master Node Service Example
`/etc/systemd/system/k3s.service` on masters:
```systemd
ExecStart=/usr/local/bin/k3s \
    server \
    '--cluster-init' \
    '--tls-san' \
    '192.168.10.51' \
    '--disable' \
    'servicelb' \
    '--disable' \
    'traefik' \
    '--node-ip' \
    '10.0.0.51' \
    '--advertise-address' \
    '10.0.0.51' \
    '--flannel-iface' \
    'eth1' \
    '--node-taint' \
    'node-role.kubernetes.io/control-plane:NoSchedule' \
    '--write-kubeconfig-mode=644' \
    '--tls-san' \
    '192.168.10.100' \
    '--tls-san' \
    '192.168.10.51' \
    '--tls-san' \
    '192.168.10.52' \
    '--tls-san' \
    '192.168.10.53' \
    '--tls-san' \
    '10.0.0.51' \
    '--tls-san' \
    '10.0.0.52' \
    '--tls-san' \
    '10.0.0.53' \
    '--embedded-registry'
```

### Registries Configuration
`/etc/rancher/k3s/registries.yaml` on all nodes:
```yaml
# K3s Embedded Registry Mirror Configuration
# Enables peer-to-peer image sharing across all cluster nodes
# Using wildcard to mirror ALL registries

mirrors:
  "*":
    # Empty endpoint list enables embedded mirror without external mirrors
```

## How It Works

### Image Pull Flow
1. Pod requests image `docker.io/library/nginx:alpine`
2. Containerd checks local image store (not found)
3. Containerd queries Spegel DHT on port 5001 (finds image on node worker-62)
4. Containerd pulls from `https://10.0.0.62:6443/v2/...` (P2P transfer)
5. If not found on any node, falls back to `docker.io` (internet)

### P2P Network
- Uses libp2p for distributed hash table (DHT)
- Each node advertises available images
- Nodes discover each other via K8s node annotations
- Image transfers use TLS with cluster CA certificates

## Testing Image Sharing

### Test Procedure
```bash
# On worker-61: Pull an image
ssh ubuntu@192.168.10.61 'sudo crictl pull nginx:alpine'

# On worker-62: Pull same image (should be faster, from peer)
ssh ubuntu@192.168.10.62 'time sudo crictl pull nginx:alpine'

# Verify it came from peer (check containerd logs)
ssh ubuntu@192.168.10.62 'sudo journalctl -u k3s-agent --since "1 minute ago" | grep nginx'
```

### Expected Behavior
- First pull: Downloads from docker.io (~10 seconds)
- Subsequent pulls on other nodes: P2P transfer (~1-2 seconds for small images)

## Security

### Authentication
- **Registry API**: Requires client certificate signed by cluster CA
- **P2P Network**: Requires preshared key + cluster CA certificate
- All traffic is TLS encrypted

### Trust Model
- ⚠️ **All cluster nodes have equal privilege**
- Nodes trust images advertised by peers without upstream verification
- For critical deployments: Use image digests instead of tags (e.g., `nginx@sha256:abc123...`)

## Monitoring

### Check Spegel Status
```bash
# View P2P addresses on all nodes
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.annotations.p2p\.k3s\.cattle\.io/node-address}{"\n"}{end}'

# Check Spegel logs on master
ssh ubuntu@192.168.10.51 'sudo journalctl -u k3s | grep spegel | tail -20'
```

### Verify Image Sharing
```bash
# List images on each node
for node in 61 62 63 65; do
  echo "=== Worker-$node ==="
  ssh ubuntu@192.168.10.$node 'sudo crictl images | wc -l'
done
```

## Troubleshooting

### Spegel Not Starting
- Check if `--embedded-registry` flag is in k3s process: `ps aux | grep "k3s server"`
- Verify registries.yaml exists: `ls -la /etc/rancher/k3s/registries.yaml`
- Check k3s logs: `journalctl -u k3s | grep spegel`

### Images Not Shared Between Nodes
- Verify P2P port 5001 is reachable: `nc -zv 10.0.0.51 5001`
- Check node annotations: `kubectl get node <node> -o yaml | grep p2p`
- Ensure registry is in mirrors list (wildcard `"*"` covers all)

### Performance Issues
- P2P transfers should be fast (~1-2s for small images on 2.5G network)
- If slow, check network: `iperf3 -c 10.0.0.51` (should show ~2.5 Gbps)
- Monitor containerd logs: `journalctl -u k3s-agent | grep -i pull`

## Changes to Existing Infrastructure

### Removed
- ❌ `image-prepull-weekly` CronJob (no longer needed)
- ❌ Manual image pre-pulling scripts

### Kept
- ✅ `containerd-cleanup` CronJob (still needed for garbage collection)

### Updated Documentation
- ✅ [K3s README](../README.md) - Cluster overview documentation
- ✅ [manifests/maintenance/README.md](README.md) - Replaced pre-pull section with Spegel documentation

## References
- K3s Embedded Registry Mirror Docs: https://docs.k3s.io/installation/registry-mirror
- Spegel Project: https://github.com/spegel-org/spegel
- K3s Version: v1.34.3+k3s1 (GA support for embedded registry)

## Next Steps (Optional)
- Monitor image pull times over next week to verify P2P sharing is working
- Consider configuring `K3S_P2P_ENABLE_LATEST=true` if you frequently use `:latest` tags (not recommended for production)
- Review security implications if cluster has multi-tenant workloads with different trust levels
