#!/usr/bin/env bash
set -euo pipefail
startup_attempts=600
poll_interval_seconds=1
for ((attempt=0; attempt<startup_attempts; attempt++)); do
  if [[ -f /run/t3-vault/scope.json ]]; then
    status=0
    SSH_AUTH_SOCK=/run/t3-vault/ssh-agent.sock ssh-add -l >/dev/null 2>&1 || status=$?
    if ((status == 0 || status == 1)); then exit 0; fi
  fi
  sleep "$poll_interval_seconds"
done
exit 1
