# Servarr Deployment - Quick Start Guide

Follow these steps to deploy the complete Servarr ecosystem to your k3s cluster.

## Prerequisites

- k3s cluster running and accessible via kubectl
- NFS server at 192.168.10.200 with media directories
- PostgreSQL at 192.168.10.105
- NVIDIA GPU on at least one worker node
- Traefik deployed with Cloudflare DNS-01 resolver

## Deployment Steps

### 1. Prepare NFS Storage on TrueNAS

```bash
cd /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr
```

App configs are stored in `local-path` PVCs (auto-provisioned when deployments are applied). No NFS config directories needed.

### 2. Setup PostgreSQL Databases

```bash
# Create databases and generate secret
./_shared/scripts/01-setup-postgres.sh
```

This will:
- Create 5 databases (sonarr-main, sonarr2-main, radarr-main, prowlarr-main, jellyseerr)
- Generate secure passwords
- Create Kubernetes secret manifest

**Important**: Save the displayed passwords securely!

### 3. Create WireGuard Secret

```bash
# Fill in actual keys from secrets.yml, then apply
kubectl apply -f _shared/secrets/wireguard-secret.yaml
```

### 4. Deploy Everything

```bash
# Deploy the complete stack
./_shared/scripts/deploy.sh
```

This script will:
1. Create the `servarr` namespace
2. Apply secrets (PostgreSQL + WireGuard)
3. Create all storage (PV/PVC)
4. Deploy apps in correct order
5. Create Traefik IngressRoutes
6. Verify VPN is working

**The deployment takes about 10-15 minutes.**

### 5. Post-Deployment Configuration

Once all pods are running:

#### A. Configure Prowlarr
1. Open http://prowlarr.epaflix.com
2. Add indexers
3. Settings → Apps → Add Applications:
   - Sonarr: http://sonarr:8989
   - Sonarr2: http://sonarr2:8989
   - Radarr: http://radarr:7878
   - Use existing API keys from TrueNAS configs

#### B. Configure Download Clients in *arr Apps

In each app (Sonarr, Sonarr2, Radarr):
1. Settings → Download Clients → Add → qBittorrent
2. Host: `qbittorrent`
3. Port: `8080`
4. Category: `tv` (Sonarr), `anime` (Sonarr2), `movies` (Radarr)

#### C. Configure Jellyfin GPU Transcoding
1. Open https://jellyfin.epaflix.com
2. Dashboard → Playback → Transcoding
3. Hardware acceleration: NVIDIA NVENC
4. Enable hardware decoding for all codecs
5. Test with a high-bitrate video

#### D. Configure Jellyseerr
1. Open https://jellyseerr.epaflix.com
2. Connect to Jellyfin: http://jellyfin:8096
3. Connect to Sonarr and Radarr using internal service URLs
4. Configure request permissions

## Migration from TrueNAS

> **Completed.** The TrueNAS → K3s migration was done in January 2026. Apps now use `local-path` PVCs for config storage and PostgreSQL for databases (migrated via SQL dumps). See `qbittorrent/MIGRATION-COMPLETE.md` for the historical record.

## Verification

### Check VPN is Working
```bash
kubectl exec -n servarr deployment/qbittorrent -- curl ifconfig.me
# Should show VPN IP, NOT 192.168.x.x
```

### Check GPU is Available
```bash
kubectl get nodes -o yaml | grep nvidia.com/gpu
# Should show: nvidia.com/gpu: "1" under allocatable
```

### Check Hardlinks
After a download completes and moves to media folder:
```bash
ls -i /mnt/pool1/dataset01/downloads/completed/show.mkv
ls -i /mnt/pool1/dataset01/tvshows/Show/Season\ 01/show.mkv
# Inode numbers should match (hardlink successful)
```

### Check All Pods Running
```bash
kubectl get pods -n servarr
# All should be Running
```

## Troubleshooting

### Pods Stuck in Pending
```bash
kubectl describe pod -n servarr <pod-name>
# Check events for PVC binding or resource issues
```

### PVC Not Binding
```bash
kubectl get pv,pvc -n servarr
# Verify NFS paths exist on TrueNAS
```

### VPN Not Working
```bash
kubectl logs -n servarr deployment/qbittorrent
# Check for WireGuard initialization errors
```

### GPU Not Available
```bash
kubectl describe node <gpu-node>
# Check if nvidia.com/gpu is listed in allocatable resources
# May need to install NVIDIA device plugin
```

## Access URLs

### Public (via Cloudflare)
- **Jellyfin**: https://jellyfin.epaflix.com
- **Jellyseerr**: https://jellyseerr.epaflix.com

### Internal LAN (*.epaflix.com via Pi-hole DNS → 192.168.10.101)
- **Sonarr**: http://sonarr.epaflix.com
- **Sonarr2** (Anime): http://sonarr2.epaflix.com
- **Radarr**: http://radarr.epaflix.com
- **Prowlarr**: http://prowlarr.epaflix.com
- **Bazarr**: http://bazarr.epaflix.com
- **qBittorrent**: http://qbittorrent.epaflix.com
- **Tdarr**: http://tdarr.epaflix.com
- **Homarr**: http://homarr.epaflix.com
- **Wizarr**: http://wizarr.epaflix.com

## Next Steps

1. **Configure media libraries in Jellyfin**
   - Add /animes, /tvshows, /movies

2. **Setup quality profiles in *arr apps**
   - Configure preferred quality settings

3. **Configure Tdarr transcoding rules**
   - Set up library and transcode flows

4. **Customize Homarr dashboard**
   - Add all app tiles and widgets

5. **Setup Wizarr invitations**
   - Configure Jellyfin integration

## Support

For issues, check:
- README.md for detailed documentation
- Pod logs: `kubectl logs -n servarr <pod-name>`
- Events: `kubectl get events -n servarr --sort-by='.lastTimestamp'`
