#!/usr/bin/env python3
import importlib.util
import pathlib
import tempfile
import unittest

path = pathlib.Path(__file__).parents[1] / 'files/provision.py'
spec = importlib.util.spec_from_file_location('provision', path)
module = importlib.util.module_from_spec(spec)
if path.exists():
    spec.loader.exec_module(module)


class Provision(unittest.TestCase):
    def test_seeds_and_refreshes_managed_file(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp)
            self.assertEqual(module.install(home, {'.kube/config': 'first'}), [])
            self.assertEqual(module.install(home, {'.kube/config': 'second'}), [])
            self.assertEqual((home / '.kube/config').read_text(), 'second')
            self.assertEqual((home / '.kube/config').stat().st_mode & 0o777, 0o600)

    def test_preserves_user_edits_and_reports_conflict(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp)
            module.install(home, {'.kube/config': 'first'})
            (home / '.kube/config').write_text('user edit')
            self.assertEqual(module.install(home, {'.kube/config': 'second'}), ['.kube/config'])
            self.assertEqual((home / '.kube/config').read_text(), 'user edit')

    def test_auth_cache_seed_only(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp)
            module.install(home, {'.azure/msal_token_cache.json': 'first'})
            module.install(home, {'.azure/msal_token_cache.json': 'second'})
            self.assertEqual((home / '.azure/msal_token_cache.json').read_text(), 'first')

    def test_rejects_history_traversal_and_unknown_paths(self):
        for name in ['.t3/userdata/state.sqlite', '.codex/sessions/a', '../outside', '/absolute', '.ssh/id_ed25519', '.azure/logs/a']:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp, self.assertRaises(ValueError):
                module.install(pathlib.Path(temp), {name: 'data'})

    def test_symlink_destination_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp)
            (home / '.kube').symlink_to('/tmp')
            with self.assertRaises(ValueError):
                module.install(home, {'.kube/config': 'data'})

    def test_metadata_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp)
            (home / '.local').symlink_to('/tmp')
            with self.assertRaises(ValueError):
                module.install(home, {'.kube/config': 'data'})


if __name__ == '__main__':
    unittest.main()
