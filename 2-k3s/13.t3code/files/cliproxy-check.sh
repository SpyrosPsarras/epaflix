#!/usr/bin/env bash
set -euo pipefail
set -a; . /etc/t3code/t3code.env; set +a
echo "base url: $ANTHROPIC_BASE_URL"
curl -s -o /dev/null -w "root status: %{http_code}\n" --max-time 10 https://cliproxy.epaflix.com/
echo "--- /v1/messages test ---"
curl -s --max-time 60 -X POST https://cliproxy.epaflix.com/v1/messages \
  -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-sonnet-5","max_tokens":16,"messages":[{"role":"user","content":"Reply with exactly: CLIPROXY_OK"}]}' | head -c 500
echo
