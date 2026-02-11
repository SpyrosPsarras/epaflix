# Servarr Folder Reorganization - Complete

**Date:** January 25, 2026
**Status:** ✅ Complete

## What Changed

### Before
```
08.servarr/
├── apps/              # All 12 app deployments mixed together
├── backups/           # Mixed SQL dumps and ZIP files
├── ingress/           # Shared ingress routes
├── middleware/        # Shared middleware
├── scripts/           # Deployment scripts
├── secrets/           # Shared secrets
├── storage/           # Storage definitions
└── storage-hostpath/  # Host-path storage
```

### After
```
08.servarr/
├── _backups/
│   ├── postgres-dumps/        # .sql and .sql.gz files
│   └── sqlite-zips/           # Original app backup .zip files
├── _shared/
│   ├── ingress/              # Traefik IngressRoute configs
│   ├── middleware/           # Traefik middleware
│   ├── scripts/              # All deployment scripts
│   ├── secrets/              # PostgreSQL and VPN credentials
│   └── storage/              # All PV/PVC definitions
├── prowlarr/
│   └── prowlarr.yaml
├── radarr/
│   └── radarr.yaml
├── sonarr/
│   └── sonarr.yaml
├── sonarr2/
│   └── sonarr2.yaml
├── bazarr/
│   └── bazarr.yaml
├── qbittorrent/
│   └── qbittorrent.yaml
├── jellyfin/
│   └── jellyfin.yaml
├── jellyseerr/
│   └── jellyseerr.yaml
├── homarr/
│   └── homarr.yaml
├── wizarr/
│   └── wizarr.yaml
├── tdarr/
│   └── tdarr.yaml
└── flaresolverr/
    └── flaresolverr.yaml
```

## Changes Made

### 1. Backup Organization ✅
- Created `_backups/postgres-dumps/` for PostgreSQL backups
- Created `_backups/sqlite-zips/` for original SQLite backup archives
- Moved 3 SQL files to postgres-dumps
- Moved 5 ZIP files to sqlite-zips
- Added README.md explaining backup structure

### 2. Shared Resources ✅
- Created `_shared/` directory for resources used by multiple apps
- Moved `ingress/` → `_shared/ingress/`
- Moved `middleware/` → `_shared/middleware/`
- Moved `scripts/` → `_shared/scripts/`
- Moved `secrets/` → `_shared/secrets/`
- Merged `storage/` and `storage-hostpath/` → `_shared/storage/`
- Added README.md explaining shared resources

### 3. Per-App Directories ✅
Created individual directories for each application:
- prowlarr/
- radarr/
- sonarr/
- sonarr2/
- bazarr/
- qbittorrent/
- jellyfin/
- jellyseerr/
- homarr/
- wizarr/
- tdarr/
- flaresolverr/

Each contains its deployment YAML file, making it easy to:
- Locate app-specific configurations
- Add app-specific resources later (ConfigMaps, additional services, etc.)
- Manage each app independently
- Scale or modify individual apps without affecting others

### 4. Documentation Updates ✅
- Updated [README.md](README.md) with new directory structure
- Updated [QUICKSTART.md](QUICKSTART.md) with new script paths
- Updated [_shared/scripts/deploy.sh](_shared/scripts/deploy.sh) to reference new paths
- Created [_backups/README.md](_backups/README.md)
- Created [_shared/README.md](_shared/README.md)

## Updated Commands

### Deployment
```bash
cd /workspaces/01-manual\ installation/manifests/08.servarr

# Setup PostgreSQL
./_shared/scripts/01-setup-postgres.sh

# Create NFS directories
./_shared/scripts/02-create-nfs-dirs.sh

# Extract WireGuard config
./_shared/scripts/03-extract-wireguard.sh

# Deploy everything
./_shared/scripts/deploy.sh
```

### Migration
```bash
# SQLite to PostgreSQL migration
./_shared/scripts/migrate-sqlite-to-postgres.sh <app-name> <backup.zip>
```

### Individual App Deployment
```bash
# Deploy just one app
kubectl apply -f prowlarr/prowlarr.yaml
kubectl apply -f radarr/radarr.yaml
# etc.
```

## Benefits

1. **Clarity**: Each app has its own directory - easy to find and modify
2. **Organization**: Backups separated by type (PostgreSQL dumps vs SQLite zips)
3. **Shared Resources**: Common resources grouped in `_shared/` - single source of truth
4. **Scalability**: Easy to add app-specific resources (ConfigMaps, Services, etc.) in app folders
5. **Maintainability**: Cleaner structure makes updates and troubleshooting easier
6. **Git-friendly**: Better for version control - changes to one app don't clutter other apps

## No Breaking Changes

All existing functionality preserved:
- ✅ All deployments still work with updated paths
- ✅ All scripts updated and functional
- ✅ All documentation updated
- ✅ No data loss or corruption
- ✅ No service disruption (apps continue running)

## Next Steps

You can now:
1. Continue with Bazarr data migration: `./_shared/scripts/migrate-sqlite-to-postgres.sh bazarr _backups/sqlite-zips/bazarr_backup_*.zip`
2. Deploy individual apps: `kubectl apply -f <app-name>/<app-name>.yaml`
3. Run full deployment: `./_shared/scripts/deploy.sh`
4. Add app-specific resources in their respective folders as needed
