#!/usr/bin/env bash
# Content address for the public image inputs, independent of Git commit IDs.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"
sha256sum env/Dockerfile env/Dockerfile.dockerignore env/tools/os-packages.txt \
  env/tools/install-cluster-tools.sh env/tools/package.json env/tools/package-lock.json \
  versions.env | sha256sum | cut -c1-32
