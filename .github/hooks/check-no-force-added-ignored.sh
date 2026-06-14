#!/usr/bin/env bash
# Pre-commit hook: refuse to commit any staged file that .gitignore matches.
#
# A normal `git add` already skips ignored files, but `git add -f` bypasses
# .gitignore — and once a path is staged, plain `git check-ignore` no longer
# reports it. `--no-index` evaluates purely against the ignore patterns, so it
# catches force-added ignored files (e.g. .github/instructions/secrets.yml,
# .history/*.md) before they can land in a commit.
#
# This is the durable guard behind the project rule "never git add -f anything
# .gitignore matches". Wire up via .github/hooks/install-hooks.sh (one-shot).
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
