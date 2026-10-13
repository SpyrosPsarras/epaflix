#!/usr/bin/env bash
# Content address for the public image inputs, independent of Git commit IDs.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"
variant=${1:-mixed}
case "$variant" in mixed|homelab|work) ;; *) exit 1 ;; esac
{
printf '%s\n' "$variant"
sha256sum env/Dockerfile env/Dockerfile.dockerignore env/tools/os-packages.txt \
  env/tools/install-cluster-tools.sh env/tools/package.json env/tools/package-lock.json \
  one/files/vault-restricted.py one/files/hub_services.py env/files/vault-restricted.sh \
  versions.env
} | sha256sum | cut -c1-32
