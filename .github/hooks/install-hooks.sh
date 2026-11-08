#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."  # repo root (or worktree root)

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import yaml' >/dev/null 2>&1; then
  echo "ERROR: Secret validation requires python3 and PyYAML." >&2
  echo "Install the dependency from .github/hooks/requirements.txt, then retry." >&2
  exit 1
fi

hooks_dir="$(git rev-parse --git-path hooks)"
src_dir=".github/hooks"

mkdir -p "$hooks_dir"

ln -sf "../../$src_dir/run-pre-commit.sh" "$hooks_dir/pre-commit"
chmod +x "$hooks_dir/pre-commit"
chmod +x "$src_dir"/check-*.sh "$src_dir/run-pre-commit.sh"

echo "Installed pre-commit hook: $hooks_dir/pre-commit -> $src_dir/run-pre-commit.sh"
echo "  runs: $(cd "$src_dir" && echo check-*.sh)"

# Link the main checkout's copy: T3 worktrees, and their copies, get deleted.
main_checkout="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
chmod +x "$src_dir/kubectl-guard.sh"
mkdir -p "$HOME/.local/bin"
ln -sfn "$main_checkout/$src_dir/kubectl-guard.sh" "$HOME/.local/bin/kubectl"
echo "Installed kubectl guard: $HOME/.local/bin/kubectl -> $main_checkout/$src_dir/kubectl-guard.sh"
echo "  ~/.local/bin must come before the real kubectl on PATH."
if [ ! -e "$main_checkout/$src_dir/kubectl-guard.sh" ]; then
  echo "WARNING: the main checkout has no $src_dir/kubectl-guard.sh yet, so kubectl is unguarded." >&2
  echo "         Pull the change into $main_checkout." >&2
fi
