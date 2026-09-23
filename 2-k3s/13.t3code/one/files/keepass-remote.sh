#!/usr/bin/env bash
# Vault MCP over stdio: starts keepass_mcp.py in syncthing/keepass
# (15.syncthing/keepass.yaml). Uses the pod ServiceAccount, whose only right is
# that exec, never the kubeconfig in $HOME.
set -euo pipefail
sa=/var/run/secrets/kubernetes.io/serviceaccount
[[ -r $sa/token ]] || { echo 'no ServiceAccount token for the vault bridge' >&2; exit 1; }
exec /usr/bin/kubectl --kubeconfig=/dev/null --server=https://kubernetes.default.svc \
  --certificate-authority="$sa/ca.crt" --token="$(<"$sa/token")" \
  -n syncthing exec -i deploy/keepass -- python /app/keepass_mcp.py
