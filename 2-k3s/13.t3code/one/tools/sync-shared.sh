#!/usr/bin/env bash
# Copy the inputs this overlay shares with ../env and 23.mcp-hub. Kustomize
# refuses to read above its root, so the copies live here. Run after editing
# the sources in env/, files/ or 23.mcp-hub/tools/hub_clients.py. `--check`
# exits 1 on drift.
#
# Owned by this overlay, NOT synced: files/entrypoint.sh (three homes, MCP hub
# registration, T3_PROJECT_DIR) and files/opencode-fast-version.sh.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
pairs=(
  "$here/../env/tools/package.json:$here/tools/package.json"
  "$here/../env/tools/package-lock.json:$here/tools/package-lock.json"
  "$here/../env/files/private-config.py:$here/files/private-config.py"
  "$here/../env/files/git-credential-github.sh:$here/files/git-credential-github.sh"
  "$here/../../23.mcp-hub/tools/hub_clients.py:$here/files/hub_clients.py"
  "$here/../files/cliproxy-models.js:$here/files/cliproxy-models.js"
)
rc=0
for pair in "${pairs[@]}"; do
  src=${pair%%:*}; dst=${pair##*:}
  if [[ ${1:-} == --check ]]; then
    cmp -s "$src" "$dst" || { echo "drift: $dst" >&2; rc=1; }
  else
    cp -p "$src" "$dst"
  fi
done
exit $rc
