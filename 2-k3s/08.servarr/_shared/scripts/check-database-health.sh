#!/bin/bash
# Servarr Database Health Check
# Checks for common database corruption issues

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo ""
echo "=================================================="
echo "  Servarr Database Health Check"
echo "=================================================="
echo ""

# Get credentials
echo "🔍 Retrieving database credentials..."
SONARR_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.sonarr-password}' | base64 -d)
SONARR2_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.sonarr2-password}' | base64 -d)
RADARR_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.radarr-password}' | base64 -d)
DB_HOST="192.168.10.105"

ISSUES_FOUND=0

# Function to check a database
check_database() {
    local APP=$1
    local USER=$2
    local DB=$3
    local PW=$4
    local FILE_TABLE=$5  # EpisodeFiles or MovieFiles
    local ITEM_TABLE=$6  # Episodes or Movies

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo -e "${BLUE}📦 Checking ${APP} (${DB})${NC}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    # Check 1: Duplicate Primary Keys in File table
    echo -n "  [1/5] Checking for duplicate ${FILE_TABLE} IDs... "
    DUP_FILES=$(PGPASSWORD="${PW}" psql -h "${DB_HOST}" -U "${USER}" -d "${DB}" -t -c \
        "SELECT COUNT(*) FROM (SELECT \"Id\" FROM \"${FILE_TABLE}\" GROUP BY \"Id\" HAVING COUNT(*) > 1) AS dups;")

    if [[ "${DUP_FILES}" -gt 0 ]]; then
        echo -e "${RED}❌ FAILED${NC}"
        echo -e "     ${RED}Found ${DUP_FILES} duplicate file ID(s)!${NC}"
        PGPASSWORD="${PW}" psql -h "${DB_HOST}" -U "${USER}" -d "${DB}" -c \
            "SELECT \"Id\", COUNT(*) as duplicates FROM \"${FILE_TABLE}\" GROUP BY \"Id\" HAVING COUNT(*) > 1;"
        ISSUES_FOUND=$((ISSUES_FOUND + 1))
    else
        echo -e "${GREEN}✓${NC}"
    fi

    # Check 2: Duplicate Primary Keys in Items (Episodes/Movies)
    echo -n "  [2/5] Checking for duplicate ${ITEM_TABLE} IDs... "
    DUP_ITEMS=$(PGPASSWORD="${PW}" psql -h "${DB_HOST}" -U "${USER}" -d "${DB}" -t -c \
        "SELECT COUNT(*) FROM (SELECT \"Id\" FROM \"${ITEM_TABLE}\" GROUP BY \"Id\" HAVING COUNT(*) > 1) AS dups;")

    if [[ "${DUP_ITEMS}" -gt 0 ]]; then
        echo -e "${RED}❌ FAILED${NC}"
        echo -e "     ${RED}Found ${DUP_ITEMS} duplicate ${ITEM_TABLE} ID(s)!${NC}"
        ISSUES_FOUND=$((ISSUES_FOUND + 1))
    else
        echo -e "${GREEN}✓${NC}"
    fi

    # Check 3: Orphaned items (referencing non-existent files)
    local FILE_ID_COL
    if [[ "${ITEM_TABLE}" == "Episodes" ]]; then
        FILE_ID_COL="EpisodeFileId"
    else
        FILE_ID_COL="MovieFileId"
    fi

    echo -n "  [3/5] Checking for orphaned file references... "
    ORPHANED=$(PGPASSWORD="${PW}" psql -h "${DB_HOST}" -U "${USER}" -d "${DB}" -t -c \
        "SELECT COUNT(*) FROM \"${ITEM_TABLE}\" e WHERE e.\"${FILE_ID_COL}\" IS NOT NULL AND e.\"${FILE_ID_COL}\" > 0 AND NOT EXISTS (SELECT 1 FROM \"${FILE_TABLE}\" ef WHERE ef.\"Id\" = e.\"${FILE_ID_COL}\");")

    if [[ "${ORPHANED}" -gt 0 ]]; then
        echo -e "${YELLOW}⚠ WARNING${NC}"
        echo -e "     ${YELLOW}Found ${ORPHANED} items with invalid file references${NC}"
        ISSUES_FOUND=$((ISSUES_FOUND + 1))
    else
        echo -e "${GREEN}✓${NC}"
    fi

    # Check 4: History table schema (DownloadId and Languages columns)
    echo -n "  [4/5] Checking History table schema... "
    HAS_DOWNLOADID=$(PGPASSWORD="${PW}" psql -h "${DB_HOST}" -U "${USER}" -d "${DB}" -t -c \
        "SELECT COUNT(*) FROM information_schema.columns WHERE table_name = 'History' AND column_name = 'DownloadId';")
    HAS_LANGUAGES=$(PGPASSWORD="${PW}" psql -h "${DB_HOST}" -U "${USER}" -d "${DB}" -t -c \
        "SELECT COUNT(*) FROM information_schema.columns WHERE table_name = 'History' AND column_name = 'Languages';")

    if [[ "${HAS_DOWNLOADID}" -eq 0 ]] || [[ "${HAS_LANGUAGES}" -eq 0 ]]; then
        echo -e "${RED}❌ FAILED${NC}"
        if [[ "${HAS_DOWNLOADID}" -eq 0 ]]; then
            echo -e "     ${RED}Missing column: History.DownloadId${NC}"
        fi
        if [[ "${HAS_LANGUAGES}" -eq 0 ]]; then
            echo -e "     ${RED}Missing column: History.Languages${NC}"
        fi
        ISSUES_FOUND=$((ISSUES_FOUND + 1))
    else
        echo -e "${GREEN}✓${NC}"
    fi

    # Check 5: File table sequence alignment
    echo -n "  [5/5] Checking ${FILE_TABLE} sequence... "
    MAX_ID=$(PGPASSWORD="${PW}" psql -h "${DB_HOST}" -U "${USER}" -d "${DB}" -t -c \
        "SELECT COALESCE(MAX(\"Id\"), 0) FROM \"${FILE_TABLE}\";")
    SEQ_VAL=$(PGPASSWORD="${PW}" psql -h "${DB_HOST}" -U "${USER}" -d "${DB}" -t -c \
        "SELECT last_value FROM \"${FILE_TABLE}_Id_seq\";")

    if [[ "${SEQ_VAL}" -lt "${MAX_ID}" ]]; then
        echo -e "${RED}❌ FAILED${NC}"
        echo -e "     ${RED}Sequence (${SEQ_VAL}) is behind max ID (${MAX_ID})!${NC}"
        echo -e "     ${YELLOW}This will cause duplicate IDs on next insert${NC}"
        ISSUES_FOUND=$((ISSUES_FOUND + 1))
    else
        echo -e "${GREEN}✓${NC} (max: ${MAX_ID}, seq: ${SEQ_VAL})"
    fi
}

# Check all databases
check_database "Sonarr" "sonarr" "sonarr-main" "${SONARR_PW}" "EpisodeFiles" "Episodes"
check_database "Sonarr2 (Anime)" "sonarr2" "sonarr2-main" "${SONARR2_PW}" "EpisodeFiles" "Episodes"
check_database "Radarr" "radarr" "radarr-main" "${RADARR_PW}" "MovieFiles" "Movies"

echo ""
echo "=================================================="
if [[ "${ISSUES_FOUND}" -eq 0 ]]; then
    echo -e "${GREEN}✅ All checks passed! Databases are healthy.${NC}"
else
    echo -e "${RED}❌ Found ${ISSUES_FOUND} issue(s) requiring attention!${NC}"
    echo ""
    echo "Fix guides:"
    echo "  • Duplicate IDs: 2-k3s/08.servarr/TROUBLESHOOTING-DUPLICATE-FILE-IDS.md"
    echo "  • Missing columns: 2-k3s/08.servarr/TROUBLESHOOTING-DB-SCHEMA.md"
    echo "  • Run fix scripts in: 2-k3s/08.servarr/_shared/scripts/"
fi
echo "=================================================="
echo ""

exit "${ISSUES_FOUND}"
