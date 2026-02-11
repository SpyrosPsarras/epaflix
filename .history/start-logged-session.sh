#!/usr/bin/env bash
set -euo pipefail

# Start a logged terminal session
# All commands and outputs will be captured automatically

DATE=$(date +%Y-%m-%d)
TIME=$(date +%H-%M-%S)
SESSION_NAME="${1:-session}"
LOG_FILE=".history/${DATE}-${SESSION_NAME}-${TIME}.log"

echo "Starting logged session: ${LOG_FILE}"
echo "Exit with 'exit' or Ctrl+D to stop logging"
echo ""

# Add header to log file
cat > "${LOG_FILE}" << EOF
# Logged Terminal Session
# Date: ${DATE}
# Time: ${TIME}
# Session: ${SESSION_NAME}

---

EOF

# Start script command to capture everything
script -a -q -c "bash --norc" "${LOG_FILE}"

echo ""
echo "Session saved to: ${LOG_FILE}"
