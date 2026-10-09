#!/usr/bin/env bash
set -u
export CLIPROXYAPI_BASE_URL="${ANTHROPIC_BASE_URL%/}/v1"
export CLIPROXYAPI_API_KEY="$ANTHROPIC_AUTH_TOKEN"
export CLIPROXYAPI_PROVIDER_NAME=cliproxy
unset ANTHROPIC_AUTH_TOKEN
exec /tools/node_modules/.bin/pi "$@"
