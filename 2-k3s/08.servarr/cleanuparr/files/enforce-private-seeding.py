#!/usr/bin/env python3
"""Keep Cleanuparr's Download Cleaner off private-tracker torrents.

Cleanuparr removes a download when (max_ratio AND min_seed_time) are reached, OR
when max_seed_time is reached regardless of ratio. A rule at privacy_type='both'
therefore applies those thresholds to PRIVATE trackers too, and with max_ratio=1.0
that is mathematically incapable of ever producing an account ratio >= 1.0:
torrents that would earn a surplus are cut dead exactly at 1.0, and torrents that
never get there are cut at max_seed_time at whatever ratio they had.

That is what demoted the TorrentDay account to ratio 0.218 and suspended its
download privileges: 0 TorrentDay torrents were left seeding, and over one 3.7-day
window 49 of 83 removals fired on MAX_SEED_TIME with an unfinished ratio.

This runs as an initContainer so it is enforced on every boot, including onto a
restored PVC. Idempotent: re-running changes nothing.
"""
import os
import sqlite3
import sys
import uuid

DB = os.environ.get("CLEANUPARR_DB", "/config/cleanuparr.db")

# Private-tracker thresholds. max_ratio well above 1.0 so a torrent can bank a
# surplus instead of being cut at break-even; max_seed_time still bounded so the
# downloads directory cannot grow without limit.
PRIVATE_MAX_RATIO = 5.0
PRIVATE_MAX_SEED_TIME = 720.0  # hours (30 days)

TABLES = ("q_bit_seeding_rules", "deluge_seeding_rules",
          "transmission_seeding_rules", "u_torrent_seeding_rules",
          "r_torrent_seeding_rules")


def enforce(con, table):
    cur = con.cursor()
    cur.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", (table,))
    if not cur.fetchone()[0]:
        return 0, 0

    # 1. No rule may govern private torrents with the public thresholds.
    demoted = cur.execute(
        f"UPDATE {table} SET privacy_type='public' WHERE privacy_type='both'").rowcount

    # 2. Every public rule gets a private counterpart, unless one already exists
    #    for the same categories (so an operator-tuned rule is never clobbered).
    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})")]
    added = 0
    publics = cur.execute(
        f"SELECT * FROM {table} WHERE privacy_type='public'").fetchall()
    for row in publics:
        rec = dict(zip(cols, row))
        already = cur.execute(
            f"SELECT 1 FROM {table} WHERE privacy_type='private' AND categories=?",
            (rec["categories"],)).fetchone()
        if already:
            continue
        rec["id"] = str(uuid.uuid4()).upper()
        rec["name"] = f"{rec['name']} (private)"
        rec["privacy_type"] = "private"
        rec["max_ratio"] = PRIVATE_MAX_RATIO
        rec["max_seed_time"] = PRIVATE_MAX_SEED_TIME
        if "priority" in rec:
            rec["priority"] = (rec["priority"] or 0) + 10
        placeholders = ",".join("?" * len(cols))
        cur.execute(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
            [rec[c] for c in cols])
        added += 1
    return demoted, added


def main():
    if not os.path.exists(DB):
        print(f"{DB} absent (fresh install) - nothing to enforce")
        return 0
    con = sqlite3.connect(DB)
    try:
        total_demoted = total_added = 0
        for table in TABLES:
            demoted, added = enforce(con, table)
            if demoted or added:
                print(f"{table}: {demoted} rule(s) taken off private torrents, "
                      f"{added} private rule(s) created")
            total_demoted += demoted
            total_added += added
        con.commit()
        if not (total_demoted or total_added):
            print("private-tracker seeding rules already correct - no change")
        for table in TABLES:
            cur = con.cursor()
            cur.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", (table,))
            if not cur.fetchone()[0]:
                continue
            for name, priv, ratio, seed in cur.execute(
                    f"SELECT name,privacy_type,max_ratio,max_seed_time FROM {table} ORDER BY priority"):
                print(f"  {table}: {name} | {priv} | ratio<={ratio} | seed<={seed}h")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
