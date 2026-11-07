#!/usr/bin/env bash
# Renovate post-upgrade task: rebuild package-lock.json from package.json with the npm the image uses.
# npm keeps a git dependency's old commit unless its spec is passed again, so git specs are re-passed.
set -euo pipefail
cd "$(dirname "$0")"
mapfile -t git_deps < <(node -e '
for (const [name, spec] of Object.entries(require("./package.json").dependencies))
  if (/^(github:|git[+:])/.test(spec)) console.log(`${name}@${spec}`)')
npx --yes npm@10.9.4 install --package-lock-only --ignore-scripts --no-audit --no-fund "${git_deps[@]}"
