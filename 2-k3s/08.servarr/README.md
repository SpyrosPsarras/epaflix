# Servarr Ecosystem Deployment for k3s

This directory contains the Kubernetes manifests for deploying the complete Servarr media management ecosystem migrated from TrueNAS Electric Eel.

## Directory Structure

```
08.servarr/
├── _backups/              # PostgreSQL dumps and SQLite backup archives
│   ├── postgres-dumps/    # .sql and .sql.gz database backups
│   └── sqlite-zips/       # Original app backup zips
├── _shared/               # Shared resources across all apps
│   ├── ingress/          # Traefik IngressRoute configs
│   ├── middleware/       # Traefik middleware
│   ├── scripts/          # Deployment and migration scripts
│   ├── secrets/          # PostgreSQL and VPN credentials
│   └── storage/          # PV/PVC definitions
├── prowlarr/             # Indexer manager deployment
├── radarr/               # Movie management deployment
├── sonarr/               # TV show management deployment
├── sonarr2/              # Anime management deployment
├── bazarr/               # Subtitle management deployment
├── qbittorrent/          # Torrent client with VPN
├── jellyfin/             # Media server
├── jellyseerr/           # Media request system
├── homarr/               # Dashboard
├── wizarr/               # User invitation system
├── tdarr/                # Media transcoding
└── flaresolverr/         # Cloudflare bypass
```

## Architecture

- **Namespace**: `servarr`
- **Storage**: NFS for configs and media (568:568), local-path for transcoding cache
- **Networking**: Traefik IngressRoutes to 192.168.10.101
- **GPU**: NVIDIA RTX 2070 Super for Jellyfin and Tdarr
- **VPN**: WireGuard for qBittorrent downloads

## Applications

### Core *arr Apps
- **Sonarr**: TV shows management (port 8989) → `/tv` = `/mnt/pool1/dataset01/tvshows`
- **Sonarr2**: Anime management (port 28989) → `/animes` = `/mnt/pool1/dataset01/animes`
- **Radarr**: Movies management (port 7878) → `/movies` = `/mnt/pool1/dataset01/movies`
- **Prowlarr**: Indexer manager (port 9696)
- **Bazarr**: Subtitle management (port 6767, development branch)

### Media & Downloads
- **Jellyfin**: Media server (port 8096) with NVIDIA GPU transcoding
  - **Note**: `jellyfin.epaflix.com` is redirected to TrueNAS (192.168.10.200:30013) via `jellyfin/jellyfin-truenas-redirect.yaml`
  - The k3s Jellyfin pod runs but is not publicly accessible
- **Jellyseerr**: Media request management (port 5055)
- **qBittorrent**: Torrent client with WireGuard VPN (ports 8080, 8999)
- **FlareSolverr**: Cloudflare bypass (port 8191)

### Utilities
- **Tdarr**: Media transcoding with DoVi node (ports 8265, 8266, NVIDIA GPU)
- **Homarr**: Dashboard
- **Wizarr**: User invitation system

## Storage Layout

### App Config Storage (local-path PVCs on K3s nodes)
Each app gets a `local-path` PVC for its config directory (auto-provisioned on deploy):
- `sonarr-config`, `sonarr2-config`, `radarr-config`, `prowlarr-config`
- `bazarr-config`, `jellyseerr-config`, `qbittorrent-config`
- `jellyfin-config`, `jellyfin-cache`, `jellyfin-transcodes`
- `homarr-config`, `huntarr-config`, `cleanuparr-config`, `wizarr-config`

### NFS Media Storage (TrueNAS 192.168.10.200)
Mounted on K3s worker nodes via fstab, exposed as hostPath PVs:

| TrueNAS Export | Node Mount | Used By |
|----------------|------------|---------|
| `/mnt/pool1/dataset01/animes` | `/mnt/k3s-animes` | Sonarr2, Jellyfin |
| `/mnt/pool1/dataset01/tvshows` | `/mnt/k3s-tvshows` | Sonarr, Jellyfin |
| `/mnt/pool1/dataset01/movies` | `/mnt/k3s-movies` | Radarr, Jellyfin |
| `/mnt/pool1/dataset01/downloads` | `/mnt/k3s-downloads` | qBittorrent, all *arr apps |

## Prerequisites

### 1. PostgreSQL Databases
```bash
# Connect to PostgreSQL
PGPASSWORD="<POSTGRES_PASSWORD>" psql -h 192.168.10.105 -U postgres

# Create databases
CREATE DATABASE "sonarr-main";
CREATE DATABASE "sonarr2-main";
CREATE DATABASE "radarr-main";
CREATE DATABASE "prowlarr-main";
CREATE DATABASE "jellyseerr";

# Create users with secure passwords
CREATE USER sonarr WITH PASSWORD 'YOUR_SECURE_PASSWORD';
CREATE USER sonarr2 WITH PASSWORD 'YOUR_SECURE_PASSWORD';
CREATE USER radarr WITH PASSWORD 'YOUR_SECURE_PASSWORD';
CREATE USER prowlarr WITH PASSWORD 'YOUR_SECURE_PASSWORD';
CREATE USER jellyseerr WITH PASSWORD 'YOUR_SECURE_PASSWORD';

# Grant permissions
GRANT ALL PRIVILEGES ON DATABASE "sonarr-main" TO sonarr;
GRANT ALL PRIVILEGES ON DATABASE "sonarr2-main" TO sonarr2;
GRANT ALL PRIVILEGES ON DATABASE "radarr-main" TO radarr;
GRANT ALL PRIVILEGES ON DATABASE "prowlarr-main" TO prowlarr;
GRANT ALL PRIVILEGES ON DATABASE "jellyseerr" TO jellyseerr;
```

