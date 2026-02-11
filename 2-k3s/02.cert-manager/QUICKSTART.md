# cert-manager Quick Start

## Install

```bash
cd /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/02.cert-manager
./01.install-cert-manager.sh
```

Installs cert-manager via Helm and creates the `letsencrypt-dns` and `selfsigned-issuer` ClusterIssuers.

## Verify

```bash
kubectl get pods -n cert-manager
kubectl get clusterissuer
kubectl get certificate -A
```

## How TLS Works

Most services get certificates **automatically via Traefik's ACME resolver** (`certResolver: cloudflare`). No cert-manager Certificate resource needed — just set `tls.certResolver: cloudflare` in the IngressRoute.

For services that need a cert-manager-managed Certificate:

```bash
# Check issued certificates
kubectl get certificate -A

# Verify live TLS
curl -I https://sonarr.epaflix.com
```

## Troubleshooting

```bash
# cert-manager logs
kubectl logs -n cert-manager -l app=cert-manager

# Certificate not issuing
kubectl describe certificate <name> -n <namespace>
kubectl get certificaterequest -A

# Verify cert issuer on a live service
echo | openssl s_client -connect 192.168.10.101:443 -servername sonarr.epaflix.com 2>/dev/null | \
  openssl x509 -noout -issuer -dates
```

## More Information

See [README.md](README.md) for full documentation.
