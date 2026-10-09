#!/usr/bin/env bash
# Self-check without starting T3: credential helper filtering and npm lock pins.
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
HELPER=$DIR/git-credential-github.sh
fail() { echo "FAIL: $*" >&2; exit 1; }
export GITHUB_TOKEN=tok-selftest
ask() { printf 'protocol=%s\nhost=%s\n\n' "$2" "$3" | bash "$HELPER" "$1"; }
[[ $(ask get https github.com) == $'username=x-access-token\npassword=tok-selftest' ]] || fail 'github.com token'
for host in gitlab.com evil.github.com; do
  [[ -z $(ask get https "$host") ]] || fail 'host filtering'
done
[[ -z $(ask get http github.com) ]] || fail 'protocol filtering'
for operation in store erase; do
  [[ -z $(ask "$operation" https github.com) ]] || fail 'operation filtering'
done
[[ -z $(GITHUB_TOKEN='' ask get https github.com) ]] || fail 'empty token'
echo 'ok: credential helper filters host/protocol/operation'
python3 - "$DIR/../tools/package.json" "$DIR/../tools/package-lock.json" <<'PY'
import json, sys
want = json.load(open(sys.argv[1]))['dependencies']
lock = json.load(open(sys.argv[2]))['packages']
assert lock['']['dependencies'] == want
for name, version in want.items():
    if version.startswith('github:'):
        package = lock[f'node_modules/{name}']
        if name == 'superpowers':
            assert package['version'] == version.split('#v')[1]
            assert package['resolved'].startswith('git+ssh://git@github.com/obra/superpowers.git#')
        else:
            assert package['resolved'].endswith(version.split('#')[1])
    else:
        assert lock[f'node_modules/{name}']['version'] == version
PY
echo 'ok: package-lock.json matches package.json'
