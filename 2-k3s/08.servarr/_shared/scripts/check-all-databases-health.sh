#!/bin/bash
# Master Database Health Check Script for All *arr Services
# Checks Sonarr, Sonarr2, and Radarr databases for duplicates and sequence issues

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

echo -e "${CYAN}=========================================="
echo "All *arr Services Database Health Check"
echo -e "==========================================${NC}"
echo ""

OVERALL_STATUS=0
SERVICES_CHECKED=0
SERVICES_HEALTHY=0
SERVICES_UNHEALTHY=0

# Function to run individual health check
check_service() {
  local service_name=$1
  local service_dir=$2

  echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${CYAN}Checking ${service_name}...${NC}"
  echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo ""

  SERVICES_CHECKED=$((SERVICES_CHECKED + 1))

  if [ -f "${BASE_DIR}/${service_dir}/check-database-health.sh" ]; then
    if bash "${BASE_DIR}/${service_dir}/check-database-health.sh"; then
      SERVICES_HEALTHY=$((SERVICES_HEALTHY + 1))
      echo ""
    else
      SERVICES_UNHEALTHY=$((SERVICES_UNHEALTHY + 1))
      OVERALL_STATUS=1
      echo ""
    fi
  else
    echo -e "${YELLOW}⚠️  Health check script not found for ${service_name}${NC}"
    echo "   Expected: ${BASE_DIR}/${service_dir}/check-database-health.sh"
    echo ""
    SERVICES_UNHEALTHY=$((SERVICES_UNHEALTHY + 1))
    OVERALL_STATUS=1
  fi
}

# Check all services
check_service "Sonarr (TV Shows)" "sonarr"
check_service "Sonarr2 (Anime)" "sonarr2"
check_service "Radarr (Movies)" "radarr"

# Final summary
echo -e "${CYAN}=========================================="
echo "Overall Summary"
echo -e "==========================================${NC}"
echo ""
echo "Services checked: ${SERVICES_CHECKED}"
echo -e "${GREEN}Healthy: ${SERVICES_HEALTHY}${NC}"
if [ $SERVICES_UNHEALTHY -gt 0 ]; then
  echo -e "${RED}Unhealthy: ${SERVICES_UNHEALTHY}${NC}"
fi
echo ""

if [ $OVERALL_STATUS -eq 0 ]; then
  echo -e "${GREEN}=========================================="
  echo "✅ ALL DATABASES ARE HEALTHY"
  echo -e "==========================================${NC}"
else
  echo -e "${YELLOW}=========================================="
  echo "⚠️  SOME DATABASES HAVE ISSUES"
  echo -e "==========================================${NC}"
  echo ""
  echo -e "${YELLOW}Next steps:${NC}"
  echo "1. Review the output above for specific issues"
  echo "2. Apply the suggested fix jobs for each affected service"
  echo "3. Restart affected services after fixes"
  echo "4. Re-run this script to verify fixes"
  echo ""
  echo -e "${YELLOW}Quick fix all (if multiple services affected):${NC}"
  echo "  cd ${BASE_DIR}"
  echo "  # For each unhealthy service, apply fixes:"
  echo "  kubectl apply -f sonarr/fix-duplicate-series.yaml"
  echo "  kubectl apply -f sonarr/fix-duplicate-episodes.yaml"
  echo "  kubectl apply -f sonarr/fix-duplicate-episodefiles.yaml"
  echo "  kubectl apply -f sonarr2/fix-duplicate-series.yaml"
  echo "  kubectl apply -f sonarr2/fix-duplicate-episodes.yaml"
  echo "  kubectl apply -f sonarr2/fix-duplicate-episodefiles.yaml"
  echo "  kubectl apply -f radarr/fix-duplicate-movies.yaml"
  echo "  kubectl apply -f radarr/fix-duplicate-moviefiles.yaml"
  echo ""
  echo "  # Then restart all services:"
  echo "  kubectl rollout restart deployment/sonarr -n servarr"
  echo "  kubectl rollout restart deployment/sonarr2 -n servarr"
  echo "  kubectl rollout restart deployment/radarr -n servarr"
fi

echo ""
exit $OVERALL_STATUS
