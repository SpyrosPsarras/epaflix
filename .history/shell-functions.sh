#!/usr/bin/env bash

export LLM_HISTORY_LOG="${LLM_HISTORY_LOG:-.history/$(date +%Y-%m-%d)-auto.log}"

init_llm_log() {
    if [ ! -f "${LLM_HISTORY_LOG}" ]; then
        mkdir -p "$(dirname "${LLM_HISTORY_LOG}")"
        cat > "${LLM_HISTORY_LOG}" << EOF

---

EOF
    fi
}

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

llm-log-to() {
    export LLM_HISTORY_LOG=".history/${1}.log"
    init_llm_log
    echo "Logging to: ${LLM_HISTORY_LOG}"
}

llm-log-file() {
    echo "Current log: ${LLM_HISTORY_LOG}"
}
