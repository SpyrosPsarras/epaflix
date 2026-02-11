#!/usr/bin/env bash
set -uo pipefail

# Database Backup Script
# Backs up all databases from the PostgreSQL cluster to timestamped files

# Configuration
POSTGRES_HOST="192.168.10.105"
POSTGRES_USER="postgres"
POSTGRES_PASSWORD="<POSTGRES_PASSWORD>"
BACKUP_DIR="/home/spy/Documents/Epaflix/k3s-proxmox/2-k3s/maintenance/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Create backup directory if it doesn't exist
mkdir -p "$BACKUP_DIR"

# List of databases to backup (excluding system databases)
DATABASES=(
    "authentik"
    "bazarr-main"
    "jellyseerr"
    "observability"
    "prowlarr-log"
    "prowlarr-main"
    "radarr-log"
    "radarr-main"
    "sonarr-log"
    "sonarr-main"
    "sonarr2-main"
)

echo "========================================="
echo "Database Backup Script"
echo "========================================="
echo "Backup Directory: $BACKUP_DIR"
echo "Timestamp: $TIMESTAMP"
echo "Total Databases: ${#DATABASES[@]}"
echo "========================================="
echo ""

# Export password for pg_dump
export PGPASSWORD="$POSTGRES_PASSWORD"

# Backup each database
SUCCESSFUL=0
FAILED=0

for db in "${DATABASES[@]}"; do
    echo "Backing up database: $db"
    BACKUP_FILE="$BACKUP_DIR/${db}_${TIMESTAMP}.sql"
    
    if pg_dump -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$db" > "$BACKUP_FILE" 2>&1; then
        # Compress the backup
        if gzip "$BACKUP_FILE"; then
            BACKUP_SIZE=$(du -h "${BACKUP_FILE}.gz" | awk '{print $1}')
            echo "  ✓ Successfully backed up $db (${BACKUP_SIZE})"
            SUCCESSFUL=$((SUCCESSFUL + 1))
        else
            echo "  ✗ Failed to compress $db"
            FAILED=$((FAILED + 1))
            rm -f "$BACKUP_FILE" "${BACKUP_FILE}.gz"
        fi
    else
        echo "  ✗ Failed to backup $db"
        FAILED=$((FAILED + 1))
        # Remove incomplete backup file if it exists
        rm -f "$BACKUP_FILE"
    fi
    echo ""
done

# Cleanup old backups (keep last 7 days)
echo "Cleaning up backups older than 7 days..."
find "$BACKUP_DIR" -name "*.sql.gz" -type f -mtime +7 -delete
echo ""

# Summary
echo "========================================="
echo "Backup Summary"
echo "========================================="
echo "Successful: $SUCCESSFUL"
echo "Failed: $FAILED"
echo "Location: $BACKUP_DIR"
echo "========================================="

# List recent backups
echo ""
echo "Recent backups:"
ls -lh "$BACKUP_DIR" | grep "$TIMESTAMP" || echo "No backups found for this run"

exit 0
