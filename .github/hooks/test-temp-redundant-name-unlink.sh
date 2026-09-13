#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
script="$repo_root/0-truenas/scripts/temp-redundant-name-unlink.py"
tmp="$(mktemp -d)"
output="$(mktemp)"
server_pid=""
cleanup() {
  if [ -n "$server_pid" ]; then kill "$server_pid" 2>/dev/null || true; fi
  rm -rf "$tmp"
  rm -f "$output"
}
trap cleanup EXIT

pool="$tmp/pool"
temp="$pool/downloads/temp"

# A stand-in qBittorrent. /torrents/info serves torrents.json; once `flip` is
# armed it serves it once more and then switches to torrents-bad.json, so a
# torrent enters `error` after the run has read its baseline. Login fails while
# `reject` exists. Once `grow` is armed the stub also hardlinks a new redundant
# pair into the pool from the second call onward, which only the second batch's
# own filesystem walk can see.
cat >"$tmp/qbt.py" <<'PY'
import http.server
import json
import os
import sys

STATE = os.environ["QBT_STATE"]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, body):
        body = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.startswith("/api/v2/auth/login"):
            self.reply("Fails." if os.path.exists(STATE + "/reject") else "Ok.")
        else:
            self.send_error(404)

    def do_GET(self):
        if not self.path.startswith("/api/v2/torrents/info"):
            self.send_error(404)
            return
        name = "torrents.json"
        if os.path.exists(STATE + "/flip"):
            if os.path.exists(STATE + "/flip-armed"):
                name = "torrents-bad.json"
            else:
                open(STATE + "/flip-armed", "w").close()
        if os.path.exists(STATE + "/grow"):
            if not os.path.exists(STATE + "/grow-armed"):
                open(STATE + "/grow-armed", "w").close()
            elif not os.path.exists(STATE + "/grow-done"):
                pool = os.environ["QBT_POOL"]
                os.link(pool + "/tvshows/late.mkv",
                        pool + "/downloads/temp/late.mkv")
                open(STATE + "/grow-done", "w").close()
        with open(STATE + "/" + name) as fh:
            self.reply(json.dumps(json.load(fh)))


server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
with open(STATE + "/port", "w") as fh:
    fh.write(str(server.server_address[1]))
sys.stderr.close()
server.serve_forever()
PY

cat >"$tmp/torrents.json" <<'JSON'
[
  {"hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "state": "stalledDL",
   "content_path": "/media/downloads/temp/live.mkv"},
  {"hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "state": "stalledUP",
   "content_path": "/media/downloads/tv-sonarr/other.mkv"}
]
JSON

cat >"$tmp/torrents-bad.json" <<'JSON'
[
  {"hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "state": "stalledDL",
   "content_path": "/media/downloads/temp/live.mkv"},
  {"hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "state": "error",
   "content_path": "/media/downloads/tv-sonarr/other.mkv"}
]
JSON

QBT_STATE="$tmp" QBT_POOL="$pool" python3 "$tmp/qbt.py" &
server_pid=$!
for _ in $(seq 1 50); do
  [ -s "$tmp/port" ] && break
  sleep 0.1
done
[ -s "$tmp/port" ] || { echo "stub qBittorrent never came up" >&2; exit 1; }
url="http://127.0.0.1:$(cat "$tmp/port")"

pass_count=0
fail_count=0

pass() {
  pass_count=$((pass_count + 1))
  printf 'ok - %s\n' "$1"
}

fail() {
  fail_count=$((fail_count + 1))
  printf 'not ok - %s\n' "$1" >&2
  if [ -s "$output" ]; then
    sed 's/^/  /' "$output" >&2
  fi
}

