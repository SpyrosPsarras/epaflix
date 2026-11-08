#!/usr/bin/env bash
# install-hooks.sh links this as ~/.local/bin/kubectl. Inside this repo's checkouts a
# kubectl call must target --context epaflix; anywhere else it runs the real kubectl as is.
set -euo pipefail

self="$(readlink -f "${BASH_SOURCE[0]}")"
real=""
IFS=: read -ra path_dirs <<<"$PATH"
for dir in "${path_dirs[@]}"; do
  if [ -x "$dir/kubectl" ] && [ "$(readlink -f "$dir/kubectl")" != "$self" ]; then
    real="$dir/kubectl"
    break
  fi
done
if [ -z "$real" ]; then
  echo "kubectl guard: no real kubectl found on PATH." >&2
  exit 127
fi

guarded="$(git -C "$(dirname "$self")" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
here="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
if [ -z "$guarded" ] || [ "$here" != "$guarded" ]; then
  exec "$real" "$@"
fi

case "${1:-}" in
  config | completion) exec "$real" "$@" ;;
esac

# kubectl keeps the last --context it is given, so the last one is what must be epaflix.
context=""
prev=""
for arg in "$@"; do
  [ "$arg" = "--" ] && break
  case "$arg" in
    --context=*) context="${arg#--context=}" ;;
    *) [ "$prev" = "--context" ] && context="$arg" ;;
  esac
  prev="$arg"
done

if [ "$context" = "epaflix" ]; then
  exec "$real" "$@"
fi
echo "kubectl guard: in this repo every kubectl call needs --context epaflix." >&2
exit 1
