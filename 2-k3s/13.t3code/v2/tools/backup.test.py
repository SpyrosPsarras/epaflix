#!/usr/bin/env python3
import importlib.util
import pathlib
import sqlite3
import tempfile
import unittest

path = pathlib.Path(__file__).parents[1] / 'files/backup.py'
spec = importlib.util.spec_from_file_location('backup', path)
module = importlib.util.module_from_spec(spec)
if path.exists():
    spec.loader.exec_module(module)


class Backup(unittest.TestCase):
    def test_consistent_database_and_two_copy_retention(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp)
            (home / 'userdata').mkdir()
            with sqlite3.connect(home / 'userdata/statev2.sqlite') as db:
                db.execute('create table data(value)')
                db.execute("insert into data values ('saved')")
            for label in ['1', '2', '3']:
                module.backup(home, label, reserve=0)
            copies = sorted((home / 'backups').iterdir())
            self.assertEqual([p.name for p in copies], ['2', '3'])
            with sqlite3.connect(copies[-1] / 'statev2.sqlite') as db:
                self.assertEqual(db.execute('select value from data').fetchone()[0], 'saved')

    def test_space_requirement_blocks_without_pruning(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp)
            (home / 'userdata').mkdir()
            with sqlite3.connect(home / 'userdata/statev2.sqlite') as db:
                db.execute('create table data(value)')
            with self.assertRaises(OSError):
                module.backup(home, 'blocked', reserve=10**18)
            self.assertFalse((home / 'backups/blocked').exists())


if __name__ == '__main__':
    unittest.main()
