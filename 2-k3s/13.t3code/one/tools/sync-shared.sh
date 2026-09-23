#!/usr/bin/env bash
# Copy the inputs this overlay shares with ../env. Kustomize refuses to read
# above its root, so the copies live here. Run after editing the sources in
# env/ or files/. `--check` exits 1 on drift.
#
# Owned by this overlay, NOT synced: files/entrypoint.sh (three homes,
# T3_PROJECT_DIR), files/searxng-mcp.py (pod has no `mcp` package) and
# files/opencode-fast-version.sh.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
pairs=(
  "$here/../env/tools/package.json:$here/tools/package.json"
  "$here/../env/tools/package-lock.json:$here/tools/package-lock.json"
  "$here/../env/files/private-config.py:$here/files/private-config.py"
  "$here/../env/files/git-credential-github.sh:$here/files/git-credential-github.sh"
  "$here/../env/files/keepass-remote.sh:$here/files/keepass-remote.sh"
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
