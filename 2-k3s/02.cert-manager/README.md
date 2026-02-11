# cert-manager Setup

cert-manager handles TLS certificate lifecycle for the cluster.

## Current State

All `*.epaflix.com` services use **Let's Encrypt** via Cloudflare DNS-01 challenge. Traefik's built-in ACME resolver (`certResolver: cloudflare`) handles most certificate issuance directly — cert-manager provides the `letsencrypt-dns` ClusterIssuer for services that need cert-manager-managed certificates.

> **Historical note:** This directory previously contained a self-signed CA setup for an internal `.epavli` domain. That domain no longer exists — all services migrated to `*.epaflix.com` with Let's Encrypt. The self-signed CA resources (`epavli-ca-issuer`, `epavli-ca-secret`, `epavli-tls`, `epavli-wildcard-cert`) were deleted in April 2026.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  TLS Certificate Sources                                    │
│                                                             │
│  1. Traefik ACME (primary)                                  │
│     certResolver: cloudflare                                │
│     → Automatic Let's Encrypt for all IngressRoutes         │
│     → Stored in Traefik's acme.json PVC                     │
│                                                             │
│  2. cert-manager (supplementary)                            │
│     ClusterIssuer: letsencrypt-dns                          │
│     → ACME DNS-01 via Cloudflare for epaflix.com zone       │
│     → Used by services needing cert-manager Certificates    │
│                                                             │
│  3. cert-manager selfsigned-issuer                          │
│     → Bootstrap issuer, available if needed                 │
└─────────────────────────────────────────────────────────────┘
```

## Deployed Resources

```bash
$ kubectl get clusterissuer
NAME                READY
letsencrypt-dns     True     # Let's Encrypt via Cloudflare DNS-01
selfsigned-issuer   True     # Bootstrap self-signed (available, rarely used)
```

## Installation

```bash
./01.install-cert-manager.sh
```

This installs cert-manager via Helm and creates the `letsencrypt-dns` and `selfsigned-issuer` ClusterIssuers.

### Verify

```bash
kubectl get pods -n cert-manager
kubectl get clusterissuer
kubectl get certificate -A
```

## How TLS Works for Services

### Most services: Traefik ACME (no cert-manager involvement)

IngressRoutes with `certResolver: cloudflare` get certificates directly from Traefik:

```yaml
apiVersion: traefik.io/v1alpha1
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
          port: 8080
  tls:
    certResolver: cloudflare
    domains:
      - main: epaflix.com
        sans:
          - "*.epaflix.com"
```

### Services needing cert-manager Certificates

For cases where a Kubernetes `Certificate` resource is needed (e.g., non-Traefik TLS termination):

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: myapp-tls
  namespace: myapp
spec:
  secretName: myapp-tls
  issuerRef:
    name: letsencrypt-dns
    kind: ClusterIssuer
  dnsNames:
    - myapp.epaflix.com
```

### HTTP to HTTPS Redirect

Traefik redirects all HTTP → HTTPS globally:

```yaml
# In traefik-values.yaml
ports:
  web:
    redirectTo:
      port: websecure
```

## Troubleshooting

### Check cert-manager health

```bash
kubectl get pods -n cert-manager
kubectl logs -n cert-manager -l app=cert-manager -f
```

### Certificate not issuing

```bash
kubectl get certificate -A
kubectl describe certificate <name> -n <namespace>
kubectl get certificaterequest -A
```

### Verify a live TLS cert

```bash
echo | openssl s_client -connect 192.168.10.101:443 -servername sonarr.epaflix.com 2>/dev/null | \
  openssl x509 -noout -issuer -dates
```

## Files

```
02.cert-manager/
├── README.md                       # This file
├── QUICKSTART.md                   # Quick install steps
├── 01.install-cert-manager.sh      # Installation script
└── issuers/
    └── self-signed-issuer.yaml     # ClusterIssuer definitions
```

## References

- [cert-manager Documentation](https://cert-manager.io/docs/)
- [Traefik TLS / ACME](https://doc.traefik.io/traefik/https/acme/)
- [Let's Encrypt DNS-01 Challenge](https://letsencrypt.org/docs/challenge-types/#dns-01-challenge)

## Related

- **Traefik deployment:** `../05.traefik-deployment/`
- **Cloudflare API token:** `.github/instructions/secrets.yml`
