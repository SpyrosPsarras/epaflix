#!/usr/bin/env bash
set -euo pipefail

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
