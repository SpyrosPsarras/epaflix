# Database Backups

This directory contains compressed SQL dumps of all databases from the PostgreSQL cluster.

## Backup Information

- **Host**: 192.168.10.105 (postgres-rw LoadBalancer)
- **Format**: SQL dumps compressed with gzip
- **Naming**: `{database-name}_{timestamp}.sql.gz`
- **Retention**: 7 days (automatic cleanup)

## Databases Backed Up

1. **authentik** - Authentik SSO authentication database
2. **bazarr-main** - Bazarr subtitle management
3. **jellyseerr** - Jellyseerr media request management
4. **observability** - Grafana/monitoring data
5. **prowlarr-log** - Prowlarr logs
6. **prowlarr-main** - Prowlarr indexer management
7. **radarr-log** - Radarr logs
8. **radarr-main** - Radarr movie management
9. **sonarr-log** - Sonarr logs
10. **sonarr-main** - Sonarr TV show management
11. **sonarr2-main** - Sonarr secondary instance

## Running Backups

Execute the backup script from the maintenance directory:

```bash
cd /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/maintenance
./backup-all-databases.sh
```

## Restoring a Database

To restore a specific database:

```bash
# Decompress the backup
gunzip -c {database-name}_{timestamp}.sql.gz > restore.sql

# Restore to PostgreSQL
PGPASSWORD="<POSTGRES_PASSWORD>" psql -h 192.168.10.105 -U postgres -d {database-name} < restore.sql

# Cleanup
rm restore.sql
```

Example:
```bash
gunzip -c sonarr-main_20260224_140353.sql.gz > restore.sql
PGPASSWORD="<POSTGRES_PASSWORD>" psql -h 192.168.10.105 -U postgres -d sonarr-main < restore.sql
rm restore.sql
```

## Automated Cleanup

The backup script automatically removes backups older than 7 days to prevent disk space issues.

## Backup Schedule

- **Manual**: Run `./backup-all-databases.sh` anytime
- **Automated**: Consider adding to cron for scheduled backups

Example cron entry (daily at 3 AM):
```
0 3 * * * /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/maintenance/backup-all-databases.sh >> /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/maintenance/backup.log 2>&1
```
