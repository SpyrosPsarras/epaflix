#!/usr/bin/env python3
"""Seconds since the last T3 state change or terminal keyboard input (pts atime; output does not touch it)."""
import glob
import os
import sqlite3
import time
from datetime import datetime

DB = os.environ.get("T3_STATE_DB", "/home/spyros/.t3/userdata/statev2.sqlite")
TTYS = os.environ.get("T3_IDLE_TTYS", "/dev/pts/[0-9]*")
LATEST = """
SELECT max(t) FROM (
  SELECT max(updated_at) AS t FROM orchestration_v2_projection_messages
  UNION ALL SELECT max(updated_at) FROM orchestration_v2_projection_turn_items
  UNION ALL SELECT max(requested_at) FROM orchestration_v2_projection_runs
  UNION ALL SELECT max(completed_at) FROM orchestration_v2_projection_runs
)
"""


def last_state_change():
    if not os.path.exists(DB):
        raise SystemExit(f"t3-idle: no T3 state database at {DB}")
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as c:
        (latest,) = c.execute(LATEST).fetchone()
    return datetime.fromisoformat(latest.replace("Z", "+00:00")).timestamp() if latest else 0.0


def last_tty_input():
    return max((os.stat(p).st_atime for p in glob.glob(TTYS)), default=0.0)


print(int(time.time() - max(last_state_change(), last_tty_input())))
