#!/bin/bash
# Fix missing DownloadId and Languages columns in all Servarr History tables
# This script is idempotent and safe to run multiple times

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo ""
echo "=================================================="
echo "  Servarr History Schema Fix - All Apps"
echo "=================================================="
echo ""

# Get database credentials from secret
echo "🔍 Retrieving database credentials..."
SONARR_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.sonarr-password}' | base64 -d)
SONARR2_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.sonarr2-password}' | base64 -d)
RADARR_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.radarr-password}' | base64 -d)
DB_HOST="192.168.10.105"

echo "✅ Credentials retrieved"
echo ""

# Function to check and fix a database
fix_database() {
    local APP=$1
    local USER=$2
    local DB=$3
    local PW=$4
    
    echo "----------------------------------------"
    echo "📦 Checking ${APP} (${DB})"
    echo "----------------------------------------"
    
    # Check current schema
    echo "Current columns:"
    COLUMNS=$(PGPASSWORD="${PW}" psql -h "${DB_HOST}" -U "${USER}" -d "${DB}" -t -c \
        "SELECT count(*) FROM information_schema.columns WHERE table_name = 'History';")
    
    echo "  Column count: ${COLUMNS}"
    
    # Check for missing columns
    HAS_DOWNLOADID=$(PGPASSWORD="${PW}" psql -h "${DB_HOST}" -U "${USER}" -d "${DB}" -t -c \
        "SELECT count(*) FROM information_schema.columns WHERE table_name = 'History' AND column_name = 'DownloadId';")
    HAS_LANGUAGES=$(PGPASSWORD="${PW}" psql -h "${DB_HOST}" -U "${USER}" -d "${DB}" -t -c \
        "SELECT count(*) FROM information_schema.columns WHERE table_name = 'History' AND column_name = 'Languages';")
    
    NEEDS_FIX=false
    
    if [[ "${HAS_DOWNLOADID}" -eq 0 ]]; then
        echo -e "${YELLOW}  ⚠ Missing: DownloadId${NC}"
        NEEDS_FIX=true
    else
        echo -e "${GREEN}  ✓ Has: DownloadId${NC}"
    fi
    
    if [[ "${HAS_LANGUAGES}" -eq 0 ]]; then
        echo -e "${YELLOW}  ⚠ Missing: Languages${NC}"
        NEEDS_FIX=true
    else
        echo -e "${GREEN}  ✓ Has: Languages${NC}"
    fi
    
    if [[ "${NEEDS_FIX}" == "true" ]]; then
        echo ""
        echo "🔧 Applying fix..."
        
        PGPASSWORD="${PW}" psql -h "${DB_HOST}" -U "${USER}" -d "${DB}" << EOF
-- Add missing DownloadId column if it doesn't exist
ALTER TABLE "History" ADD COLUMN IF NOT EXISTS "DownloadId" text;

-- Add missing Languages column if it doesn't exist
ALTER TABLE "History" ADD COLUMN IF NOT EXISTS "Languages" text DEFAULT '[]'::text NOT NULL;
EOF
        
        echo -e "${GREEN}✅ ${APP} fixed!${NC}"
    else
        echo -e "${GREEN}✅ ${APP} schema is correct (no fix needed)${NC}"
    fi
    
    # Show final column count
    FINAL_COLUMNS=$(PGPASSWORD="${PW}" psql -h "${DB_HOST}" -U "${USER}" -d "${DB}" -t -c \
        "SELECT count(*) FROM information_schema.columns WHERE table_name = 'History';")
    echo "  Final column count: ${FINAL_COLUMNS}"
    echo ""
}

# Fix all databases
fix_database "Sonarr" "sonarr" "sonarr-main" "${SONARR_PW}"
fix_database "Sonarr2" "sonarr2" "sonarr2-main" "${SONARR2_PW}"
fix_database "Radarr" "radarr" "radarr-main" "${RADARR_PW}"

echo "=================================================="
echo "✅ All databases checked and fixed (if needed)"
echo "=================================================="
echo ""
echo "ℹ️  This fix is idempotent. Safe to run anytime."
echo ""
