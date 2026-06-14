#!/usr/bin/env bash
# One-shot installer: symlinks repo hooks into .git/hooks/.
# Idempotent. Run once per fresh clone.
set -euo pipefail
cd "$(dirname "$0")/../.."  # repo root
hooks_dir=".git/hooks"
src_dir=".github/hooks"

mkdir -p "$hooks_dir"

ln -sf "../../$src_dir/run-pre-commit.sh" "$hooks_dir/pre-commit"
chmod +x "$hooks_dir/pre-commit"
chmod +x "$src_dir"/check-*.sh "$src_dir/run-pre-commit.sh"

echo "Installed pre-commit hook: $hooks_dir/pre-commit -> $src_dir/run-pre-commit.sh"
echo "  runs: $(cd "$src_dir" && echo check-*.sh)"
