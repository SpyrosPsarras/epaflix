#!/usr/bin/env python3
"""Keep two verified v2 SQLite recovery copies and associated runtime settings."""
import argparse
import os
import pathlib
import shutil
import sqlite3
import tempfile


def backup(home, label, reserve=8 * 1024**3):
    source = home / 'userdata/statev2.sqlite'
    root = home / 'backups'
    settings = [home / 'userdata/settings.json', home / 'monitor.json']
    size = source.stat().st_size + sum(p.stat().st_size for p in settings if p.exists())
    if shutil.disk_usage(home).free < reserve + size * 2:
        raise OSError('Insufficient backup and free-space headroom')
    if not label or '/' in label or label in {'.', '..'}:
        raise ValueError('Invalid backup label')
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink():
        raise ValueError('Backup directory must not be a symlink')
    destination = root / label
    if destination.exists():
        return destination
    temporary = pathlib.Path(tempfile.mkdtemp(prefix='.pending-', dir=root))
    try:
        with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(temporary / 'statev2.sqlite') as target:
            src.backup(target)
            if target.execute('pragma integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Backup integrity check failed')
        for path in settings:
            if path.exists():
                shutil.copy2(path, temporary / path.name)
        for path in temporary.iterdir():
            path.chmod(0o600)
        if shutil.disk_usage(home).free < reserve:
            raise OSError('Backup crossed free-space floor')
        os.replace(temporary, destination)
        copies = sorted((p for p in root.iterdir() if p.is_dir() and not p.name.startswith('.')), key=lambda p: p.stat().st_mtime)
        for old in copies[:-2]:
            if old.is_symlink():
                raise ValueError('Unexpected backup symlink')
            shutil.rmtree(old)
        return destination
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=pathlib.Path, required=True)
    parser.add_argument('--label', required=True)
    args = parser.parse_args()
    backup(args.home, args.label)
    print('v2 recovery backup verified')
