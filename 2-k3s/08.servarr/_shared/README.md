# Shared Servarr Resources

Resources shared across multiple Servarr applications.

## Directories

### ingress/
Traefik IngressRoute configurations:
- `internal-routes.yaml` - Internal network access routes
- `public-routes.yaml` - Public-facing routes (Jellyfin, Jellyseerr, Wizarr)

### middleware/
Traefik middleware configurations:
- `arr-headers.yaml` - Security headers for *arr apps

### scripts/
Management scripts:
- `01-setup-postgres.sh` - Initialize PostgreSQL databases for apps
- `auto-restore.sh` - Automated restore from backups
- `deploy.sh` - Deploy all apps
- `restore-from-backup.sh` - Manual restore helper
- `check-database-health.sh` - DB health checks
- `check-all-databases-health.sh` - Run health checks across all apps
- `fix-all-history-schemas.sh` - Fix missing History table columns

### secrets/
Kubernetes secrets:
- `postgres-secret.yaml` - PostgreSQL connection credentials
- `wireguard-secret.yaml` - WireGuard VPN configuration

### storage/
Persistent volume and claim definitions:
- `arr-configs.yaml` - Config storage for Sonarr/Radarr/Prowlarr
- `media-app-configs.yaml` - Config storage for media apps
- `media-pvcs.yaml` - Media library PVCs
- `utility-configs.yaml` - Config storage for utilities
- Host-path PV/PVC definitions for local storage
