#!/usr/bin/env bash
# Render a kustomize tier into a private temp file, run a check, shred on exit (#1060).
#
# A local render of a ksops tier is a decrypted copy of every Secret in that
# tier. Redirecting one to a file under /tmp leaves plaintext credentials
# behind until reboot (see #602 for what that cost). This wrapper owns the
# file: private mode, and shred -u on EXIT, including on failure.
#
# Usage: render-tier.sh <tier-dir> [check [args...]]
#   No check: counts Secret documents in the render, proving the file was
#   credential-bearing without printing any value.
#   A check runs with the render file appended as its last argument; $RENDER_FILE
#   is also exported for checks that need to pass the path around.
#
#   render-tier.sh 2-k3s/10.observability grep -c smtp_auth_password
#
# Rendering needs the ksops exec plugin and the SOPS age key in the environment.
set -euo pipefail
umask 077

usage() {
  echo "usage: $0 <tier-dir> [check [args...]]" >&2
  exit 2
}

(( $# >= 1 )) || usage
tier=$1
shift
[[ -d $tier ]] || { echo "not a directory: $tier" >&2; exit 2; }

render=$(mktemp)
trap 'shred -u "$render"' EXIT

kustomize build --enable-helm --enable-alpha-plugins --enable-exec "$tier" >"$render"
export RENDER_FILE=$render

if (( $# == 0 )); then
  count=$(grep -c '^kind: Secret$' "$render" || true)
  echo "$count Secret(s) decrypted into the render of $tier."
  echo "The file was credential-bearing and is now shredded."
else
  "$@" "$render"
fi
