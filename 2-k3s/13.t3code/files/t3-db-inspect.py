#!/usr/bin/env python3
"""Read-only look at the T3 state database: tables, schemas, provider rows."""
import sqlite3

c = sqlite3.connect("file:/home/spyros/.t3/userdata/state.sqlite?mode=ro", uri=True)
tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print("tables:", tables)
for t in tables:
    if any(k in t.lower() for k in ("provider", "instance", "setting", "usage_limit")):
        print(f"\n== {t} ==")
        for row in c.execute(f"PRAGMA table_info({t})"):
            print("  col:", row[1], row[2])
        for row in c.execute(f"SELECT * FROM {t} LIMIT 10"):
            vals = []
            for v in row:
                s = str(v)
                vals.append(s[:60] + ("…" if len(s) > 60 else ""))
            print("  row:", vals)