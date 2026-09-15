#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
script="$repo_root/0-truenas/scripts/arc-baseline-export.py"
tmp="$(mktemp -d)"
output="$(mktemp)"
trap 'rm -rf "$tmp"; rm -f "$output"; [ -n "${stub_pid:-}" ] && kill "$stub_pid" 2>/dev/null || true' EXIT

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

# Case 1: full export. Ratio max is 13e9/12e9 = 1.08333 (1 sample above 1.05),
# throttle +5, direct +5, indirect +10, memory 12.5% free.
port_file="$tmp/port"
python3 -c "
import json, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

BASE = 1789430400  # 2026-09-15T00:00:00Z

CANNED = {
    'node_zfs_arc_size':                       [12.0e9, 12.3e9, 13.0e9],
    'node_zfs_arc_c':                          [12.0e9, 12.0e9, 12.0e9],
    'node_zfs_arc_c_max':                      [12.0e9, 12.0e9, 12.0e9],
    'node_zfs_arc_memory_direct_count':        [100.0, 100.0, 105.0],
    'node_zfs_arc_memory_indirect_count':      [1000.0, 1005.0, 1010.0],
    'node_zfs_arc_memory_throttle_count':      [0.0, 0.0, 5.0],
    'node_memory_MemAvailable_bytes':          [4.0e9, 4.0e9, 4.0e9],
    'node_memory_MemTotal_bytes':              [32.0e9, 32.0e9, 32.0e9],
}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        name = query.get('query', [''])[0].split('{')[0]
        start = float(query.get('start', ['0'])[0])
        end = float(query.get('end', ['1e18'])[0])
        values = [[str(BASE + i * 300), repr(v)]
                  for i, v in enumerate(CANNED.get(name, []))
                  if start <= BASE + i * 300 <= end]
        body = json.dumps({'status': 'success', 'data': {'resultType': 'matrix',
                                                         'result': [{'metric': {'__name__': name, 'job': 'truenas-node-exporter'}, 'values': values}] if values else []}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

server = HTTPServer(('127.0.0.1', 0), Handler)
open('$port_file', 'w').write(str(server.server_address[1]))
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(120)
" &
stub_pid=$!
for _ in $(seq 1 50); do
  [ -s "$port_file" ] && break
  sleep 0.1
done
if [ ! -s "$port_file" ]; then
  fail "stub Prometheus never bound a port"
  printf '1..%s\n' "$((pass_count + fail_count))"
  exit 1
fi
port="$(cat "$port_file")"

rc=0; python3 "$script" --url "http://127.0.0.1:$port" \
  --start 2026-09-15T00:00:00Z --end 2026-09-15T00:10:00Z --step 300 \
  --out "$tmp/baseline.jsonl" >"$output" 2>&1 || rc=$?
if [ "$rc" -eq 0 ] \
  && [ "$(wc -l <"$tmp/baseline.jsonl")" -eq 3 ] \
  && grep -q 'arc size/c_max over 3 samples: p50 1.02500 p99 1.08333 max 1.08333 (at 2026-09-15T00:10:00Z); 2 above 1.01, 1 above 1.05' "$output" \
  && grep -q 'counter node_zfs_arc_memory_throttle_count: 0 -> 5 (+5)' "$output" \
  && grep -q 'host memory available: p50 12.5% min 12.5%' "$output" \
  && [ "$(head -1 "$tmp/baseline.jsonl")" = '{"ts": "2026-09-15T00:00:00Z", "node_zfs_arc_size": 12000000000.0, "node_zfs_arc_c": 12000000000.0, "node_zfs_arc_c_max": 12000000000.0, "node_zfs_arc_memory_direct_count": 100.0, "node_zfs_arc_memory_indirect_count": 1000.0, "node_zfs_arc_memory_throttle_count": 0.0, "node_memory_MemAvailable_bytes": 4000000000.0, "node_memory_MemTotal_bytes": 32000000000.0}' ]; then
  pass "exports one JSONL row per sample and prints the ratio/counter summary"
else
  fail "exports one JSONL row per sample and prints the ratio/counter summary (rc=$rc)"
fi

rc=0; python3 "$script" --url "http://127.0.0.1:$port" \
  --start 2026-09-14T00:00:00Z --end 2026-09-14T00:10:00Z --step 300 \
  --out "$tmp/empty.jsonl" >"$output" 2>&1 || rc=$?
if [ "$rc" -eq 1 ] && ! [ -e "$tmp/empty.jsonl" ]; then
  pass "an empty window exits 1 and writes nothing"
else
  fail "an empty window exits 1 and writes nothing (rc=$rc)"
fi

rc=0; python3 "$script" --url "http://127.0.0.1:1" \
  --start 2026-09-15T00:00:00Z --end 2026-09-15T00:10:00Z \
  --out "$tmp/err.jsonl" >"$output" 2>&1 || rc=$?
if [ "$rc" -eq 2 ]; then
  pass "an unreachable Prometheus exits 2"
else
  fail "an unreachable Prometheus exits 2 (rc=$rc)"
fi

if [ -x "$script" ]; then
  pass "arc-baseline-export.py is executable"
else
  fail "arc-baseline-export.py is executable"
fi

printf '%s\n' "1..$((pass_count + fail_count))"
if [ "$fail_count" -ne 0 ]; then
  printf '%s fixture test(s) failed\n' "$fail_count" >&2
  exit 1
fi
printf 'All %s fixture tests passed.\n' "$pass_count"
