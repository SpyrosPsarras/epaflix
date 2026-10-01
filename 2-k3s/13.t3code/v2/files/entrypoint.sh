#!/usr/bin/env bash
set -euo pipefail
python3 /v2/provision.py --home "$HOME" --manifest /v2-private/bundle.json
mkdir -p "$HOME/.local/bin"
if [[ ! -e "$HOME/.local/bin/kubelogin" ]]; then
  ln -s /usr/local/bin/kubelogin "$HOME/.local/bin/kubelogin"
fi
export T3_V2_SUPERVISED_START=1
# Managed v1 startup seeds provider settings, plugins, instructions and credentials.
# The /v2/t3 launcher supervises its final exec without changing the shared script.
exec bash /scripts/entrypoint.sh
