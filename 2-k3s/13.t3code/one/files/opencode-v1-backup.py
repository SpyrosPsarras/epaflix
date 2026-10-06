#!/usr/bin/env python3
"""Keep an OpenCode 1 snapshot before OpenCode 2 converts the database in place.

Runs first in the entrypoint, before anything opens the database or rewrites the
OpenCode config. OpenCode 2 converts opencode.db in place, so after its first
start the old store exists nowhere else. The snapshot is:

  <data>/opencode.db.v1-backup        SQLite online backup (a file copy misses the WAL)
  <data>/v1-backup-files/             the OpenCode config and T3's settings and state
  <data>/opencode.db.v1-backup.marker written last; startup trusts the backup only with it

The marker says "v1-backup" or "no-v1" (no database existed). A converted
database without a marker is refused, so a V2 database is never labelled V1.
A "v1-backup" marker records the size, mtime, ctime, inode and SHA-256 of the
backup and of every snapshot file; each later start checks them, hashing again
only a file whose size, mtime, ctime or inode changed, and runs PRAGMA
quick_check on the backup. The full integrity_check runs once, when the
backup is made: on a multi-gigabyte store it takes minutes, and the hashes
already prove the bytes are the ones it passed.
Nothing here deletes a backup: the owner removes it after accepting OpenCode 2.
"""
from contextlib import closing
import hashlib
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

SPACE_FACTOR = 2.5
BACKUP = "opencode.db.v1-backup"
MARKER = "opencode.db.v1-backup.marker"
FILES = "v1-backup-files"


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def query(path, sql):
    # Read-only: the store is never written, checkpointed or converted here.
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)) as db:
        return db.execute(sql).fetchall()


def is_v2(path):
    return ("session_v2",) in query(path, "select name from sqlite_master where type = 'table'")


def sessions(path):
    return query(path, "select count(*) from session")[0][0]


def integrity(path):
    return query(path, "pragma integrity_check") == [("ok",)]


