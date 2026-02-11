#!/bin/bash
# Radarr Database Health Check Script
# Checks for duplicate IDs and sequence mismatches in critical tables

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}=========================================="
echo "Radarr Database Health Check"
echo -e "==========================================${NC}"
echo ""

# Get database credentials
echo "🔍 Retrieving database credentials..."
RADARR_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.radarr-password}' | base64 -d)
DB_HOST="192.168.10.105"
DB_NAME="radarr-main"
DB_USER="radarr"

if [ -z "$RADARR_PW" ]; then
  echo -e "${RED}❌ Failed to retrieve database password${NC}"
  exit 1
fi

echo -e "${GREEN}✅ Credentials retrieved${NC}"
echo ""

# Function to run psql query
run_query() {
  PGPASSWORD="${RADARR_PW}" psql -h "${DB_HOST}" -U "${DB_USER}" -d "${DB_NAME}" -t -c "$1"
}

# Function to check for duplicates in a table
check_duplicates() {
  local table=$1
  local count=$(run_query "SELECT COUNT(*) FROM (SELECT \"Id\" FROM \"${table}\" GROUP BY \"Id\" HAVING COUNT(*) > 1) sub;")
  count=$(echo $count | xargs) # trim whitespace

  if [ "$count" -eq 0 ]; then
    echo -e "${GREEN}✅ ${table}: No duplicates found${NC}"
    return 0
  else
    echo -e "${RED}❌ ${table}: Found ${count} duplicate IDs${NC}"
    echo "   Run: kubectl apply -f fix-duplicate-${table,,}.yaml"
    return 1
  fi
}

# Function to check sequence alignment
check_sequence() {
  local table=$1
  local result=$(run_query "SELECT MAX(\"Id\"), (SELECT last_value FROM \"${table}_Id_seq\") FROM \"${table}\";")
  local max_id=$(echo $result | awk '{print $1}' | xargs)
  local sequence=$(echo $result | awk '{print $3}' | xargs)

  if [ "$sequence" -ge "$max_id" ]; then
    echo -e "${GREEN}✅ ${table}: Sequence OK (max_id=${max_id}, sequence=${sequence})${NC}"
    return 0
  else
    echo -e "${RED}❌ ${table}: Sequence MISMATCH (max_id=${max_id}, sequence=${sequence})${NC}"
    echo "   Sequence should be >= max_id!"
    return 1
  fi
}

# Check duplicates in critical tables
echo -e "${BLUE}📊 Checking for duplicate IDs...${NC}"
DUPLICATES_FOUND=0

check_duplicates "Movies" || DUPLICATES_FOUND=1
check_duplicates "MovieFiles" || DUPLICATES_FOUND=1

echo ""

# Check sequence alignment
echo -e "${BLUE}📊 Checking sequence alignment...${NC}"
SEQUENCES_BAD=0

check_sequence "Movies" || SEQUENCES_BAD=1
check_sequence "MovieFiles" || SEQUENCES_BAD=1

echo ""

# Show detailed stats
echo -e "${BLUE}📊 Database Statistics:${NC}"
run_query "SELECT COUNT(*) as total_movies FROM \"Movies\";" | xargs | awk '{print "   Movies: " $1}'
run_query "SELECT COUNT(*) as total_files FROM \"MovieFiles\";" | xargs | awk '{print "   MovieFiles: " $1}'

echo ""

# Final summary
if [ $DUPLICATES_FOUND -eq 0 ] && [ $SEQUENCES_BAD -eq 0 ]; then
  echo -e "${GREEN}=========================================="
  echo "✅ Database is HEALTHY"
  echo -e "==========================================${NC}"
  exit 0
else
  echo -e "${YELLOW}=========================================="
  echo "⚠️  Database issues found!"
  echo -e "==========================================${NC}"
  echo ""

  if [ $DUPLICATES_FOUND -eq 1 ]; then
    echo -e "${YELLOW}Fix duplicates with:${NC}"
    echo "  kubectl apply -f /home/spy/Documents/Epaflix/k3s-proxmox/2-k3s/08.servarr/radarr/fix-duplicate-movies.yaml"
    echo "  kubectl apply -f /home/spy/Documents/Epaflix/k3s-proxmox/2-k3s/08.servarr/radarr/fix-duplicate-moviefiles.yaml"
    echo ""
  fi

  if [ $SEQUENCES_BAD -eq 1 ]; then
    echo -e "${YELLOW}Sequence issues will be fixed automatically by the duplicate fix jobs${NC}"
    echo ""
  fi

  echo -e "${YELLOW}After fixes, restart Radarr:${NC}"
  echo "  kubectl rollout restart deployment/radarr -n servarr"
  echo ""

  exit 1
fi
