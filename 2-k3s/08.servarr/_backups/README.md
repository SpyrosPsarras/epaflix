# Servarr Backups

## Structure

- **postgres-dumps/**: PostgreSQL database backups (.sql, .sql.gz)
- **sqlite-zips/**: Original SQLite backup archives from app UI exports

## PostgreSQL Dumps

Created after successful SQLite-to-PostgreSQL migrations:
- `prowlarr-main-backup-*.sql.gz` - Prowlarr database (12 indexers, partial History)
- `radarr-main-backup-*.sql.gz` - Radarr database (110 movies, 98 files)
- Sonarr backups (if created)
- Sonarr2 backups (if created)

## SQLite Archives

Original backup zips exported from app UIs before PostgreSQL migration:
- `prowlarr_backup_*.zip`
- `radarr_backup_*.zip`
- `sonarr_backup_*.zip`
- `sonarr2_backup_*.zip`
- `bazarr_backup_*.zip`

These were used as source data for `pgloader` migrations.
