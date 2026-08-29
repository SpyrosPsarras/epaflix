#!/usr/bin/env python3
"""Flag any ZFS snapshot that outlives the retention of the periodic snapshot
task covering its dataset.

Why this exists: TrueNAS prunes a periodic snapshot only if its parsed timestamp
falls on the task's schedule (`PeriodicSnapshotTaskSnapshotOwner.owns_snapshot`
returns `schedule.should_run(...)`). A snapshot created by "Run Now" is stamped
with the wall-clock minute, so it matches the naming schema but not the
schedule, no owner claims it, and it is never destroyed. See issue #843.

The check reuses the box's own zettarepl decision functions rather than
reimplementing them, so it cannot drift from the real pruner.

Run on the TrueNAS box:  sudo python3 snapshot-retention-audit.py
Exit 0 = clean, 1 = at least one snapshot is leaking, 2 = could not run.
"""
import json
import subprocess
import sys
from datetime import datetime

import isodate
from zettarepl.dataset.relationship import belongs_to_tree
from zettarepl.retention.calculate import calculate_snapshots_to_remove
from zettarepl.retention.snapshot_owner import SnapshotOwner
from zettarepl.scheduler.cron import CronSchedule
from zettarepl.snapshot.name import parse_snapshot_name
from zettarepl.snapshot.snapshot import Snapshot
from zettarepl.utils.datetime import idealized_datetime

ISO_DURATION = {"HOUR": "PT%dH", "DAY": "P%dD", "WEEK": "P%dW",
                "MONTH": "P%dM", "YEAR": "P%dY"}


def run(*argv):
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout


class TaskOwner(SnapshotOwner):
    """Mirror of zettarepl's PeriodicSnapshotTaskSnapshotOwner, built from the
    middleware's task row instead of from a zettarepl Definition."""

    def __init__(self, now, task):
        self.idealized_now = idealized_datetime(now)
        self.task = task
        s = task["schedule"]
        self.schedule = CronSchedule.from_data({
            "minute": s["minute"], "hour": s["hour"], "day-of-month": s["dom"],
            "month": s["month"], "day-of-week": s["dow"],
            "begin": s.get("begin", "00:00"), "end": s.get("end", "23:59"),
        })
        self.lifetime = isodate.parse_duration(
            ISO_DURATION[task["lifetime_unit"]] % task["lifetime_value"])

    def get_naming_schemas(self):
        return [self.task["naming_schema"]]

    def owns_dataset(self, dataset):
        return belongs_to_tree(dataset, self.task["dataset"], self.task["recursive"],
                               self.task["exclude"])

    def owns_snapshot(self, dataset, parsed_snapshot_name):
        return self.schedule.should_run(parsed_snapshot_name.datetime)

    def wants_to_delete(self):
        return True

    def should_retain(self, dataset, parsed_snapshot_name):
        return (idealized_datetime(parsed_snapshot_name.datetime) >
                self.idealized_now - self.lifetime)

    def __repr__(self):
        return f"<TaskOwner id={self.task['id']}>"


def main():
    tasks = json.loads(run("midclt", "call", "pool.snapshottask.query"))
    now = datetime.now()
    owners = [TaskOwner(now, t) for t in tasks if t["enabled"]]

    snapshots, used_by = [], {}
    for line in run("zfs", "list", "-H", "-p", "-t", "snapshot", "-o", "name,used").splitlines():
        full, used = line.rsplit("\t", 1)
        dataset, name = full.split("@", 1)
        snapshots.append(Snapshot(dataset, name))
        used_by[full] = int(used)

    doomed = {f"{s.dataset}@{s.name}"
              for s in calculate_snapshots_to_remove(owners, snapshots)}

    leaks = []
    for snapshot in snapshots:
        full = f"{snapshot.dataset}@{snapshot.name}"
        if full in doomed:
            continue  # the pruner has it, nothing to report
        for owner in owners:
            if not owner.owns_dataset(snapshot.dataset):
                continue
            try:
                parsed = parse_snapshot_name(snapshot.name, owner.task["naming_schema"])
            except ValueError:
                continue  # different naming schema, not this task's snapshot
            if owner.should_retain(snapshot.dataset, parsed):
                continue  # inside the retention window, correct to keep
            if owner.owns_snapshot(snapshot.dataset, parsed):
                reason = "LAST kept as the only snapshot left for its naming schema"
            else:
                reason = "ORPHAN timestamp does not fall on the task schedule"
            age = (idealized_datetime(now) - idealized_datetime(parsed.datetime)).days
            leaks.append((full, used_by[full], owner.task, age, reason))
            break

    disabled = [t for t in tasks if not t["enabled"]]
    for snapshot in snapshots:
        full = f"{snapshot.dataset}@{snapshot.name}"
        if full in doomed or any(row[0] == full for row in leaks):
            continue
        for task in disabled:
            if not belongs_to_tree(snapshot.dataset, task["dataset"], task["recursive"],
                                   task["exclude"]):
                continue
            if any(o.owns_dataset(snapshot.dataset) for o in owners):
                continue  # an enabled task still covers it
            try:
                parsed = parse_snapshot_name(snapshot.name, task["naming_schema"])
            except ValueError:
                continue
            age = (idealized_datetime(now) - idealized_datetime(parsed.datetime)).days
            leaks.append((full, used_by[full], task, age,
                          "DISABLED its only task is disabled, nothing prunes it"))
            break

    if not leaks:
        print("OK no snapshot outlives the retention of its periodic snapshot task")
        return 0

    total = 0
    print(f"{'SNAPSHOT':<62}{'USED':>9}  TASK  AGE      REASON")
    for full, used, task, age, reason in sorted(leaks, key=lambda r: -r[1]):
        total += used
        window = f"{age}d>{task['lifetime_value']}{task['lifetime_unit'][0]}"
        print(f"{full:<62}{used / 2 ** 30:8.2f}G  id={task['id']:<3} {window:<8} {reason}")
    print(f"\n{len(leaks)} snapshot(s) leaking, {total / 2 ** 30:.2f}G pinned")
    print("Before destroying any of them: grep open issues for the name and confirm")
    print("no issue still lists it as a rollback target (#515 destroyed one that was needed).")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as e:
        print(f"failed: {e.cmd[0]} exited {e.returncode}", file=sys.stderr)
        sys.exit(2)
