#!/usr/bin/env python3
"""Record pool capacity over time and forecast when the pool stops being enough.

The #805 deliverable (owner decision 2026-08-23): with no disk budget until
~2027-02, the roadmap is retention-policy + cleanup only, and what the owner
asked for is the weekly net growth number and a forecast naming the date the
runway ends. Headroom is meant to come from the media deletion service the
owner is building (decision 2026-08-21), so this script also prints the
deletion rate that service has to beat.

Each run reads `zpool list`, appends one measurement row to a JSONL history
file, fits a line through the last window of rows, and prints:
  - current used vs size and fragmentation,
  - weekly net growth (freeing counts as negative),
  - forecast dates for 90% and 95% used (95% is the practical ZFS floor:
    past it CoW has no room, fragmentation explodes and writes start failing),
  - 12/24 month projections and the pool-full date,
  - the one-time reclaim and weekly deletion rate that hold 80% used
    (the 20-30% headroom target from the issue).

Run on the TrueNAS box:  sudo python3 pool-capacity-forecast.py
Weekly cron:  15 4 * * 1 root python3 /path/to/pool-capacity-forecast.py
Exit 0 = measured, runway beyond the horizon (or history too short to fit),
1 = the 95% date falls within the horizon, 2 = could not run.

POOLCAP_NOW overrides "now" (ISO datetime) so tests can pin the calendar.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

GIB = 2 ** 30
THRESHOLDS = (90, 95)  # percent used; 95% is the "stops being enough" line


def load_rows(path, pool=None):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                row["ts"] = datetime.fromisoformat(row["ts"])
            except (ValueError, KeyError, TypeError):
                print(f"warning: skipping corrupt history line: {line[:80]}", file=sys.stderr)
                continue
            if pool is None or row.get("pool") == pool:
                rows.append(row)
    rows.sort(key=lambda r: r["ts"])
    return rows


def append_row(path, pool, now, size, alloc, free, frag):
    # rewrite keeps every pool's rows: only this pool's same-date row is replaced
    rows = [r for r in load_rows(path)
            if not (r.get("pool") == pool and r["ts"].date() == now.date())]
    rows.append({"ts": now, "pool": pool, "size": size, "alloc": alloc,
                 "free": free, "frag": frag})
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        for row in rows:
            row["ts"] = row["ts"].isoformat(timespec="seconds")
            f.write(json.dumps(row) + "\n")
    os.replace(tmp, path)  # atomic: a crash mid-write keeps the old history


def fit(rows):
    """Least-squares slope (bytes/day) and intercept of alloc against time."""
    t0 = rows[0]["ts"]
    xs = [(r["ts"] - t0).total_seconds() / 86400 for r in rows]
    ys = [r["alloc"] for r in rows]
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0, (sy / n if n else 0.0)
    slope = (n * sxy - sx * sy) / denom
    return slope, (sy - slope * sx) / n


def crossing(t0, intercept, slope, threshold):
    """Calendar date the fitted line reaches threshold bytes."""
    if slope <= 0:
        return None
    return t0 + timedelta(days=(threshold - intercept) / slope)


def fmt_date(date, now):
    days = (date - now).days
    if days <= 0:
        return f"crossed ({date:%Y-%m-%d})"
    return f"{date:%Y-%m-%d} ({days} days)"


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--pool", default="pool1")
    p.add_argument("--history", default="/var/db/pool-capacity/history.jsonl")
    p.add_argument("--horizon-days", type=int, default=180,
                   help="exit 1 when the 95%% date is within this many days")
    p.add_argument("--window-days", type=int, default=90,
                   help="fit the growth line over this many days of history")
    args = p.parse_args()

    out = subprocess.run(
        ["zpool", "list", "-Hp", "-o", "name,size,alloc,free,frag", args.pool],
        capture_output=True, text=True, check=True).stdout.splitlines()
    fields = out[0].split("\t") if out else []
    if len(fields) != 5:
        print(f"could not parse zpool output for {args.pool}", file=sys.stderr)
        return 2
    pool, size, alloc, free, frag = fields[0], *(int(v) for v in fields[1:])

    if os.environ.get("POOLCAP_NOW"):
        now = datetime.fromisoformat(os.environ["POOLCAP_NOW"])
    else:
        now = datetime.now().replace(microsecond=0)
    append_row(args.history, pool, now, size, alloc, free, frag)

    rows = [r for r in load_rows(args.history, pool)
            if r["size"] == size and r["ts"] >= now - timedelta(days=args.window_days)]
    print(f"{pool}: {alloc / GIB:.1f} GiB used of {size / GIB:.1f} GiB "
          f"({100 * alloc / size:.1f}%), frag {frag}%")

    span_days = (rows[-1]["ts"] - rows[0]["ts"]).days if rows else 0
    if len(rows) < 2 or span_days < 7:
        print(f"history too short: {len(rows)} point(s) over {span_days} days; "
              "weekly growth needs 2+ points spanning 7+ days")
        return 0

    slope, intercept = fit(rows)
    t0 = rows[0]["ts"]
    weekly = slope * 7 / GIB
    points = f"(fit over {len(rows)} points, {span_days} days)"
    if slope < 0:
        print(f"weekly net growth: freeing {-weekly:.1f} GiB {points}")
    else:
        print(f"weekly net growth: {weekly:.1f} GiB {points}")

    for pct in THRESHOLDS:
        date = crossing(t0, intercept, slope, size * pct / 100)
        if date:
            print(f"forecast {pct}% used: {fmt_date(date, now)}")
        else:
            print(f"forecast {pct}% used: not reached at this rate")

    m12 = now + timedelta(days=365)
    for label, at in (("12 months", m12), ("24 months", now + timedelta(days=730))):
        projected = intercept + slope * (at - t0).total_seconds() / 86400
        print(f"{label}: {projected / GIB:.1f} GiB ({100 * projected / size:.1f}%)")
    full = crossing(t0, intercept, slope, size)
    if full:
        print(f"pool full {full:%Y-%m-%d}")

    reclaim = max(0.0, alloc - 0.8 * size) / GIB
    needed = max(0.0, weekly)
    print(f"hold at 80% used: reclaim {reclaim:.1f} GiB now, "
          f"then delete {needed:.1f} GiB/week")

    date95 = crossing(t0, intercept, slope, size * 95 / 100)
    if date95 and (date95 - now).days <= args.horizon_days:
        print(f"runway warning: 95% within {(date95 - now).days} days "
              f"(horizon {args.horizon_days})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (subprocess.CalledProcessError, OSError) as e:
        detail = getattr(e, "stderr", None)
        print(f"failed: {e}", file=sys.stderr)
        if detail:
            print(detail, file=sys.stderr)
        sys.exit(2)
