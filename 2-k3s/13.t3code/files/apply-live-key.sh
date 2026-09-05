#!/usr/bin/env bash
# Replaces ANTHROPIC_AUTH_TOKEN in the guest env file with the value from
# /tmp/.ompkey (0600, removed after), then restarts the T3 service.
set -euo pipefail
TOKEN=$(cat /tmp/.ompkey)
q=${TOKEN//\'/\'\'}
sed -i "s|^ANTHROPIC_AUTH_TOKEN=.*|ANTHROPIC_AUTH_TOKEN='${q}'|" /etc/t3code/t3code.env
rm -f /tmp/.ompkey
grep -c "^ANTHROPIC_AUTH_TOKEN" /etc/t3code/t3code.env >/dev/null
UID_=$(id -u spyros)
sudo -iu spyros env XDG_RUNTIME_DIR=/run/user/$UID_ systemctl --user restart t3code.service
echo "env updated + service restarted"