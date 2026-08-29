#!/usr/bin/env bash
set -euo pipefail

fail=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  if git check-ignore -q --no-index -- "$f"; then
    echo "ERROR: $f is git-ignored but staged (force-added). Refusing to commit." >&2
    echo "       It is likely a secret or local-only file. Unstage it with:" >&2
    echo "         git restore --staged -- \"$f\"" >&2
    fail=1
  fi
done < <(git diff --cached --name-only --diff-filter=ACMR)

exit $fail