def sqlite_copy(source, target):
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
    os.chmod(target, 0o600)
    fd = os.open(target, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_marker(directory, state, **extra):
    record = {"state": state, "createdAt": datetime.now(timezone.utc).isoformat(), **extra}
    temporary = directory / f"{MARKER}.tmp-{os.getpid()}"
    temporary.write_text(json.dumps(record, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, directory / MARKER)
    print(f"opencode-v1-backup: marker {state}", flush=True)


def t3_files(t3):
    return [item for item in sorted(t3.iterdir()) if item.is_file() and item.suffix in (".sqlite", ".json")] if t3.is_dir() else []


def snapshot_bytes(config, t3):
    tree = [p for p in config.rglob("*") if p.is_file() and not p.is_symlink()] if config.is_dir() else []
    wals = [p.with_name(p.name + "-wal") for p in t3_files(t3)]
    return sum(p.stat().st_size for p in tree + t3_files(t3) + [w for w in wals if w.exists()])


def snapshot_files(data, config, t3):
    target = data / FILES
    if target.exists():
        shutil.rmtree(target)
    temporary = data / f"{FILES}.tmp-{os.getpid()}"
    shutil.rmtree(temporary, ignore_errors=True)
    private_dir(temporary)
    if config.is_dir():
        shutil.copytree(config, temporary / "opencode-config", symlinks=True)
    state = temporary / "t3-userdata"
    private_dir(state)
    for item in t3_files(t3):
        if item.suffix == ".sqlite":
            sqlite_copy(item, state / item.name)
        else:
            shutil.copy2(item, state / item.name)
            os.chmod(state / item.name, 0o600)
    for path in (temporary, *temporary.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o700)
        elif path.is_file() and not path.is_symlink():
            path.chmod(0o600)
    os.replace(temporary, target)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def identity(path):
    stat = path.stat()
    return {"bytes": stat.st_size, "mtimeNs": stat.st_mtime_ns, "ctimeNs": stat.st_ctime_ns, "inode": stat.st_ino}


def manifest(data, known=None):
    paths = [data / BACKUP, *sorted((data / FILES).rglob("*"))]
    record = {}
    for path in paths:
        name = str(path.relative_to(data))
        if path.is_symlink():
            record[name] = {"link": os.readlink(path)}
        elif path.is_file():
            before = identity(path)
            old = (known or {}).get(name, {})
            digest = old["sha256"] if all(old.get(k) == v for k, v in before.items()) else sha256(path)
            if identity(path) != before:
                refuse(f"{path} changed while it was checked")
            record[name] = {**before, "sha256": digest}
    return record


def content(files):
    return {name: (entry.get("link"), entry.get("bytes"), entry.get("sha256")) for name, entry in files.items()}


def refuse(reason):
    raise SystemExit(f"opencode-v1-backup: {reason}; refusing to start")


def verified_backup(data, files=None):
    backup = data / BACKUP
    try:
        valid = backup.is_file() and not is_v2(backup) and query(backup, "pragma quick_check" if files else "pragma integrity_check") == [("ok",)]
    except sqlite3.DatabaseError:
        valid = False
    if not valid:
        refuse(f"{backup} is missing or not a valid OpenCode 1 database")
    if files is not None and (not (data / FILES).is_dir() or content(manifest(data, files)) != content(files)):
        refuse(f"{backup} or {data / FILES} no longer matches the sizes and hashes in {data / MARKER}")


def need(data, size, what):
    free = shutil.disk_usage(data).free
    if free < size:
        refuse(f"{free} bytes free, need {int(size)} ({what})")


def run(data, config, t3):
    private_dir(data)
    db, marker, published = data / "opencode.db", data / MARKER, data / BACKUP
    if marker.exists():
        record = json.loads(marker.read_text())
        state = record["state"]
        if state == "v1-backup":
            verified_backup(data, record["files"])
            print("opencode-v1-backup: valid backup already kept; not touching it", flush=True)
        elif state != "no-v1":
            raise SystemExit(f"opencode-v1-backup: unknown marker state {state!r}")
        return
    for stale in data.glob("*.tmp-*"):
        shutil.rmtree(stale, ignore_errors=True) if stale.is_dir() else stale.unlink()
    if not db.exists():
        write_marker(data, "no-v1")
        return
    if is_v2(db):
        raise SystemExit("opencode-v1-backup: opencode.db is already converted and has no V1 backup marker; refusing to label it a V1 backup. "
                         "Restore the V1 database or create the marker by hand after checking the files (migration/README.md)")
    wal = db.with_name(db.name + "-wal")
    size = db.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
    if published.exists():
        # A run that stopped between publishing the backup and writing the marker. Keep it if it is valid.
        verified_backup(data)
    else:
        need(data, SPACE_FACTOR * size, "2.5 x database + WAL")
        temporary = data / f"{BACKUP}.tmp-{os.getpid()}"
        start = time.time()
        sqlite_copy(db, temporary)
        if not integrity(temporary) or sessions(temporary) != sessions(db):
            temporary.unlink()
            refuse("backup failed its integrity check")
        os.replace(temporary, published)
        print(f"opencode-v1-backup: copied and verified {size} bytes in {int(time.time() - start)} s", flush=True)
    need(data, snapshot_bytes(config, t3), "config and T3 state snapshot")
    snapshot_files(data, config, t3)
    write_marker(data, "v1-backup", dbBytes=db.stat().st_size, walBytes=wal.stat().st_size if wal.exists() else 0,
                 backupBytes=published.stat().st_size, sessions=sessions(published), files=manifest(data))


def main():
    home = Path(os.environ.get("HOME", "~")).expanduser()
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share") / "opencode"
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "opencode"
    t3 = Path(os.environ.get("T3CODE_HOME") or home / ".t3") / "userdata"
    run(data, config, t3)


if __name__ == "__main__":
    main()
