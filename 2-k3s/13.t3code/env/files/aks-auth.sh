#!/usr/bin/env bash
# Kubeconfig exec helper, or explicit device-code login. The t3code pod is the
# only T3 server, so it runs the central helper locally.
set -euo pipefail
action=${1:-token}
[[ $# -le 1 && ( $action == token || $action == login ) ]] || {
  echo 'Usage: aks-auth [token|login]' >&2; exit 2;
}
exec "$HOME/.local/bin/aks-auth-central" "$action"
