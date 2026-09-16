#!/usr/bin/env bash
# Stubs hostname via PATH. No Proxmox state is changed.
set -euo pipefail
HOOK=$(cd "$(dirname "$0")" && pwd)/pin-vm-host.sh
stub=$(mktemp -d); trap 'rm -rf "$stub"' EXIT
fake_host() { printf '#!/bin/sh\necho %s\n' "$1" >"$stub/hostname"; chmod +x "$stub/hostname"; }
run() { PATH="$stub:$PATH" bash "$HOOK" "$@"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

fake_host takaros
run 1062 pre-start            || fail "1062 must start on takaros"
run 1065 pre-start 2>/dev/null && fail "1065 must not start on takaros"
run 9999 pre-start 2>/dev/null && fail "unknown vmid must be refused"
run 1065 post-start           || fail "non pre-start phases must exit 0"
run 1065 pre-stop             || fail "non pre-start phases must exit 0"
PVE_MIGRATED_FROM=evanthoulaki run 1065 pre-start 2>"$stub/err" && fail "migration target must be refused"
grep -q "migration from evanthoulaki" "$stub/err" || fail "error must name the source host"

fake_host evanthoulaki
run 1065 pre-start            || fail "1065 must start on evanthoulaki"
run 1062 pre-start 2>/dev/null && fail "1062 must not start on evanthoulaki"

fake_host other-node
run 1062 pre-start 2>/dev/null && fail "unknown host must be refused"
echo "ok: pin-vm-host refuses wrong host and unknown vmid; allows own host and other phases"
