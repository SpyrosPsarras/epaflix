#!/usr/bin/env bash
# Pre-commit hook (#659): fail if .a5c/processes/ has untracked files.
#
# Root cause of the recurring drift: the path-scoped babysitter git flow
# never runs `git add .a5c/processes/` for its own process definitions, so a
# real work product sits on disk, untracked, until someone notices (143 of
# 154 files, PR #110/#111 swept it once and it fully regrew). Since these
# files are no longer gitignored (see .gitignore), any left uncommitted just
# show up as plain untracked files - this hook turns that into a blocked
# commit instead of a silent gap.
set -euo pipefail

untracked=$(git status --porcelain -- .a5c/processes/ | grep '^??' | awk '{print $2}' || true)

if [ -n "$untracked" ]; then
  echo "ERROR: untracked files under .a5c/processes/ (#659 - these silently vanish from git history if left uncommitted):" >&2
  echo "$untracked" >&2
  echo "  git add them, or delete if genuinely scratch." >&2
  exit 1
fi
echo "No untracked files under .a5c/processes/."
