#!/usr/bin/env bash
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)

if kustomize build "$here" | grep -q 'name: containerd-cleanup$'; then
    echo "FAIL: maintenance still renders the containerd-cleanup CronJob; kubelet image GC owns image cleanup (#1906)" >&2
    exit 1
fi

echo "PASS: containerd-cleanup is not rendered"
