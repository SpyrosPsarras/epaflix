# Quick Deployment Guide

## One-Command Deployment

```bash
./01.deploy.sh
```

Use this for initial bootstrap before ArgoCD exists, or emergency manual
recovery. This will:
1. Create the `traefik-system` namespace
2. Create the Cloudflare API token secret
3. Deploy Traefik with LoadBalancer on `192.168.10.101`
4. Apply middleware (HTTPS redirect + security headers)
5. Deploy whoami test app

## Preferred ArgoCD Adoption

For an already-running cluster with ArgoCD installed, use the Traefik
Application instead of re-running `./01.deploy.sh`:

```bash
kubectl kustomize --enable-helm 2-k3s/05.traefik-deployment >/tmp/traefik-rendered.yaml
kubectl -n traefik-system get secret cloudflare-api-token
kubectl -n traefik-system get pvc
kubectl -n traefik-system get svc traefik -o wide
kubectl apply -f 2-k3s/11.argocd/apps/app-traefik.yaml
argocd app diff traefik
argocd app sync traefik
```

Review the first diff before syncing. It must preserve `192.168.10.101`, one
Traefik replica, existing ACME storage at `/data/acme.json`, and the
`cloudflare-api-token` Secret reference. Leave prune disabled during adoption.

## Manual Step-by-Step

```bash
# 1. Create namespace
kubectl apply -f namespace.yaml

# 2. Create Cloudflare secret
./certificates/create-cloudflare-secret.sh

# 3. Deploy Traefik
helm repo add traefik https://traefik.github.io/charts
helm repo update
helm upgrade --install traefik traefik/traefik \
  -n traefik-system \
  -f values/traefik-values.yaml \
  --wait

# 4. Apply middleware
kubectl apply -f middleware/

# 5. Deploy test app
kubectl apply -f examples/whoami-demo.yaml
```

## Verification Commands

```bash
# Check Traefik pods
kubectl -n traefik-system get pods

# Check LoadBalancer IP
kubectl -n traefik-system get svc traefik

# Check certificate generation logs
kubectl -n traefik-system logs -l app.kubernetes.io/name=traefik | grep -i acme

# Test whoami service
kubectl -n whoami-test get pods,svc,ingressroute

# View certificates
kubectl -n traefik-system exec -it deployment/traefik -- cat /data/acme.json
```

## Testing Access

```bash
# From LAN
curl https://whoami.epaflix.com
curl https://traefik.epaflix.com/dashboard/

# Check certificate
openssl s_client -connect whoami.epaflix.com:443 -servername whoami.epaflix.com
```

## Router & DNS Setup

### Router Port Forwarding
```
TCP 80  → 192.168.10.101:80
TCP 443 → 192.168.10.101:443
```

### Pi-hole DNS
```
Local Record:
*.epaflix.com → 192.168.10.101
```

## Troubleshooting

### Certificate not issued after 5 minutes
```bash
# Check Traefik logs for errors
kubectl -n traefik-system logs -l app.kubernetes.io/name=traefik --tail=100

# Verify Cloudflare token
kubectl -n traefik-system get secret cloudflare-api-token -o jsonpath='{.data.api-token}' | base64 -d
```

### LoadBalancer stuck in Pending
```bash
# Check kube-vip cloud provider
kubectl -n kube-system get configmap kubevip -o yaml
kubectl -n kube-system logs -l component=kube-vip-cloud-provider
```

### DNS not resolving
```bash
# Test DNS from cluster node
dig whoami.epaflix.com
nslookup whoami.epaflix.com

# Test from Pi-hole
ssh <pihole-ip> "dig whoami.epaflix.com"
```

## Clean Up (Uninstall)

```bash
# Remove test app
kubectl delete -f examples/whoami-demo.yaml

# Remove Traefik
helm -n traefik-system uninstall traefik

# Remove middleware
kubectl delete -f middleware/

# Remove secret
kubectl -n traefik-system delete secret cloudflare-api-token

# Remove namespace
kubectl delete namespace traefik-system
```

## Configuration Files Summary

- **namespace.yaml**: Creates `traefik-system` namespace
- **kustomization.yaml**: ArgoCD entrypoint; renders the Traefik Helm chart and static Traefik resources
- **values/traefik-values.yaml**: Traefik settings (DNS challenge, LoadBalancer IP, etc.)
- **middleware/redirect-https.yaml**: HTTP → HTTPS redirect
- **middleware/security-headers.yaml**: Security headers for all responses
- **examples/whoami-demo.yaml**: Test application with IngressRoute
- **certificates/create-cloudflare-secret.sh**: Helper script for secret creation
- **01.deploy.sh**: Bootstrap/manual automated deployment

## Key Configuration Details

- **Cloudflare Account ID**: `<CLOUDFLARE_ACCOUNT_ID>`
- **API Token**: Stored in `cloudflare-api-token` secret
- **Traefik IP**: `192.168.10.101`
- **Certificate Resolver**: `cloudflare` (DNS-01 challenge)
- **Domain**: `*.epaflix.com` (wildcard support)
- **ACME Email**: `admin@epaflix.com`
