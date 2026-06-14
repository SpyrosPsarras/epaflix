#!/usr/bin/env bash
# Pre-commit dispatcher: runs every .github/hooks/check-*.sh in sorted order
# and aborts the commit if any check exits non-zero. Add a new check by
# dropping a check-<name>.sh next to this file — no installer change needed.
set -euo pipefail

# Resolve through the .git/hooks symlink to this script's real directory,
# otherwise the glob below would scan .git/hooks (which holds no checks).
self="${BASH_SOURCE[0]}"
self="$(readlink -f "$self" 2>/dev/null || echo "$self")"
here="$(cd "$(dirname "$self")" && pwd)"

fail=0
for check in "$here"/check-*.sh; do
  [ -e "$check" ] || continue
  if ! bash "$check"; then
    fail=1
  fi
done

exit $fail
