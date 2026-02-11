# Quick Deployment Guide

## One-Command Deployment

```bash
./deploy.sh
```

This will:
1. Create the `traefik-system` namespace
2. Create the Cloudflare API token secret
3. Deploy Traefik with LoadBalancer on `192.168.10.101`
4. Apply middleware (HTTPS redirect + security headers)
5. Deploy whoami test app

## Manual Step-by-Step

```bash
# 1. Create namespace
kubectl apply -f namespace.yaml

# 2. Create Cloudflare secret
./certificates/create-cloudflare-secret.sh

# 3. Deploy Traefik
helmfile sync

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
helmfile destroy

# Remove middleware
kubectl delete -f middleware/

# Remove secret
kubectl -n traefik-system delete secret cloudflare-api-token

# Remove namespace
kubectl delete namespace traefik-system
```

## Configuration Files Summary

- **namespace.yaml**: Creates `traefik-system` namespace
- **helmfile.yaml**: Helm release configuration
- **values/traefik-values.yaml**: Traefik settings (DNS challenge, LoadBalancer IP, etc.)
- **middleware/redirect-https.yaml**: HTTP → HTTPS redirect
- **middleware/security-headers.yaml**: Security headers for all responses
- **examples/whoami-demo.yaml**: Test application with IngressRoute
- **certificates/create-cloudflare-secret.sh**: Helper script for secret creation
- **deploy.sh**: Full automated deployment

## Key Configuration Details

- **Cloudflare Account ID**: `<CLOUDFLARE_ACCOUNT_ID>`
- **API Token**: Stored in `cloudflare-api-token` secret
- **Traefik IP**: `192.168.10.101`
- **Certificate Resolver**: `cloudflare` (DNS-01 challenge)
- **Domain**: `*.epaflix.com` (wildcard support)
- **ACME Email**: `admin@epaflix.com`
