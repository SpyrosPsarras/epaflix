#!/usr/bin/env bash
# Command logger wrapper - logs individual commands with context
# Usage: ./log-cmd.sh "Description of what this does" "command to run"

set -euo pipefail

if [ "$#" -lt 2 ]; then
    echo "Usage: $0 <description> <command> [log-file]"
    echo "Example: $0 'Check VM status' 'qm status 8001'"
    exit 1
fi

DESCRIPTION="$1"
COMMAND="$2"
DATE=$(date +%Y-%m-%d)
TIME=$(date +%H:%M:%S)
LOG_FILE="${3:-.history/${DATE}-commands.log}"

# Create log file if it doesn't exist
if [ ! -f "${LOG_FILE}" ]; then
    cat > "${LOG_FILE}" << EOF
# Command Log
# Date: ${DATE}

---

EOF
fi

# Log the command entry
{
    echo ""
    echo "## [${DATE} ${TIME}] - ${DESCRIPTION}"
    echo ""
    echo "**Command**:"
    echo '```bash'
    echo "${COMMAND}"
    echo '```'
    echo ""
    echo "**Output**:"
    echo '```'
} >> "${LOG_FILE}"

# Execute command and capture output
if eval "${COMMAND}" 2>&1 | tee -a "${LOG_FILE}"; then
    EXIT_CODE=0
    RESULT="Success"
else
    EXIT_CODE=$?
    RESULT="Failed (exit code: ${EXIT_CODE})"
fi

# Log the result
{
    echo '```'
    echo ""
    echo "**Result**: ${RESULT}"
    echo ""
    echo "---"
} >> "${LOG_FILE}"

echo ""
echo "✓ Logged to: ${LOG_FILE}"

exit ${EXIT_CODE}
