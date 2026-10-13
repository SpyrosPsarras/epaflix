#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
sha256sum reader/Dockerfile reader/Dockerfile.dockerignore reader/package.json reader/package-lock.json \
  reader/requirements.txt files/reader_bw_bridge.cjs files/vaultwarden_reader.py \
  files/vaultwarden_reader_http.py files/reader_startup.py | sha256sum | cut -c1-32
