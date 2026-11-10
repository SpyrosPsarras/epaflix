#!/usr/bin/env bash
set -euo pipefail

shard="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/unittest-shard.py"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

cat >"$tmp/sample.test.py" <<'EOF'
import os, unittest
def setUpModule():
    if os.environ.get("BREAK") == "module": raise RuntimeError("module setup")
class A(unittest.TestCase):
    def test_a1(self): pass
    def test_a2(self): self.assertNotEqual(os.environ.get("BREAK"), "a2")
class B(unittest.TestCase):
    def test_b1(self): pass
if __name__ == '__main__':
    raise SystemExit("must not run the script's main block")
EOF

ran=$(for i in 0 1; do python3 "$shard" "$tmp/sample.test.py" "$i" 2 2>&1 || fail "shard $i failed"; done | grep -oE '^test_[a-z0-9]+' | sort | tr '\n' ' ')
[[ $ran == "test_a1 test_a2 test_b1 " ]] || fail "shards did not run every test exactly once: $ran"

if BREAK=a2 python3 "$shard" "$tmp/sample.test.py" 1 2 >/dev/null 2>&1; then fail "a failing test passed"; fi
if BREAK=module python3 "$shard" "$tmp/sample.test.py" 0 2 >/dev/null 2>&1; then fail "a failing setUpModule passed"; fi
if python3 "$shard" "$tmp/sample.test.py" 3 4 >/dev/null 2>&1; then fail "an empty shard passed"; fi

echo "PASS unittest-shard: shards cover every test once, and failures, module setup errors and empty shards fail"
