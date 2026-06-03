# Seerr — Quick Reference

`seerr` is the single, canonical media-request deployment in the servarr stack.
It is reconciled by **ArgoCD** via `2-k3s/08.servarr/kustomization.yaml` — there
is nothing to deploy by hand and there is no migration to run.

> This was a **name consolidation**: the redundant `jellyseerr` Deployment was
> retired. `seerr` runs the `fallenbagel/jellyseerr:preview-OIDC` fork image
> (UID 568) on the existing `jellyseerr-config` PVC and `jellyseerr` Postgres
> DB — same image, same data. NOT an image migration.

## Make a change

Edit the manifests in git and let ArgoCD reconcile (selfHeal is on — manual
`kubectl edit` against managed resources gets reverted):

- Deployment / Service: `seerr/seerr.yaml`
- PodDisruptionBudget: `seerr/pdb.yaml`
- Ingress (both hosts → `seerr` Service): `../_shared/ingress/public-routes.yaml`

## Verify

```bash
# Pod status
kubectl get pods -n servarr -l app=seerr

# Service + endpoints
kubectl get svc seerr -n servarr
kubectl get endpoints seerr -n servarr

# Logs
kubectl logs -n servarr -l app=seerr -f
```

## Access

- **Internal**: http://seerr:5055
- **External**: https://seerr.epaflix.com
- **Legacy** (also resolves to seerr): https://jellyseerr.epaflix.com

## Data-safety

⚠️ **DO NOT** rename or delete the PVC `jellyseerr-config`, the `jellyseerr`
Postgres DB/user, or the `jellyseerr-*` keys in the `servarr-postgres` Secret.
They are legacy names reused by `seerr`; renaming forces a data migration.

## Backup

```bash
cd ../jellyseerr
./backup-jellyseerr-db.sh
```

## Troubleshooting

```bash
# Pod not starting?
kubectl describe pod -n servarr -l app=seerr
kubectl logs -n servarr -l app=seerr --tail=100

# Config permission issues (pod runs as UID 568)?
kubectl get pvc jellyseerr-config -n servarr
kubectl describe pvc jellyseerr-config -n servarr

# Database connection issues?
kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.jellyseerr-host}' | base64 -d
```

## See also

- [README.md](README.md) — full detail, rollback, and OIDC/Authentik setup
