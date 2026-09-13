#!/usr/bin/env bash
# Exercise the real updater with a scratch repository and stubbed OS commands.
set -euo pipefail
SRC=$(cd "$(dirname "$0")/../.." && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cp -r "$SRC" "$tmp/repo"
# Redirect external state only in the test copy, including optional integrations.
sed -i "s|^STAMP=.*|STAMP=$tmp/stamp|; s|/var/lib/t3code|$tmp/lib|g; s|/etc/t3code|$tmp/etc|g; s|/opt/keepass-mcp|$tmp/keepass|g" "$tmp/repo/update.sh"
mkdir -p "$tmp/bin"
for c in npm curl tar sha256sum apt-get sudo; do
  printf '#!/bin/sh\necho "STUB %s $*" >>"%s/calls"\n' "$c" "$tmp" >"$tmp/bin/$c"
  chmod +x "$tmp/bin/$c"
done
printf '#!/bin/sh\nexit 1\n' >"$tmp/bin/id"
chmod +x "$tmp/bin/id"
printf '#!/bin/sh\ncase "$*" in *"/usr/local/bin/"*) exit 0;; esac\nexec /usr/bin/install "$@"\n' >"$tmp/bin/install"
chmod +x "$tmp/bin/install"
export PATH=$tmp/bin:$PATH T3_USER=nobody-t3env
fail() { echo "FAIL: $*" >&2; exit 1; }
run() { : >"$tmp/calls"; bash "$tmp/repo/update.sh"; }
pin() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["dependencies"][sys.argv[2]])' "$tmp/repo/env/tools/package.json" "$1"; }
run >/dev/null
grep -q "STUB npm install -g t3@$(pin t3) " "$tmp/calls" || fail "npm pins must come from package.json"
grep -q "^T3_VERSION=$(pin t3)$" "$tmp/stamp" || fail "stamp must carry T3_VERSION"
[[ $(stat -c %a "$tmp/stamp") == 644 ]] || fail "stamp mode"
out=$(run)
grep -q 'already at pinned versions' <<<"$out" || fail "same pins must no-op"
[[ ! -s $tmp/calls ]] || fail "no-op must not install"
python3 - "$tmp/repo/env/tools/package.json" <<'PY'
import json, sys
p = json.load(open(sys.argv[1]))
p["dependencies"]["t3"] = "9.9.9"
with open(sys.argv[1], "w") as f: json.dump(p, f)
PY
run >/dev/null
grep -q 'STUB npm install -g t3@9.9.9 ' "$tmp/calls" || fail "npm bump must reinstall"
grep -q '^T3_VERSION=9.9.9$' "$tmp/stamp" || fail "new pin not stamped"
sed -i 's/^SOPS_VERSION=[^ ]*/SOPS_VERSION=0.0.1/' "$tmp/repo/versions.env"
run >/dev/null
grep -q 'sops-v0.0.1' "$tmp/calls" || fail "cluster tool bump must reinstall"
printf 'T3_VERSION=0.0.1\n' >"$tmp/stamp"
out=$(run)
grep -q 'already at pinned' <<<"$out" && fail "legacy stamp must trigger migration"
echo 'ok: shared npm pins, no-op, npm/tool updates and legacy stamp migration'
