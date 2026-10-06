#!/usr/bin/env python3
"""Fixture checks for files/opencode-v1-backup.py: WAL-backed store, marker last, restarts, refusals."""
from collections import namedtuple
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from unittest.mock import patch

FILES = Path(__file__).resolve().parents[1] / "files"
spec = importlib.util.spec_from_file_location("backup", FILES / "opencode-v1-backup.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)
Usage = namedtuple("Usage", "total used free")


def fails(fn, text):
    try:
        fn()
    except SystemExit as e:
        assert text in str(e), e
    else:
        raise AssertionError(f"expected a refusal: {text}")


def free(size):
    return patch.object(backup.shutil, "disk_usage", return_value=Usage(0, 0, size))


def v1_store(data, rows=3):
    # The writer connection stays open with autocheckpoint off, so the rows stay in the WAL.
    data.mkdir(parents=True)
    db = sqlite3.connect(data / "opencode.db")
    db.execute("pragma journal_mode=wal")
    db.execute("pragma wal_autocheckpoint=0")
    db.execute("create table session (id text primary key, title text)")
    db.execute("create table session_context_epoch (id text)")
    db.executemany("insert into session values (?, ?)", [(f"ses_{i}", "synthetic") for i in range(rows)])
    db.commit()
    return db


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    data, config, t3 = root / "data", root / "config", root / "t3"
    writer = v1_store(data)
    assert (data / "opencode.db-wal").stat().st_size > 0, "fixture keeps its rows in the WAL"
    config.mkdir()
    (config / "opencode.json").write_text('{"plugin": ["file:///x/dist/index.js"]}')
    (config / "plugins").mkdir()
    (config / "plugins/jev-auto.js").write_text("export default {}")
    t3.mkdir()
    (t3 / "settings.json").write_text('{"providers": {}}')
    with closing(sqlite3.connect(t3 / "statev2.sqlite")) as state:
        state.execute("pragma journal_mode=wal")
        state.execute("create table projection_threads (id text)")
        state.execute("insert into projection_threads values ('thread-1')")
        state.commit()
    (t3 / "server-runtime.json.lock").write_text("not copied")
    main_db = hashlib.sha256((data / "opencode.db").read_bytes()).hexdigest()

    with free(10):
        fails(lambda: backup.run(data, config, t3), "2.5 x database + WAL")
    assert not (data / backup.BACKUP).exists() and not (data / backup.MARKER).exists(), "a space refusal writes nothing"
    assert not list(data.glob("*.tmp-*"))

    with patch.object(backup, "snapshot_files", side_effect=OSError("disk went away")):
        try:
            backup.run(data, config, t3)
        except OSError:
            pass
    assert (data / backup.BACKUP).exists() and not (data / backup.MARKER).exists(), "the marker is written last"
    first = (data / backup.BACKUP).stat()
    snapshot = backup.snapshot_bytes(config, t3)
    assert 0 < snapshot < backup.SPACE_FACTOR * (data / "opencode.db").stat().st_size
    with free(snapshot - 1):
        fails(lambda: backup.run(data, config, t3), "config and T3 state snapshot")
    with free(snapshot):
        backup.run(data, config, t3)
    marker = json.loads((data / backup.MARKER).read_text())
    assert marker["state"] == "v1-backup" and marker["sessions"] == 3 and marker["walBytes"] > 0
    assert marker["backupBytes"] == (data / backup.BACKUP).stat().st_size
    assert (data / backup.BACKUP).stat().st_ino == first.st_ino, "a retry reuses the published backup without space for a second copy"
    assert marker["files"][backup.BACKUP]["sha256"] == hashlib.sha256((data / backup.BACKUP).read_bytes()).hexdigest()
    assert {"v1-backup-files/opencode-config/opencode.json", "v1-backup-files/t3-userdata/settings.json",
            "v1-backup-files/t3-userdata/statev2.sqlite"} <= marker["files"].keys()
    with closing(sqlite3.connect(data / backup.BACKUP)) as kept:
        assert kept.execute("select count(*) from session").fetchone()[0] == 3, "rows only in the WAL are in the backup"
        assert kept.execute("pragma integrity_check").fetchone()[0] == "ok"
    assert hashlib.sha256((data / "opencode.db").read_bytes()).hexdigest() == main_db, "the source store is not written"

    saved = data / backup.FILES
    assert json.loads((saved / "opencode-config/opencode.json").read_text())["plugin"] == ["file:///x/dist/index.js"]
    assert (saved / "opencode-config/plugins/jev-auto.js").exists()
    assert json.loads((saved / "t3-userdata/settings.json").read_text()) == {"providers": {}}
    with closing(sqlite3.connect(saved / "t3-userdata/statev2.sqlite")) as state:
        assert state.execute("select id from projection_threads").fetchall() == [("thread-1",)], "T3 SQLite through the backup API"
    assert not (saved / "t3-userdata/server-runtime.json.lock").exists()
    for path in (data, saved, *saved.rglob("*")):
        assert path.stat().st_mode & 0o077 == 0, f"not private: {path}"
    assert (data / backup.BACKUP).stat().st_mode & 0o077 == 0 and (data / backup.MARKER).stat().st_mode & 0o077 == 0

    writer.execute("create table session_v2 (id text)")
    writer.commit()
    stamp = (data / backup.BACKUP).stat().st_mtime_ns
    backup.run(data, config, t3)
    assert (data / backup.BACKUP).stat().st_mtime_ns == stamp, "after conversion the backup is checked, never replaced"
    with closing(sqlite3.connect(data / backup.BACKUP)) as kept:
        assert "session_v2" not in {r[0] for r in kept.execute("select name from sqlite_master")}
    with patch.object(backup, "sha256", side_effect=AssertionError("re-hashed an unchanged file")):
        backup.run(data, config, t3)
    original = (data / backup.BACKUP).read_bytes()
    at = original.rindex(b"synthetic")
    with open(data / backup.BACKUP, "r+b") as f:
        f.seek(at)
        f.write(b"SYNTHETIC")
    assert (data / backup.BACKUP).stat().st_ino == first.st_ino and (data / backup.BACKUP).stat().st_mtime_ns != stamp
    fails(lambda: backup.run(data, config, t3), "no longer matches")
    with open(data / backup.BACKUP, "r+b") as f:
        f.seek(at)
        f.write(b"synthetic")
    backup.run(data, config, t3)

    kept_backup = root / "kept-backup"
    shutil.copy2(data / backup.BACKUP, kept_backup)
    (data / backup.BACKUP).unlink()
    fails(lambda: backup.run(data, config, t3), "not a valid OpenCode 1 database")
    (data / backup.BACKUP).write_bytes(b"not sqlite")
    fails(lambda: backup.run(data, config, t3), "not a valid OpenCode 1 database")
    with closing(sqlite3.connect(root / "other.db")) as other:
        other.execute("create table session (id text)")
    (root / "other.db").replace(data / backup.BACKUP)
    fails(lambda: backup.run(data, config, t3), "no longer matches")
    corrupt = bytearray(kept_backup.read_bytes())
    at = corrupt.rindex(b"synthetic")
    corrupt[at:at + 9] = b"SYNTHETIC"
    (data / backup.BACKUP).write_bytes(corrupt)
    assert (data / backup.BACKUP).stat().st_size == kept_backup.stat().st_size
    fails(lambda: backup.run(data, config, t3), "no longer matches")
    shutil.copy2(kept_backup, data / backup.BACKUP)
    backup.run(data, config, t3)

    settings = saved / "t3-userdata/settings.json"
    kept_settings = settings.read_bytes()
    settings.unlink()
    fails(lambda: backup.run(data, config, t3), "no longer matches")
    settings.write_bytes(kept_settings)
    settings.chmod(0o600)
    backup.run(data, config, t3)
    shutil.move(saved, root / "kept-files")
    fails(lambda: backup.run(data, config, t3), "no longer matches")
    shutil.move(root / "kept-files", saved)
    backup.run(data, config, t3)
    snapshot_config = saved / "opencode-config/opencode.json"
    good, before = snapshot_config.read_bytes(), snapshot_config.stat()
    with open(snapshot_config, "r+b") as f:
        f.write(good.replace(b"dist", b"evil"))
    os.utime(snapshot_config, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = snapshot_config.stat()
    assert (after.st_size, after.st_ino, after.st_mtime_ns) == (before.st_size, before.st_ino, before.st_mtime_ns)
    fails(lambda: backup.run(data, config, t3), "no longer matches")
    with open(snapshot_config, "r+b") as f:
        f.write(good)
    backup.run(data, config, t3)
    writer.close()
print("PASS: WAL-only rows backed up through the SQLite backup API; source store unchanged; 2.5x space refusal leaves nothing; "
      "marker written last; a retry reuses the published backup and needs space only for the snapshot; private config and T3 state; "
      "later starts never replace the backup; a missing, damaged, swapped or same-size corrupted backup, or a deleted snapshot file "
      "or directory, or a same-size, same-inode snapshot edit with its mtime restored, stops startup", flush=True)

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    data = root / "data"
    writer = v1_store(data)
    writer.execute("create table session_v2 (id text)")
    writer.commit()
    fails(lambda: backup.run(data, root / "config", root / "t3"), "already converted")
    assert not (data / backup.BACKUP).exists() and not (data / backup.MARKER).exists()
    writer.close()
    fresh = root / "fresh"
    backup.run(fresh, root / "config", root / "t3")
    assert json.loads((fresh / backup.MARKER).read_text())["state"] == "no-v1"
    with closing(sqlite3.connect(fresh / "opencode.db")) as db:
        db.execute("create table session_v2 (id text)")
    backup.run(fresh, root / "config", root / "t3")
    assert not (fresh / backup.BACKUP).exists(), "a fresh home's later V2 store is accepted"
print("PASS: converted store without marker refused; fresh home marked no-v1 and its later V2 store accepted", flush=True)
