#!/usr/bin/env bash
# Shell functions for automatic command logging
# Add to your ~/.zshrc or ~/.bashrc:
#   source /path/to/this/file

# Set the history log file for the current session
export LLM_HISTORY_LOG="${LLM_HISTORY_LOG:-.history/$(date +%Y-%m-%d)-auto.log}"

# Initialize log file with header if it doesn't exist
init_llm_log() {
    if [ ! -f "${LLM_HISTORY_LOG}" ]; then
        mkdir -p "$(dirname "${LLM_HISTORY_LOG}")"
        cat > "${LLM_HISTORY_LOG}" << EOF
# Auto-logged Commands
# Date: $(date +%Y-%m-%d)
# Started: $(date +%Y-%m-%d\ %H:%M:%S)

---

EOF
    fi
}

# Log a command with description
# Usage: llm-log "Description" "command"
llm-log() {
    init_llm_log

    local description="$1"
    local command="$2"
    local timestamp=$(date +%Y-%m-%d\ %H:%M:%S)

    {
        echo ""
        echo "## [${timestamp}] - ${description}"
        echo ""
        echo "**Command**:"
        echo '```bash'
        echo "${command}"
        echo '```'
        echo ""
        echo "**Output**:"
        echo '```'
    } >> "${LLM_HISTORY_LOG}"

    # Execute and capture
    if eval "${command}" 2>&1 | tee -a "${LLM_HISTORY_LOG}"; then
        local result="Success"
    else
        local result="Failed (exit code: $?)"
    fi

    {
        echo '```'
        echo ""
        echo "**Result**: ${result}"
        echo ""
        echo "---"
    } >> "${LLM_HISTORY_LOG}"

    echo "✓ Logged to: ${LLM_HISTORY_LOG}"
}

# Quick log function - logs last command
# Usage: Run a command, then type: llm-log-last "Description"
llm-log-last() {
    init_llm_log

    local description="$1"
    local last_cmd=$(fc -ln -1 | sed 's/^[[:space:]]*//')
    local timestamp=$(date +%Y-%m-%d\ %H:%M:%S)

    {
        echo ""
        echo "## [${timestamp}] - ${description}"
        echo ""
        echo "**Command**:"
        echo '```bash'
        echo "${last_cmd}"
        echo '```'
        echo ""
        echo "**Note**: Output not captured (logged retroactively)"
        echo ""
        echo "---"
    } >> "${LLM_HISTORY_LOG}"

    echo "✓ Logged to: ${LLM_HISTORY_LOG}"
}

# Set log file for current session
# Usage: llm-log-to "2026-02-14-my-session"
llm-log-to() {
    export LLM_HISTORY_LOG=".history/${1}.log"
    init_llm_log
    echo "Logging to: ${LLM_HISTORY_LOG}"
}

# Show current log file
llm-log-file() {
    echo "Current log: ${LLM_HISTORY_LOG}"
}
