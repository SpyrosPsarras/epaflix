#!/usr/bin/env bash
set -euo pipefail


DATE=$(date +%Y-%m-%d)
TIME=$(date +%H-%M-%S)
SESSION_NAME="${1:-session}"
LOG_FILE=".history/${DATE}-${SESSION_NAME}-${TIME}.log"

echo "Starting logged session: ${LOG_FILE}"
echo "Exit with 'exit' or Ctrl+D to stop logging"
echo ""

cat > "${LOG_FILE}" << EOF

---

EOF

script -a -q -c "bash --norc" "${LOG_FILE}"

echo ""
echo "Session saved to: ${LOG_FILE}"
