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

## Prerequisites

1. Cloudflare API token with DNS edit permissions for `epaflix.com`
2. Router configured to forward TCP 80/443 to `192.168.10.101`
3. Pi-hole DNS records: `*.epaflix.com` → router public IP (or `192.168.10.101` for LAN)
4. **k3s DNS configuration** without search domains (see step 0 below)

## Deployment Steps

### 0. Configure k3s to use custom DNS resolv.conf (REQUIRED for Let's Encrypt)

This prevents pods from inheriting the host's DNS search domain, which would cause ACME DNS queries to fail.

**On all k3s master nodes (51, 52, 53):**

```bash
for ip in 192.168.10.51 192.168.10.52 192.168.10.53; do
  echo "Configuring k3s on $ip..."
  ssh ubuntu@$ip "sudo mkdir -p /etc/rancher/k3s && \
    echo 'kubelet-arg:
  - \"resolv-conf=/etc/k3s-resolv.conf\"' | sudo tee /etc/rancher/k3s/config.yaml && \
    echo 'nameserver 192.168.10.30' | sudo tee /etc/k3s-resolv.conf && \
    sudo systemctl restart k3s"
done
```

Wait for cluster to stabilize:
```bash
kubectl get nodes
```

### 1. Create namespace
```bash
kubectl apply -f namespace.yaml
```

### 2. Use the automated deployment script (Recommended)
```bash
./01.deploy.sh
```

This script will:
1. Create the namespace
2. Create the Cloudflare API token secret
3. Deploy Traefik with Helm (automatically creates local-path PVC)
4. Wait for LoadBalancer IP assignment
5. Apply middleware

### Manual deployment (Alternative)

### 3. Create Cloudflare API token secret
```bash
kubectl create secret generic cloudflare-api-token \
  --namespace=traefik-system \
  --from-literal=api-token=<CLOUDFLARE_API_TOKEN>
```

### 4. Deploy Traefik via Helmfile
```bash
helmfile sync
```

**OR using Helm directly:**
```bash
helm repo add traefik https://traefik.github.io/charts
helm repo update
helm install traefik traefik/traefik \
  -n traefik-system \
  -f values/traefik-values.yaml
```

### 5. Verify LoadBalancer IP
```bash
kubectl -n traefik-system get svc traefik
# Should show EXTERNAL-IP: 192.168.10.101
```

### 6. Verify NFS storage
```bash
kubectl -n traefik-system get pv,pvc
# Should show traefik-nfs-pv and traefik-acme-storage as Bound
```

### 7. Apply middleware
```bash
kubectl apply -f middleware/
```

### 8. Deploy test application (whoami)
```bash
kubectl apply -f examples/whoami-demo.yaml
```

### 9. Wait for certificate issuance (~2 minutes)
```bash
# Check Traefik logs
kubectl -n traefik-system logs -l app.kubernetes.io/name=traefik -f

# Verify both replicas are running
kubectl -n traefik-system get pods -o wide
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
kubectl -n traefik-system logs -l app.kubernetes.io/name=traefik | grep -i acme

# Verify Cloudflare token
kubectl -n traefik-system get secret cloudflare-api-token -o yaml
```

### LoadBalancer pending
```bash
# Check kube-vip cloud provider
kubectl -n kube-system get configmap kubevip -o yaml
kubectl -n kube-system logs -l app=kube-vip-cloud-provider
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