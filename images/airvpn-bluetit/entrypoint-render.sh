#!/bin/sh
# Renders /etc/airvpn/bluetit.rc = shipped template + our fragment + credentials.
#
# The shipped file carries bootserver/rsaexponent/rsamodulus, which are REQUIRED
# for AirVPN support to work at all. Always start from the pristine copy so a
# container restart cannot append twice.
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
  # Bluetit's own rc parser rejects a directive with no value at all (verified
  # against the real binary: an empty airusername/airpassword line makes it
  # fail with "Error while parsing ... file. Exiting."). Omit them when unset
  # so a credential-less boot (e.g. our own safety test) still starts.
  # NB: under `set -e`, `[ -n "$x" ] && cmd` exits the script when the test is
  # false - use if/then instead (see test.sh's own warning about this).
  if [ -n "${AIRVPN_USERNAME:-}" ]; then
    printf 'airusername                 %s\n' "$AIRVPN_USERNAME"
  fi
  if [ -n "${AIRVPN_PASSWORD:-}" ]; then
    printf 'airpassword                 %s\n' "$AIRVPN_PASSWORD"
  fi
} >> "$RC"

chmod 0600 "$RC"
