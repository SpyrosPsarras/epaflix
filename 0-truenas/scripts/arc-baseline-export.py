#!/usr/bin/env python3
"""Export the TrueNAS ZFS ARC baseline from Prometheus to a durable JSONL file.

The #1113 deliverable: decision #918 refuses a tuned truenas-memory threshold
without a measured baseline, and Prometheus retention is finite (8 days at
capture time, 15d since #1402), so the evidence has to leave the TSDB to
survive. Each run pulls the ARC gauge trio
(node_zfs_arc_size, _c, _c_max), the shrink/throttle counters
(node_zfs_arc_memory_direct_count, _indirect_count, _throttle_count) and the
host free/total memory gauges over one window, and writes one JSON object per
timestamp:

    {"ts": "2026-09-13T04:30:00Z", "node_zfs_arc_size": 9244017344.0, ...}

It also prints the summary the tuned-threshold decision reads from: the
size/c_max ratio percentiles, whether the shipped structural thresholds
(TruenasArcOverCap > 1.05) would have fired, and counter deltas.

The baseline windows captured with this script (2026-09-15):
  arc-baseline/normal-day.jsonl   2026-09-14 00:00Z..24:00Z, no memory pressure
  arc-baseline/load-window.jsonl  2026-09-12 20:00Z..2026-09-13 09:00Z, ollama
                                  model load: host sat under 10% free memory
                                  01:50..07:30Z (min 3.4%), ARC shrank to
                                  3.4 GiB (22:35Z) and grew back to its cap by
                                  08:55Z, reclaim counters ticked, throttle
                                  stayed at 0

Run while the window is still inside retention (15d since #1402, 8d at
capture time):
  kubectl port-forward -n observability svc/kube-prometheus-stack-prometheus 9090:9090 &
  python3 arc-baseline-export.py --start 2026-09-12T20:00:00Z --end 2026-09-13T09:00:00Z \
      --out arc-baseline/load-window.jsonl
Exit 0 = exported, 1 = window empty (series missing or aged out of retention),
2 = could not run (Prometheus unreachable, bad window, unexpected cardinality).
"""
import argparse
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

JOB = 'truenas-node-exporter'
SERIES = (
    'node_zfs_arc_size',
    'node_zfs_arc_c',
    'node_zfs_arc_c_max',
    'node_zfs_arc_memory_direct_count',
    'node_zfs_arc_memory_indirect_count',
    'node_zfs_arc_memory_throttle_count',
    'node_memory_MemAvailable_bytes',
    'node_memory_MemTotal_bytes',
)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_ts(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()


def fetch(base_url, name, start, end, step):
    query = f'{name}{{job="{JOB}"}}'
    params = urllib.parse.urlencode(
        {'query': query, 'start': start, 'end': end, 'step': step})
    with urllib.request.urlopen(f'{base_url}/api/v1/query_range?{params}', timeout=60) as r:
        body = json.load(r)
    if body.get('status') != 'success':
        raise RuntimeError(f'{name}: {body}')
    samples = body['data']['result']
    if not samples:
        return []
    if len(samples) != 1:
        raise RuntimeError(
            f'{name}: expected 1 series for job="{JOB}", got {len(samples)}')
    return [(float(t), float(v)) for t, v in samples[0]['values']]


def percentile(sorted_values, q):
    """Nearest-rank percentile; overstates rather than understates the tail."""
    return sorted_values[min(len(sorted_values) - 1,
                             max(0, math.ceil(q * len(sorted_values)) - 1))]


def summarize(rows):
    """Print the facts the threshold decision reads: ratio stats, counters."""
    ratio = []
    for row in rows:
        size, c_max = row.get('node_zfs_arc_size'), row.get('node_zfs_arc_c_max')
        if size is not None and c_max:
            ratio.append((row['ts'], size / c_max))
    if ratio:
        values = sorted(v for _, v in ratio)
        worst_ts, worst = max(ratio, key=lambda x: x[1])
        over_105 = sum(1 for _, v in ratio if v > 1.05)
        over_101 = sum(1 for _, v in ratio if v > 1.01)
        print(f'arc size/c_max over {len(ratio)} samples: '
              f'p50 {percentile(values, 0.5):.5f} p99 {percentile(values, 0.99):.5f} '
              f'max {worst:.5f} (at {worst_ts}); '
              f'{over_101} above 1.01, {over_105} above 1.05')
    c_max_values = {row['node_zfs_arc_c_max'] for row in rows
                    if row.get('node_zfs_arc_c_max') is not None}
    if c_max_values:
        pretty = ', '.join(f'{v / 2**30:.0f} GiB' for v in sorted(c_max_values))
        print(f'arc c_max values seen: {pretty}')
    for name in ('node_zfs_arc_memory_direct_count',
                 'node_zfs_arc_memory_indirect_count',
                 'node_zfs_arc_memory_throttle_count'):
        seen = [(row['ts'], row[name]) for row in rows if row.get(name) is not None]
        if len(seen) >= 2:
            print(f'counter {name}: {seen[0][1]:.0f} -> {seen[-1][1]:.0f} '
                  f'(+{seen[-1][1] - seen[0][1]:.0f})')
    avail = [row['node_memory_MemAvailable_bytes'] for row in rows
             if row.get('node_memory_MemAvailable_bytes') is not None]
    total = [row['node_memory_MemTotal_bytes'] for row in rows
             if row.get('node_memory_MemTotal_bytes') is not None]
    if avail and total:
        free = sorted(a / t for a, t in zip(avail, total))
        print(f'host memory available: p50 {percentile(free, 0.5):.1%} '
              f'min {free[0]:.1%}')


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--url', default='http://localhost:9090',
                   help='Prometheus base URL (default http://localhost:9090)')
    p.add_argument('--start', required=True, help='window start, ISO UTC (e.g. 2026-09-13T01:00:00Z)')
    p.add_argument('--end', required=True, help='window end, ISO UTC')
    p.add_argument('--step', type=int, default=300,
                   help='seconds between rows (default 300; the exporter scrapes every 60s)')
    p.add_argument('--out', required=True, help='JSONL path to write')
    args = p.parse_args()

    start, end = parse_ts(args.start), parse_ts(args.end)
    if start >= end:
        print(f'empty window: start {args.start} >= end {args.end}', file=sys.stderr)
        return 2

    by_ts = {}
    got = {}
    for name in SERIES:
        try:
            got[name] = fetch(args.url, name, start, end, args.step)
        except (RuntimeError, OSError) as e:
            print(f'failed to query {name}: {e}', file=sys.stderr)
            return 2
        if not got[name]:
            print(f'warning: {name} has no samples in the window '
                  '(aged out of retention or metric renamed)', file=sys.stderr)
        for ts, value in got[name]:
            by_ts.setdefault(ts, {'ts': iso(ts)})[name] = value

    rows = [by_ts[ts] for ts in sorted(by_ts)]
    if not rows:
        print('no samples in the window', file=sys.stderr)
        return 1
    present = sum(1 for samples in got.values() if samples)
    print(f'window {iso(start)} .. {iso(end)}: {len(rows)} rows, '
          f'{present}/{len(SERIES)} series present')

    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    tmp = f'{args.out}.tmp'
    with open(tmp, 'w') as f:
        for row in rows:
            f.write(json.dumps(row) + '\n')
    os.replace(tmp, args.out)  # atomic: a crash mid-write keeps the old export
    print(f'wrote {len(rows)} rows to {args.out}')

    summarize(rows)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 - any failure means "could not run"
        print(f'failed: {e}', file=sys.stderr)
        sys.exit(2)
