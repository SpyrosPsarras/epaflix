#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import yaml' >/dev/null 2>&1; then
  echo "ERROR: check-sops-encrypted requires python3 and PyYAML" >&2
  exit 1
fi

exec python3 "$here/check_sops_encrypted.py" "$@"