# One tree per case: the script unlinks, so cases cannot share a fixture.
#   s01e01 / s01e02  redundant - a temp name and a library name for one inode
#   live             redundant in shape, but torrent aaaa... still holds it
#   part.mkv.!qB     a real partial, its only name is under temp
#   a.mkv / b.mkv    one inode named twice inside temp, no library name
#   lonely.mkv       library only, never a candidate
seed_tree() {
  rm -rf "$pool"
  mkdir -p "$temp" "$pool/tvshows" "$pool/movies" "$pool/downloads/tv-sonarr"
  for name in s01e01 s01e02 live; do
    head -c 4096 /dev/zero >"$pool/tvshows/$name.mkv"
    ln "$pool/tvshows/$name.mkv" "$temp/$name.mkv"
  done
  head -c 2048 /dev/zero >"$temp/part.mkv.!qB"
  head -c 1024 /dev/zero >"$temp/a.mkv"
  ln "$temp/a.mkv" "$temp/b.mkv"
  head -c 512 /dev/zero >"$pool/movies/lonely.mkv"
}

run_script() {
  DATASET_ROOT="$pool" TEMP_DIR="$temp" QBT_ROOT="${QBT_ROOT_OVERRIDE:-/media}" \
    QBT_URL="$url" QBT_USERNAME=u QBT_PASSWORD=p BATCH="${BATCH:-10}" \
    python3 "$script" "$@" >"$output" 2>&1
}

survivors_intact() {
  [ -f "$temp/part.mkv.!qB" ] && [ -f "$temp/a.mkv" ] && [ -f "$temp/b.mkv" ] \
    && [ -f "$temp/live.mkv" ] && [ -f "$pool/movies/lonely.mkv" ] \
    && [ -f "$pool/tvshows/s01e01.mkv" ] && [ -f "$pool/tvshows/s01e02.mkv" ]
}

rc=0; python3 "$script" --selftest >"$output" 2>&1 || rc=$?
if [ "$rc" -eq 0 ] && grep -q 'selftest OK' "$output"; then
  pass "in-script selftest passes"
else
  fail "in-script selftest passes (rc=$rc)"
fi

seed_tree
rc=0; run_script || rc=$?
if [ "$rc" -eq 0 ] \
  && grep -q 'DRY RUN' "$output" \
  && grep -q "would unlink $temp/s01e01.mkv" "$output" \
  && grep -q "would unlink $temp/s01e02.mkv" "$output" \
  && ! grep -q "would unlink $temp/live.mkv" "$output" \
  && ! grep -q 'part.mkv' "$output" \
  && grep -q 'candidates: 2 inodes, 2 temp names, frees 0 bytes' "$output" \
  && [ -f "$temp/s01e01.mkv" ] && [ -f "$temp/s01e02.mkv" ] \
  && survivors_intact; then
  pass "dry run names only the redundant inodes and unlinks nothing"
else
  fail "dry run names only the redundant inodes and unlinks nothing (rc=$rc)"
fi

seed_tree
rc=0; run_script --apply || rc=$?
if [ "$rc" -eq 0 ] \
  && [ ! -e "$temp/s01e01.mkv" ] && [ ! -e "$temp/s01e02.mkv" ] \
  && [ "$(stat -c %h "$pool/tvshows/s01e01.mkv")" = "1" ] \
  && [ "$(stat -c %h "$temp/live.mkv")" = "2" ] \
  && survivors_intact \
  && grep -q 'unlinked 2 temp name(s), freed 0 bytes' "$output"; then
  pass "--apply drops both redundant temp names and keeps every library name"
else
  fail "--apply drops both redundant temp names and keeps every library name (rc=$rc)"
fi

seed_tree
rc=0; run_script --apply || rc=$?
rc=0; run_script --apply || rc=$?
if [ "$rc" -eq 0 ] && grep -q 'OK: nothing to unlink' "$output" \
  && survivors_intact; then
  pass "a second pass finds nothing and leaves the partials alone"
else
  fail "a second pass finds nothing and leaves the partials alone (rc=$rc)"
fi

