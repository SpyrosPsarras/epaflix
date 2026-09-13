#!/usr/bin/env bash
# Dedicated runtime identity, restricted on the LXC to the vault MCP command.
set -euo pipefail
credentials=${T3_CREDENTIALS_DIR:-/run/t3-credentials}
[[ -r $credentials/identity && -r $credentials/known_hosts ]] || {
  echo 'KeePass runtime SSH identity or trusted host key is missing' >&2
  exit 1
}
exec ssh -T -F /dev/null -o BatchMode=yes -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes -o ConnectTimeout=10 \
  -o "UserKnownHostsFile=$credentials/known_hosts" \
  -i "$credentials/identity" "${T3_KEEPASS_TARGET:-spyros@192.168.10.240}" \
  /usr/local/bin/keepass-mcp
