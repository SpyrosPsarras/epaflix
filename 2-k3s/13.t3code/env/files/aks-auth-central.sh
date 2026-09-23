#!/usr/bin/env bash
# AKS token/login helper. Takes the action from $1 (or SSH_ORIGINAL_COMMAND);
# never evaluates it as shell code.
set -euo pipefail
umask 077
export AZURE_CONFIG_DIR="$HOME/.azure"
tenant=9d4e1d19-7bb4-460c-b762-c2d9d38ea759
mkdir -p "$AZURE_CONFIG_DIR"
login() {
  AZURE_CORE_LOGIN_EXPERIENCE_V2=off az login --tenant "$tenant" \
    --use-device-code --output none >&2
}
token() {
  "$HOME/.local/bin/kubelogin" get-token --login azurecli \
    --tenant-id "$tenant" --server-id 6dae42f8-4368-4678-94ff-3960e28e3630
}
case "${SSH_ORIGINAL_COMMAND:-${1:-token}}" in
  token)
    exec 9>"$AZURE_CONFIG_DIR/t3-aks-auth.lock"
    flock -w 900 9 || { echo 'Timed out waiting for central Azure authentication.' >&2; exit 1; }
    errors=$(mktemp "$AZURE_CONFIG_DIR/t3-aks-error.XXXXXX")
    trap 'rm -f "$errors"' EXIT
    if credential=$(token 2>"$errors"); then
      printf '%s\n' "$credential"
    else
      cat "$errors" >&2
      # Only authentication failures trigger login, not network or RBAC errors.
      if ! grep -Eqi 'az login|interaction_required|invalid_grant|AADSTS(50058|50076|50079|50173|70043|700082|700084)' "$errors"; then
        exit 1
      fi
      echo 'Starting Azure login. Complete the device-code prompt below; this request will then retry automatically.' >&2
      login
      credential=$(token)
      printf '%s\n' "$credential"
    fi
    ;;
  login)
    exec 9>"$AZURE_CONFIG_DIR/t3-aks-auth.lock"
    flock -n 9 || { echo 'Another central authentication request is running. Retry shortly.' >&2; exit 1; }
    # Device-code instructions go to the caller; no token or account JSON on stdout.
    login
    ;;
  *) echo 'Only token and login are allowed.' >&2; exit 2 ;;
esac
