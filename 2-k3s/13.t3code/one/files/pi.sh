#!/usr/bin/env bash
set -u
export CLIPROXYAPI_BASE_URL="${ANTHROPIC_BASE_URL%/}/v1"
export CLIPROXYAPI_API_KEY="$ANTHROPIC_AUTH_TOKEN"
export CLIPROXYAPI_PROVIDER_NAME=cliproxy
export JEV_GUARD_CONFIG=/run/jev-guard/config.json
export JEV_GUARD_ASK_SCORE=3 JEV_GUARD_ASK_P=1
export JEV_GUARD_SKIP_TOOLS=mcp__vaultwarden__vault_add,mcp__vaultwarden__vault_attach,mcp__vaultwarden__vault_attachment,mcp__vaultwarden__vault_get,mcp__vaultwarden__vault_list,mcp__vaultwarden__vault_trash,mcp__vaultwarden__vault_update
export JEV_GUARD_SKIP_SCAN="$JEV_GUARD_SKIP_TOOLS"
unset ANTHROPIC_AUTH_TOKEN
exec /tools/node_modules/.bin/pi "$@"
