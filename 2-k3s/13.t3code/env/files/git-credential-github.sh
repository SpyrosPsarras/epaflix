#!/usr/bin/env bash
# git credential helper. Answers only `get` for protocol=https host=github.com
# and only with the token from $GITHUB_TOKEN. Anything else gets nothing, so
# git falls through to the next helper or to an auth failure. Never writes.
set -euo pipefail
[[ ${1:-} == get ]] || exit 0
protocol='' host=''
while IFS='=' read -r key value; do
  [[ -n $key ]] || break
  case $key in
    protocol) protocol=$value ;;
    host) host=$value ;;
  esac
done
[[ $protocol == https && $host == github.com && -n ${GITHUB_TOKEN:-} ]] || exit 0
printf 'username=x-access-token\npassword=%s\n' "$GITHUB_TOKEN"
