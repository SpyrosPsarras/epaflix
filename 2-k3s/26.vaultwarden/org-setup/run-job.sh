#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 render-job.py | kubectl create -f - -o name
