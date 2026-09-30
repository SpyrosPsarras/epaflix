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

    def test_json_key_refresh_preserves_user_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp)
            name = '.config/opencode/opencode.json'
            module.install(home, {name: '{"provider":{"cliproxy":{"name":"old"}},"model":"old"}'})
            (home / name).write_text('{"provider":{"cliproxy":{"name":"old"}},"model":"user","local":true}')
            conflicts = module.install(home, {name: '{"provider":{"cliproxy":{"name":"new"}},"model":"new"}'})
            import json
            result = json.loads((home / name).read_text())
            self.assertEqual(result['provider']['cliproxy']['name'], 'new')
            self.assertEqual(result['model'], 'user')
            self.assertTrue(result['local'])
            self.assertIn(name + ':model', conflicts)

    def test_toml_managed_value_refresh_preserves_user_value(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp)
            name = '.codex/config.toml'
            module.install(home, {name: '[mcp_servers.test]\nurl = "old"\nmodel = "old"\n'})
            (home / name).write_text('[mcp_servers.test]\nurl = "old"\nmodel = "user"\n')
            conflicts = module.install(home, {name: '[mcp_servers.test]\nurl = "new"\nmodel = "new"\n'})
            self.assertIn('url = "new"', (home / name).read_text())
            self.assertIn('model = "user"', (home / name).read_text())
            self.assertTrue(conflicts)

    def test_new_toml_section_with_two_keys_survives_next_refresh(self):
        import tomllib
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp)
            name = '.codex/config.toml'
            module.install(home, {name: 'model = "a"\n'})
            incoming = 'model = "a"\n[mcp_servers.hub]\nurl = "http://hub"\nbearer_token_env_var = "HUB_TOKEN"\n'
            module.install(home, {name: incoming})
            parsed = tomllib.loads((home / name).read_text())
            self.assertEqual(parsed['mcp_servers']['hub']['bearer_token_env_var'], 'HUB_TOKEN')
            module.install(home, {name: incoming})
            self.assertEqual(tomllib.loads((home / name).read_text()), parsed)

    def test_toml_insert_does_not_shift_later_replacement(self):
        import tomllib
        old = '[a]\nx = 1\n[b]\ny = 1\n'
        incoming = '[a]\nx = 1\nnew = 2\n[b]\ny = 2\n'
        merged, conflicts = module.merge_toml(old, old, incoming)
        self.assertEqual(tomllib.loads(merged), {'a': {'x': 1, 'new': 2}, 'b': {'y': 2}})
        self.assertEqual(conflicts, [])


if __name__ == '__main__':
    unittest.main()
