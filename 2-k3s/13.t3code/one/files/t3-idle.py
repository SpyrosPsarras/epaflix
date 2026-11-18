#!/usr/bin/env python3
"""Seconds since the last T3 run ended (0 while one is executing) or terminal keyboard input (pts atime; output does not touch it)."""
import glob
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

DB = os.environ.get("T3_STATE_DB", os.path.expanduser("~/.t3/userdata/statev2.sqlite"))
TTYS = os.environ.get("T3_IDLE_TTYS", "/dev/pts/[0-9]*")
WAITING_HOLDS_POD = timedelta(hours=24)
# Run statuses: packages/contracts/src/orchestrationV2.ts.
LATEST = """
SELECT CASE WHEN max(status IN ('preparing', 'starting', 'running') OR (status = 'waiting' AND requested_at > ?))
  THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
  ELSE max(max(requested_at), coalesce(max(completed_at), ''))
END FROM orchestration_v2_projection_runs
"""


def last_state_change():
    if not os.path.exists(DB):
        raise SystemExit(f"t3-idle: no T3 state database at {DB}")
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=2) as c:
        waiting_since = (datetime.now(timezone.utc) - WAITING_HOLDS_POD).isoformat(timespec="milliseconds")
        (latest,) = c.execute(LATEST, (waiting_since.replace("+00:00", "Z"),)).fetchone()
    return datetime.fromisoformat(latest.replace("Z", "+00:00")).timestamp() if latest else 0.0


def last_tty_input():
    return max((os.stat(p).st_atime for p in glob.glob(TTYS)), default=0.0)


print(int(time.time() - max(last_state_change(), last_tty_input())))
