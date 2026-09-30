#!/usr/bin/env python3
import importlib.util
import pathlib
import subprocess
import tempfile
import unittest

path = pathlib.Path(__file__).parents[1] / 'files/bootstrap-projects.py'
spec = importlib.util.spec_from_file_location('bootstrap', path)
module = importlib.util.module_from_spec(spec)
if path.exists():
    spec.loader.exec_module(module)


class Bootstrap(unittest.TestCase):
    def test_clone_and_rerun_preserves_local_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            remote = root / 'remote.git'
            subprocess.run(['git', 'init', '-q', '--bare', str(remote)], check=True)
            destination = root / 'clone'
            module.clone_projects([{'remoteUrl': str(remote), 'destination': str(destination)}])
            (destination / 'local').write_text('keep')
            module.clone_projects([{'remoteUrl': str(remote), 'destination': str(destination)}])
            self.assertEqual((destination / 'local').read_text(), 'keep')

    def test_duplicate_destination_rejected_before_cloning(self):
        with self.assertRaises(ValueError):
            module.clone_projects([{'remoteUrl': 'a', 'destination': '/tmp/a'}, {'remoteUrl': 'b', 'destination': '/tmp/a'}])

    def test_existing_non_repository_rejected(self):
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(ValueError):
            module.clone_projects([{'remoteUrl': 'a', 'destination': temp}])


if __name__ == '__main__':
    unittest.main()
