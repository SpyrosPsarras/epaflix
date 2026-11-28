#!/usr/bin/env bash
set -euo pipefail
export CLIPROXYAPI_BASE_URL="${ANTHROPIC_BASE_URL%/}/v1"
export CLIPROXYAPI_API_KEY="$ANTHROPIC_AUTH_TOKEN"
export CLIPROXYAPI_PROVIDER_NAME=cliproxy
export PI_MCP_EXT_INCLUDE=skills
export PI_MCP_EXT_SKILL_AUTO_APPROVE=skills
unset ANTHROPIC_AUTH_TOKEN
exec /tools/node_modules/.bin/pi "$@"
