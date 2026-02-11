# Jellyseerr to Seerr Migration - Quick Start

## Prerequisites
- kubectl access to the cluster
- Namespace: `servarr`
- Existing Jellyseerr deployment

## Quick Migration (Automated)

The easiest way to migrate is using the automated script:

```bash
cd /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/seerr
./migrate.sh
```

This script will:
1. ✅ Backup Jellyseerr database and config
2. ✅ Stop Jellyseerr safely
3. ✅ Deploy Seerr
4. ✅ Monitor automatic migration
5. ✅ Verify deployment
6. ✅ Optionally update ingress

**Total time**: ~5-10 minutes (depending on database size)

## Manual Migration (Step-by-Step)

If you prefer manual control:

### 1. Backup (REQUIRED!)
```bash
cd ../jellyseerr
./backup-jellyseerr-db.sh
```

### 2. Stop Jellyseerr
```bash
kubectl scale deployment jellyseerr -n servarr --replicas=0
kubectl wait --for=delete pod -l app=jellyseerr -n servarr --timeout=60s
```

### 3. Deploy Seerr
```bash
cd ../seerr
kubectl apply -f seerr.yaml
```

### 4. Monitor Migration
```bash
kubectl logs -n servarr -l app=seerr -f
```

Wait for "Server ready" or similar message.

### 5. Deploy Ingress (Optional)
```bash
kubectl apply -f ingress.yaml
```

## Verify Migration

```bash
# Check pod status
kubectl get pods -n servarr -l app=seerr

# Check service
kubectl get svc seerr -n servarr

# Test API
kubectl run test-seerr --image=busybox --restart=Never --rm -it -n servarr -- \
  wget -O- http://seerr:5055/api/v1/status
```

## Access Seerr

- **Internal**: http://seerr:5055
- **External** (if ingress deployed): https://seerr.epaflix.com

## Rollback (If Needed)

```bash
kubectl scale deployment seerr -n servarr --replicas=0
kubectl scale deployment jellyseerr -n servarr --replicas=1
```

## Cleanup (After 24-48h verification)

```bash
kubectl delete deployment jellyseerr -n servarr
kubectl delete service jellyseerr -n servarr
# Optionally delete old ingress
kubectl delete ingress jellyseerr -n servarr
```

⚠️ **DO NOT** delete the PVC `jellyseerr-config` - it's being used by Seerr!

## Troubleshooting

### Pod not starting?
```bash
kubectl describe pod -n servarr -l app=seerr
kubectl logs -n servarr -l app=seerr --tail=100
```

### Permission errors?
The init container should fix permissions automatically. If issues persist:
```bash
kubectl get pvc jellyseerr-config -n servarr
kubectl describe pvc jellyseerr-config -n servarr
```

### Database connection issues?
```bash
kubectl get secret servarr-postgres -n servarr -o yaml
```

## Need Help?

- See [README.md](README.md) for detailed documentation
- [Seerr Documentation](https://docs.seerr.dev/)
- [Seerr Discord](https://discord.gg/seerr)
