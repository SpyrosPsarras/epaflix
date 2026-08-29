#!/bin/sh
set -eu

RC=/etc/airvpn/bluetit.rc
FRAGMENT=${BLUETIT_CONFIG:-/config/bluetit.conf}

cp /etc/airvpn/bluetit.rc.shipped "$RC"

{
  echo ""
  echo "# --- appended by entrypoint-render.sh, do not edit by hand ---"
  if [ -f "$FRAGMENT" ]; then
    cat "$FRAGMENT"
  fi
  if [ -n "${AIRVPN_USERNAME:-}" ]; then
    printf 'airusername                 %s\n' "$AIRVPN_USERNAME"
  fi
  if [ -n "${AIRVPN_PASSWORD:-}" ]; then
    printf 'airpassword                 %s\n' "$AIRVPN_PASSWORD"
  fi
} >> "$RC"

chmod 0600 "$RC"