### 3. NVIDIA Device Plugin
```bash
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.14.0/nvidia-device-plugin.yml

# Verify GPU is available
kubectl get nodes -o yaml | grep nvidia.com/gpu
```

### 4. WireGuard Config
WireGuard config is stored in `secrets.yml`. Create the K8s secret from `_shared/secrets/wireguard-secret.yaml`.

## Deployment Order

1. **Namespace and Secrets**
   ```bash
   kubectl apply -f namespace.yaml
   kubectl apply -f secrets/
   ```

2. **Storage (PV/PVC)**
   ```bash
   kubectl apply -f storage/
   kubectl get pvc -n servarr  # Wait for all to be Bound
   ```

3. **Applications**
   ```bash
   # Deploy in order (some apps depend on others)
   kubectl apply -f prowlarr/
   kubectl apply -f flaresolverr/
   kubectl apply -f qbittorrent/

   # Wait for download client to be ready
   kubectl wait --for=condition=ready pod -l app=qbittorrent -n servarr --timeout=300s

   # Deploy *arr apps
   kubectl apply -f sonarr/
   kubectl apply -f sonarr2/
   kubectl apply -f radarr/
   kubectl apply -f bazarr/

   # Deploy media apps
   kubectl apply -f jellyfin/
   kubectl apply -f jellyseerr/

   # Deploy utilities
   kubectl apply -f homarr/
   ```

4. **Ingress Routes**
   ```bash
   kubectl apply -f ingress/
   ```

## Post-Deployment Configuration

### 1. Verify qBittorrent VPN
```bash
# Check VPN connection
kubectl exec -n servarr -it deployment/qbittorrent -- curl ifconfig.me
# Should show VPN IP, NOT 192.168.x.x
```

### 2. Configure Download Clients in *arr Apps
Update each app (Sonarr, Sonarr2, Radarr) to use:
- Host: `qbittorrent`
- Port: `8080`
- Category: `tv` / `anime` / `movies` respectively

### 3. Configure Prowlarr Sync
Add applications in Prowlarr:
- Sonarr: `http://sonarr:8989`
- Sonarr2: `http://sonarr2:8989`
- Radarr: `http://radarr:7878`

Use existing API keys from TrueNAS configs.

### 4. Jellyfin GPU Transcoding
In Jellyfin settings:
- Dashboard → Playback → Transcoding
- Hardware acceleration: NVIDIA NVENC
- Enable hardware decoding for all codecs

### 5. Verify Hardlinks
```bash
# Download a file and let Sonarr/Radarr move it
# Then check:
ls -i /mnt/pool1/dataset01/downloads/completed/show.mkv
ls -i /mnt/pool1/dataset01/tvshows/Show/Season\ 01/show.mkv
# Inode numbers should match = hardlink successful
```

## Access URLs

### Internet (via Cloudflare + Traefik 192.168.10.101)
- Jellyfin: https://jellyfin.epaflix.com
- Jellyseerr: https://jellyseerr.epaflix.com

### Internal LAN (*.epaflix.com → 192.168.10.101 via Pi-hole)
- Sonarr: http://sonarr.epaflix.com
- Sonarr2: http://sonarr2.epaflix.com
- Radarr: http://radarr.epaflix.com
- Prowlarr: http://prowlarr.epaflix.com
- Bazarr: http://bazarr.epaflix.com
- qBittorrent: http://qbittorrent.epaflix.com
- Tdarr: http://tdarr.epaflix.com
- Homarr: http://homarr.epaflix.com
- Wizarr: http://wizarr.epaflix.com

## Migration from TrueNAS

> **Completed.** The TrueNAS → K3s migration was done in January 2026. Apps use `local-path` PVCs for config and PostgreSQL for databases. See `qbittorrent/MIGRATION-COMPLETE.md` for details.

## Troubleshooting

### Pods in CrashLoopBackOff
```bash
kubectl logs -n servarr <pod-name>
kubectl describe pod -n servarr <pod-name>
```

### PVC not binding
```bash
kubectl get pv,pvc -n servarr
kubectl describe pvc -n servarr <pvc-name>
# Check NFS mount on TrueNAS is accessible
```

### VPN not working
```bash
kubectl exec -n servarr -it deployment/qbittorrent -- bash
# Inside pod:
ip addr  # Check wg0 interface exists
curl ifconfig.me  # Should show VPN IP
ping 8.8.8.8  # Test connectivity
```

### GPU not detected
```bash
kubectl get nodes -o yaml | grep -A10 allocatable
# Look for nvidia.com/gpu
kubectl describe node <gpu-node>
```

## Notes

- All apps run as PUID=568, PGID=568 (matching TrueNAS permissions)
- Timezone: Europe/Oslo
- Existing API keys from TrueNAS are preserved in config files
- Bazarr runs on development branch for latest features
- Hardlinks require full download volume mount (no subPath)
