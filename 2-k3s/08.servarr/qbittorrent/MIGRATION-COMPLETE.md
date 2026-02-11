# qBittorrent Migration from TrueNAS to Kubernetes

## Migration Date
January 25, 2026

## Source
- **TrueNAS Path**: `/mnt/apps/qbittorrent`
- **NFS Mount**: `192.168.10.200:/mnt/apps/qbittorrent`

## Destination
- **Namespace**: `servarr`
- **PVC**: `qbittorrent-config`
- **Deployment**: `qbittorrent`

## Transferred Data

### Complete Copy (17.9MB total)
All files and directories from TrueNAS were transferred:

1. **qBittorrent Configuration** (7.8KB)
   - `qBittorrent.conf` - Main settings
   - `qBittorrent-data.conf` - Session data
   - `categories.json` - Download categories
   - `watched_folders.json` - Auto-add folder rules

2. **Torrent Resume Data** (297.5KB)
   - `BT_backup/` directory with 11 torrent files
   - `.fastresume` files preserve torrent state
   - `.torrent` metadata files

3. **RSS Feeds** (1.5KB)
   - `rss/` directory with feed configurations
   - Automated download rules

4. **Application Data** (10MB)
   - `data/` directory with internal qBittorrent data
   - Search engine data
   - Plugin data

5. **GeoIP Database** (5.5MB)
   - `GeoDB/` directory
   - IP geolocation database for peer tracking

6. **Logs** (1.7MB)
   - Historical log files from TrueNAS installation
   - Useful for troubleshooting

7. **Cache & Config** (31.5KB)
   - Internal application cache
   - Additional configuration files

## Backups Created

### Kubernetes Original Config
- **Location**: `/config/qBittorrent.backup.k8s`
- **Purpose**: Backup of initial k8s-generated config before TrueNAS transfer

### WireGuard Config
- **Location**: `/config/wireguard.backup.k8s`
- **Purpose**: Backup of k8s WireGuard configuration (kept original)

## VPN Configuration

### WireGuard Settings (preserved from k8s deployment)
- **Interface**: wg0
- **VPN IP**: 10.13.13.2/32
- **Remote Endpoint**: 45.86.221.65:12662
- **Protocol**: UDP

### Kill Switch (Active)
- iptables rules ensure ALL traffic goes through VPN
- LAN access: 192.168.10.0/24 allowed
- DNS: 1.1.1.1, 1.0.0.1

### Security
- `runAsUser: 0` (required for VPN operations)
- `NET_ADMIN` capability
- `privileged: true`
- `/dev/net/tun` device mounted

## Post-Migration Status

### Verification Results
✅ VPN connected (wg0: UP, 10.13.13.2)
✅ Kill switch active (iptables DROP all non-VPN)
✅ 11 torrents loaded from BT_backup
✅ All categories preserved
✅ RSS feeds restored
✅ GeoIP database available
✅ Historical logs transferred
✅ Web UI accessible at `qbittorrent.epaflix.com`

### Pod Status
```
NAME                           READY   STATUS    RESTARTS   AGE
qbittorrent-59dfcc76fc-l6lnx   1/1     Running   0          2m
```

## Migration Method

Used helper pod with dual mounts:
```yaml
volumes:
  - name: qbittorrent-config
    persistentVolumeClaim:
      claimName: qbittorrent-config
  - name: truenas-mount
    nfs:
      server: 192.168.10.200
      path: /mnt/apps/qbittorrent
```

Copied all files while qBittorrent deployment was scaled to 0 replicas to ensure no file locks or corruption.

## Important Notes

1. **VPN Requirement**: qBittorrent MUST only connect via VPN - kill switch enforced
2. **Root Required**: Container runs as root for VPN operations (NET_ADMIN)
3. **Torrent State**: All torrents should resume from where they left off
4. **Categories**: Download categories from TrueNAS preserved
5. **RSS**: Automated RSS download rules intact
6. **Logs**: Historical logs available for reference

## Next Steps

1. ✅ Verify torrents resume correctly in WebUI
2. ⏳ Test downloading new torrents
3. ⏳ Verify RSS feeds auto-download
4. ⏳ Confirm categories work properly
5. ⏳ Test VPN kill switch (disconnect VPN, ensure no leaks)

## Files
- Deployment: [qbittorrent.yaml](qbittorrent.yaml)
- Ingress: [../_shared/ingress/internal-routes.yaml](../_shared/ingress/internal-routes.yaml)
- Service: Defined in qbittorrent.yaml