seed_tree
rm -f "$pool/tvshows/s01e01.mkv" "$pool/tvshows/s01e02.mkv"
rc=0; run_script --apply || rc=$?
if [ "$rc" -eq 0 ] \
  && grep -q 'OK: nothing to unlink' "$output" \
  && [ -f "$temp/s01e01.mkv" ] && [ -f "$temp/s01e02.mkv" ]; then
  pass "a temp name with no library counterpart is never a candidate"
else
  fail "a temp name with no library counterpart is never a candidate (rc=$rc)"
fi

seed_tree
: >"$tmp/flip"
rc=0; BATCH=1 run_script --apply || rc=$?
remaining=0
for name in s01e01 s01e02; do
  if [ -e "$temp/$name.mkv" ]; then remaining=$((remaining + 1)); fi
done
rm -f "$tmp/flip" "$tmp/flip-armed"
if [ "$rc" -eq 1 ] \
  && grep -q 'ABORT: 1 torrent(s) entered missingFiles/error' "$output" \
  && grep -q '1 temp name(s) unlinked before the abort' "$output" \
  && [ "$remaining" -eq 1 ] \
  && survivors_intact; then
  pass "a torrent entering error stops the run after the current batch"
else
  fail "a torrent entering error stops the run after the current batch (rc=$rc, remaining=$remaining)"
fi

seed_tree
head -c 4096 /dev/zero >"$pool/tvshows/late.mkv"
: >"$tmp/grow"
rc=0; BATCH=1 run_script --apply || rc=$?
rm -f "$tmp/grow" "$tmp/grow-armed" "$tmp/grow-done"
if [ "$rc" -eq 0 ] \
  && grep -q 'unlinked 3 temp name(s)' "$output" \
  && [ ! -e "$temp/late.mkv" ] \
  && [ -f "$pool/tvshows/late.mkv" ] \
  && survivors_intact; then
  pass "a candidate that appears mid-run is picked up by the next batch"
else
  fail "a candidate that appears mid-run is picked up by the next batch (rc=$rc)"
fi

seed_tree
rc=0; QBT_ROOT_OVERRIDE=/nowhere run_script --apply || rc=$?
if [ "$rc" -eq 2 ] \
  && grep -q 'QBT_ROOT mapping is wrong' "$output" \
  && [ -f "$temp/s01e01.mkv" ] && survivors_intact; then
  pass "a path mapping that matches no torrent exits 2 instead of unlinking blind"
else
  fail "a path mapping that matches no torrent exits 2 instead of unlinking blind (rc=$rc)"
fi

seed_tree
: >"$tmp/reject"
rc=0; run_script --apply || rc=$?
rm -f "$tmp/reject"
if [ "$rc" -eq 2 ] \
  && grep -q 'login rejected' "$output" \
  && [ -f "$temp/s01e01.mkv" ] && survivors_intact; then
  pass "a rejected qBittorrent login exits 2 and unlinks nothing"
else
  fail "a rejected qBittorrent login exits 2 and unlinks nothing (rc=$rc)"
fi

seed_tree
rc=0; TEMP_DIR="$tmp/no-such-temp" DATASET_ROOT="$pool" QBT_URL="$url" \
  QBT_USERNAME=u QBT_PASSWORD=p python3 "$script" --apply >"$output" 2>&1 || rc=$?
if [ "$rc" -eq 2 ] && grep -q 'is not a directory' "$output" && survivors_intact; then
  pass "a missing temp directory exits 2 rather than walking the pool"
else
  fail "a missing temp directory exits 2 rather than walking the pool (rc=$rc)"
fi

if [ -x "$script" ]; then
  pass "temp-redundant-name-unlink.py is executable"
else
  fail "temp-redundant-name-unlink.py is executable"
fi

printf '%s\n' "1..$((pass_count + fail_count))"
if [ "$fail_count" -ne 0 ]; then
  printf '%s fixture test(s) failed\n' "$fail_count" >&2
  exit 1
fi
printf 'All %s fixture tests passed.\n' "$pass_count"
