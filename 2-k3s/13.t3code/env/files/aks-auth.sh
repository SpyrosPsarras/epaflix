#!/usr/bin/env bash
# Kubeconfig exec helper, or explicit central device-code login.
set -euo pipefail
action=${1:-token}
[[ $# -le 1 && ( $action == token || $action == login ) ]] || {
  echo 'Usage: aks-auth [token|login]' >&2; exit 2;
}
credentials="$HOME/.local/share/t3-aks-auth"
exec ssh -T -F /dev/null -o BatchMode=yes -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes -o ConnectTimeout=10 \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -o "UserKnownHostsFile=$credentials/known_hosts" \
  -i "$credentials/identity" spyros@192.168.10.240 "$action"
