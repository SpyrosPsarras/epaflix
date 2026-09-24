#!/usr/bin/env bash
# Write the hub bearer token to its three homes: the laptop key file and the
# two sops-encrypted Secrets (hub side, t3code client side). Each Secret gets
# a fresh plaintext revision annotation, which the kustomizations copy into
# the pod templates. After merging, ArgoCD rolls mcp-hub by itself; t3code
# syncs manually (app-t3code.yaml), so sync it or T3 keeps the old token.
#
#   rotate-token.sh          new random token
#   rotate-token.sh --reuse  re-encrypt the token already in KEY_FILE
#
# The key file is replaced last, only after both Secrets encrypted. The
# laptop gets 401 from the moment it changes until the hub has rolled.
# Needs sops and the age recipient in .sops.yaml. The token is never printed
# and never on a command line. Linux (GNU mktemp suffix templates).
set -euo pipefail
case ${1:-} in ''|--reuse) ;; *) echo "usage: $0 [--reuse]" >&2; exit 2 ;; esac
cd "$(git rev-parse --show-toplevel)"
KEY_FILE=${KEY_FILE:-$HOME/.config/opencode/mcp-hub.key}
umask 077
mkdir -p "$(dirname "$KEY_FILE")"
new_key=$(mktemp "$KEY_FILE.XXXXXX")
tmps=("$new_key")
trap 'rm -f "${tmps[@]}"' EXIT

if [[ ${1:-} == --reuse ]]; then
  [[ -s $KEY_FILE ]] || { echo "no token in $KEY_FILE" >&2; exit 1; }
  cp "$KEY_FILE" "$new_key"
else
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))' >"$new_key"
fi

revision=$(date -u +%Y%m%dT%H%M%SZ)
declare -A staged
for spec in 2-k3s/23.mcp-hub/mcp-hub-token.enc.yaml:mcp-hub-token:mcp-hub \
            2-k3s/13.t3code/one/mcp-hub-client.enc.yaml:mcp-hub-client:t3code; do
  IFS=: read -r path name ns <<<"$spec"
  dir=$(dirname "$path")
  tmp=$(mktemp "$dir/.rotate-XXXXXX.enc.yaml")
  tmps+=("$tmp")
  python3 - "$new_key" "$name" "$ns" "$revision" >"$tmp" <<'PY'
import json, sys
key_file, name, ns, rev = sys.argv[1:]
token = open(key_file).read().strip()
print(f"""apiVersion: v1
kind: Secret
metadata:
  name: {name}
  namespace: {ns}
  annotations:
    mcp-hub.epaflix.com/revision: "{rev}"
type: Opaque
stringData:
  token: {json.dumps(token)}""")
PY
  (cd "$dir" && sops -e -i "$(basename "$tmp")")
  chmod 0644 "$tmp"
  staged[$path]=$tmp
done

# Both encrypted: publish all three.
for path in "${!staged[@]}"; do
  mv "${staged[$path]}" "$path"
  echo "wrote $path"
done
mv "$new_key" "$KEY_FILE"
echo "laptop key: $KEY_FILE. Commit both Secrets, merge, then sync t3code in ArgoCD."
