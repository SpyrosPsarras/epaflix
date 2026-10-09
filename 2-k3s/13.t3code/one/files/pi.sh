#!/usr/bin/env bash
set -u
export CLIPROXYAPI_BASE_URL="${ANTHROPIC_BASE_URL%/}/v1"
export CLIPROXYAPI_API_KEY="$ANTHROPIC_AUTH_TOKEN"
export CLIPROXYAPI_PROVIDER_NAME=cliproxy
timeout 5 python3 /scripts/pi-setup.py aliases ~ || echo 'pi-setup: alias refresh failed or timed out' >&2
unset ANTHROPIC_AUTH_TOKEN
exec /tools/node_modules/.bin/pi "$@"
