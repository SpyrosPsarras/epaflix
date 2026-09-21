#!/usr/bin/env python3
"""Offline T3 merger. Inputs are snapshots; outputs are never live homes."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

BASE = "8b45bdca9b132cc243433d4d8466474f8f7f9dff"
SCHEMA_SHA256 = "2219bfb1fd381a8fe6564d4c0e3544077497a9deeed493767eb705acff27ede1"
SOURCES = {"host": ("/home/spyros", "/home/spyros"),
           "t3env-0": ("/home/t3", "/home/t3env-0"),
           "t3env-1": ("/home/t3", "/home/t3env-1")}
# The LXC reaches cliproxy through Pi-hole -> internal Traefik. Cluster DNS
# resolves the same name to Cloudflare, where it 404s. Inside the pod the
# provider must use the Service the env pods already use.
CLIPROXY_URLS = {"https://cliproxy.epaflix.com": "http://cliproxy.remote-pi.svc.cluster.local:8317"}
DRIVERS = {"codex", "claudeAgent", "opencode"}
SPECIAL = {"effect_sql_migrations", "projection_state", "sqlite_sequence"}
PATH_KEYS = {"cwd", "workspaceRoot", "worktreePath", "homePath", "shadowHomePath",
             "binaryPath", "configDirectory", "faviconPath", "filePath", "absolutePath"}
PATH_COLUMNS = {"workspace_root", "worktree_path", "favicon_path"}
PROJECTORS = {"projection." + x for x in (
    "projects", "threads", "thread-messages", "thread-proposed-plans",
    "thread-activities", "thread-sessions", "thread-turns", "checkpoints",
    "pending-approvals", "attachment-cleanup")}


class Invalid(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Invalid(message)


def ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def digest(path):
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def schema(c):
    return c.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                     "WHERE sql IS NOT NULL ORDER BY type,name").fetchall()


def ident(s):
    return '"' + s.replace('"', '""') + '"'


def rows(c, table):
    cursor = c.execute("SELECT * FROM " + ident(table))
    names = [x[0] for x in cursor.description]
    for row in cursor:
        yield dict(zip(names, row))


def maximum(c, table, column):
    return c.execute(f"SELECT coalesce(max({ident(column)}),0) FROM {ident(table)}").fetchone()[0]


def path_map(value, source):
    old, new = SOURCES[source]
    if isinstance(value, str) and (value == old or value.startswith(old + "/")):
        return new + value[len(old):]
    return value


def instance(value, source, instances):
    require(value in instances, "unconfigured provider instance")
    # Host keeps its ids so defaultModelSelection and new threads stay on them.
    # Env sources get a prefix so their threads resume against their own home.
    return value if source == "host" else source + "__" + value


def transform_json(value, source, instances, key=""):
    # Only structural fields change. Prompt text, tool output and JSONL never do.
    if isinstance(value, dict):
        return {k: transform_json(v, source, instances, k) for k, v in value.items()}
    if isinstance(value, list):
        return [transform_json(v, source, instances, key) for v in value]
    if value is not None and key in {"instanceId", "providerInstanceId"}:
        return instance(value, source, instances)
    if key in PATH_KEYS:
        return path_map(value, source)
    return value


def transformed(row, table, source, instances, event_offset, turn_offset):
    result = dict(row)
    # Older rows carry only provider_name; the runtime fills provider_instance_id
    # from it. Do that here once so the single rewrite below covers both shapes.
    if table in {"provider_session_runtime", "projection_thread_sessions"} \
            and not row.get("provider_instance_id") and row.get("provider_name"):
        result["provider_instance_id"] = row["provider_name"]
    for col, value in list(result.items()):
        if value is None:
            continue
        if col in PATH_COLUMNS:
            result[col] = path_map(value, source)
        elif col == "provider_instance_id":
            result[col] = instance(value, source, instances)
        elif col.endswith("_json"):
            parsed = json.loads(value)
            changed = transform_json(parsed, source, instances)
            if table == "orchestration_events" and col == "payload_json":
                activity = changed.get("activity") if isinstance(changed, dict) else None
                if isinstance(activity, dict) and activity.get("sequence") is not None:
                    activity["sequence"] += event_offset
            if changed != parsed:
                result[col] = json.dumps(changed, ensure_ascii=False, separators=(",", ":"))
    offset_col = {"orchestration_events": ("sequence", event_offset),
                  "orchestration_command_receipts": ("result_sequence", event_offset),
                  "projection_thread_activities": ("sequence", event_offset),
                  "projection_turns": ("row_id", turn_offset)}.get(table)
    if offset_col and row[offset_col[0]] is not None:
        result[offset_col[0]] += offset_col[1]
    return result


def source_instances(settings, c):
    instances = dict(settings.get("providerInstances", {}))
    for driver, in c.execute("SELECT DISTINCT provider_name FROM provider_session_runtime"):
        require(driver in DRIVERS, "unsupported provider driver")
        instances.setdefault(driver, {"driver": driver,
                                      "config": settings.get("providers", {}).get(driver, {})})
    for name, value in instances.items():
        require(value.get("driver") in DRIVERS and len(name) <= 53,
                "unsupported provider instance")
    return instances


def provider_config(config, source, instances):
    result = transform_json(config, source, instances)
    inner = result.get("config")
    if isinstance(inner, dict):
        for old, new in CLIPROXY_URLS.items():
            if isinstance(inner.get("launchArgs"), str):
                inner["launchArgs"] = inner["launchArgs"].replace(old, new)
            if isinstance(inner.get("serverUrl"), str) and inner["serverUrl"].startswith(old):
                inner["serverUrl"] = new + inner["serverUrl"][len(old):]
    home = SOURCES[source][1]
    env = {x["name"]: dict(x) for x in result.get("environment", [])}
    overrides = {"HOME": home, "XDG_DATA_HOME": home + "/.local/share",
                 "XDG_CONFIG_HOME": home + "/.config", "XDG_STATE_HOME": home + "/.local/state",
                 "XDG_CACHE_HOME": home + "/.cache", "CODEX_HOME": home + "/.codex",
                 "CLAUDE_CONFIG_DIR": home + "/.claude"}
    for name, value in overrides.items():
        if name in env:
            require(not env[name].get("valueRedacted"), "redacted provider environment")
            env[name]["value"] = path_map(env[name]["value"], source)
        else:
            env[name] = {"name": name, "value": value, "sensitive": False}
    require(all(not x.get("valueRedacted") for x in env.values()),
            "redacted environment requires source secret-store resolution")
    result["environment"] = list(env.values())
    if source != "host":
        result["displayName"] = f"{result.get('displayName') or config.get('driver')} [{source} history]"
    return result


def validate_source(c):
    require(c.execute("PRAGMA integrity_check").fetchone() == ("ok",), "source integrity")
    require(not c.execute("PRAGMA foreign_key_check").fetchall(), "source foreign keys")
    require(c.execute("SELECT count(*) FROM effect_sql_migrations").fetchone()[0] == 53,
            "expected 53 migrations")
    top = maximum(c, "orchestration_events", "sequence")
    states = {r["projector"]: r for r in rows(c, "projection_state")}
    require(set(states) == PROJECTORS, "unexpected projectors")
    require(all(r["last_applied_sequence"] == top for k, r in states.items()
                if k != "projection.attachment-cleanup"), "projection lag: take another snapshot")
    cleanup = states["projection.attachment-cleanup"]["last_applied_sequence"]
    require(0 <= cleanup <= top, "invalid cleanup watermark")
    # The server replays thread.deleted after this watermark on boot and removes
    # that thread's attachment files; that is idempotent. thread.reverted prunes
    # against message state instead, so a pending revert is refused.
    pending = c.execute("SELECT count(*) FROM orchestration_events WHERE sequence > ? "
                        "AND event_type = 'thread.reverted'", (cleanup,)).fetchone()[0]
    require(pending == 0, "pending attachment prune after revert: drain cleanup first")
    for table, col in [("orchestration_command_receipts", "result_sequence"),
                       ("projection_thread_activities", "sequence")]:
        require(c.execute(f"SELECT count(*) FROM {table} WHERE {col}<0 OR {col}>?", (top,)).fetchone()[0] == 0,
                "out-of-range sequence")
    return top, states


def copy_tree(source, dest):
    """Copy a frozen provider/home tree byte-for-byte, refusing unsafe links."""
    manifest = {}
    source = source.resolve()
    require(source.is_dir(), "missing frozen home")
    for root, dirs, files in os.walk(source, followlinks=False):
        for name in dirs + files:
            p = Path(root) / name
            require(not p.is_symlink(), "home contains symlinks: stage resolved files explicitly")
            if p.is_file():
                rel = p.relative_to(source)
                out = dest / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                before = digest(p)
                shutil.copy2(p, out)
                require(before == digest(p) == digest(out), "file changed during copy")
                manifest[str(rel)] = before
            else:
                require(p.is_dir(), "unsupported special file")
                (dest / p.relative_to(source)).mkdir(parents=True, exist_ok=True)
    return manifest


def merge(inputs, output):
    inputs, output = Path(inputs).resolve(), Path(output).absolute()
    require(not output.exists(), "output already exists")
    require(not output.is_relative_to(inputs), "output cannot be inside inputs")
    require(output.parent.is_dir(), "output parent missing")
    with tempfile.TemporaryDirectory(prefix=".merge-", dir=output.parent) as tmp:
        stage = Path(tmp)
        out = sqlite3.connect(stage / "state.sqlite")
        report = {"base": BASE, "sources": {}, "cutover_ready": False}
        merged_settings = None
        event_offset = turn_offset = 0
        cleanup_start = None
        totals = {}
        with contextlib.ExitStack() as stack:
            stack.callback(out.close)
            for source in SOURCES:
                folder = inputs / source
                db = folder / "state.sqlite"
                before = digest(db)
                c = stack.enter_context(contextlib.closing(ro(db)))
                require(hashlib.sha256(json.dumps(schema(c), separators=(",", ":")).encode()).hexdigest()
                        == SCHEMA_SHA256, "schema differs from inspected schema")
                migrations = c.execute("SELECT migration_id,name FROM effect_sql_migrations ORDER BY migration_id").fetchall()
                top, states = validate_source(c)
                settings = json.loads((folder / "settings.json").read_text())
                instances = source_instances(settings, c)
                if merged_settings is None:
                    c.backup(out)
                    tables = [r[1] for r in schema(c) if r[0] == "table" and r[1] not in SPECIAL]
                    for table in tables:
                        out.execute("DELETE FROM " + ident(table))
                    out.execute("DELETE FROM sqlite_sequence")
                    merged_settings = transform_json(settings, source, instances)
                    merged_settings["providerInstances"] = {}
                    merged_settings["continueThreadsAfterServerUpdate"] = False
                    for entry in merged_settings.get("usageLimitSources", {}).values():
                        for old, new in CLIPROXY_URLS.items():
                            if entry.get("url") == old:
                                entry["url"] = new
                    first_migrations = migrations
                require(migrations == first_migrations, "migration identities differ")
                for key, config in instances.items():
                    merged_settings["providerInstances"][instance(key, source, instances)] = provider_config(config, source, instances)
                counts = {}
                for table in tables:
                    counts[table] = 0
                    for row in rows(c, table):
                        changed = transformed(row, table, source, instances, event_offset, turn_offset)
                        sql = f"INSERT INTO {ident(table)} ({','.join(map(ident,changed))}) VALUES ({','.join('?' for _ in changed)})"
                        try:
                            out.execute(sql, list(changed.values()))
                        except sqlite3.IntegrityError:
                            raise Invalid("key collision in " + table) from None
                        counts[table] += 1
                    totals[table] = totals.get(table, 0) + counts[table]
                report["sources"][source] = {"sha256": before, "counts": counts,
                    "event_offset": event_offset, "turn_offset": turn_offset,
                    "original_watermarks": {k: v["last_applied_sequence"] for k, v in states.items()}}
                cleanup_wm = states["projection.attachment-cleanup"]["last_applied_sequence"]
                if cleanup_wm < top and cleanup_start is None:
                    cleanup_start = event_offset + cleanup_wm
                event_offset += top
                turn_offset += maximum(c, "projection_turns", "row_id")
                require(digest(db) == before, "input snapshot changed")
                if (folder / "home").exists():
                    report["sources"][source]["home_files"] = copy_tree(folder / "home", stage / "homes" / source)
            # Data projectors covered every source event. The cleanup watermark rewinds to
            # the earliest unconsumed tail so the destination server replays each pending
            # thread.deleted; deletions before that point are already applied and re-running
            # them only re-removes files that are gone.
            out.execute("UPDATE projection_state SET last_applied_sequence=?", (event_offset,))
            out.execute("UPDATE projection_state SET last_applied_sequence=? "
                        "WHERE projector='projection.attachment-cleanup'",
                        (event_offset if cleanup_start is None else cleanup_start,))
            out.commit()
            require(out.execute("PRAGMA integrity_check").fetchone() == ("ok",), "output integrity")
            require(not out.execute("PRAGMA foreign_key_check").fetchall(), "output foreign keys")
            for table, count in totals.items():
                require(out.execute("SELECT count(*) FROM " + ident(table)).fetchone()[0] == count,
                        "row count mismatch")
            require(out.execute("SELECT count(*) FROM projection_threads t LEFT JOIN projection_projects p "
                                "ON p.project_id=t.project_id WHERE p.project_id IS NULL").fetchone()[0] == 0,
                    "orphan thread project")
            for table, col in [("orchestration_command_receipts", "result_sequence"),
                               ("projection_thread_activities", "sequence")]:
                require(out.execute(f"SELECT count(*) FROM {table} a LEFT JOIN orchestration_events e "
                                    f"ON e.sequence=a.{col} WHERE a.{col} IS NOT NULL AND e.sequence IS NULL").fetchone()[0] == 0,
                        "dangling " + col)
            # Attachment files are named <thread-segment>-<uuid>-<ext>.<ext>. Distinct thread ids
            # across sources means the three attachments/ folders can be unioned by filename.
            require(out.execute("SELECT count(*) FROM (SELECT lower(thread_id) t FROM projection_threads "
                                "GROUP BY t HAVING count(*)>1)").fetchone()[0] == 0, "thread id case collision")
            report["totals"] = totals
            report["event_high_water"] = event_offset
            (stage / "settings.json").write_text(json.dumps(merged_settings, indent=2) + "\n")
            (stage / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        # Publish only a fully validated result. Never replace an existing destination.
        output.mkdir(mode=0o700)
        try:
            for p in stage.iterdir():
                shutil.move(str(p), output / p.name)
        except BaseException:
            shutil.rmtree(output)
            raise
    return report


SNAPSHOT_CODE = '''import sqlite3,sys
c=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True)
d=sqlite3.connect(":memory:")
c.backup(d)
sys.stdout.buffer.write(d.serialize())
'''


def snapshot(output):
    """SQLite online backup, no remote temp files and no source writes."""
    output = Path(output)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    for source, (home, _) in SOURCES.items():
        folder = output / source
        folder.mkdir(mode=0o700)
        prefix = [] if source == "host" else ["kubectl", "-n", "remote-pi", "exec", source, "--"]
        with (folder / "state.sqlite").open("xb") as f:
            subprocess.run(prefix + ["python3", "-c", SNAPSHOT_CODE, home + "/.t3/userdata/state.sqlite"],
                           stdout=f, check=True)
        with (folder / "settings.json").open("xb") as f:
            subprocess.run(prefix + ["python3", "-c",
                "import pathlib,sys; sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())",
                home + "/.t3/userdata/settings.json"], stdout=f, check=True)
    print("Snapshot complete. SQLite-only rehearsal; provider homes not captured.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("snapshot")
    p.add_argument("output", type=Path)
    p = sub.add_parser("merge")
    p.add_argument("inputs", type=Path)
    p.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            snapshot(args.output)
        else:
            r = merge(args.inputs, args.output)
            print(json.dumps({"threads": r["totals"]["projection_threads"],
                              "events": r["totals"]["orchestration_events"], "cutover_ready": False}))
    except (Invalid, sqlite3.Error, ValueError, OSError, subprocess.SubprocessError) as exc:
        # Never emit SQL values, settings, or conversation-bearing exceptions.
        print("Migration failed: " + (str(exc) if isinstance(exc, Invalid) else type(exc).__name__), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
