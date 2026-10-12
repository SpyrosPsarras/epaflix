#!/usr/bin/env bash
set -euo pipefail
export T3_RUNTIME_VARIANT=homelab
exec bash "$(dirname "$0")/isolated-smoke.sh" "$@"
