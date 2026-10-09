#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / 't3-pi-settings.py').exists(), 'Pi settings migration is missing')
        spec = importlib.util.spec_from_file_location('settings', ROOT / 't3-pi-settings.py')
        self.settings = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.settings)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.path = self.home / 'userdata/settings.json'

    def test_fresh_install_seeds_pi_and_existing_codex_defaults(self):
        self.settings.migrate(self.home, 'http://proxy.test')
        data = json.loads(self.path.read_text())
        self.assertEqual(set(data['providerInstances']), {'pi', 'codex', 'claudeAgent'})
        self.assertEqual(data['providerInstances']['pi'], {
            'driver': 'pi', 'displayName': 'Pi', 'enabled': True,
            'config': {'binaryPath': '/scripts/pi.sh'}})
        for key in ['defaultModelSelection', 'textGenerationModelSelection']:
            self.assertEqual(data[key], {'instanceId': 'pi', 'model': 'cliproxy/claude/claude-opus-5-5'})
        codex = data['providerInstances']['codex']
        self.assertTrue(codex['enabled'])
        self.assertEqual(codex['config']['customModels'], ['codex/codex-auto-review'])
        self.assertIn('http://proxy.test/v1', codex['config']['launchArgs'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_migration_preserves_codex_user_fields_and_selection_options(self):
        self.path.parent.mkdir()
        codex = {'driver': 'codex', 'enabled': False, 'config': {'customModels': ['mine']}}
        self.path.write_text(json.dumps({
            'providerInstances': {'opencode': {'driver': 'opencode'}, 'codex': codex,
                                  'pi': {'driver': 'old', 'enabled': False, 'displayName': 'My Pi',
                                         'config': {'userKey': 1, 'binaryPath': 'old'}}},
            'providers': {'opencode': {'enabled': True}, 'codex': {'userKey': 2}},
            'defaultModelSelection': {'instanceId': 'opencode', 'model': 'old',
                                     'options': [{'id': 'variant', 'value': 'high'}]},
            'textGenerationModelSelection': {'instanceId': 'codex', 'model': 'mine'},
            'userKey': 3}))
        self.settings.migrate(self.home, 'http://proxy.test')
        data = json.loads(self.path.read_text())
        self.assertNotIn('opencode', data['providerInstances'])
        self.assertNotIn('opencode', data['providers'])
        self.assertEqual(data['providerInstances']['codex'], codex)
        self.assertEqual(data['providers']['codex'], {'userKey': 2})
        self.assertEqual(data['providerInstances']['pi']['config']['userKey'], 1)
        self.assertEqual(data['providerInstances']['pi'], {
            'driver': 'pi', 'enabled': True, 'displayName': 'Pi',
            'config': {'userKey': 1, 'binaryPath': '/scripts/pi.sh'}})
        self.assertEqual(data['defaultModelSelection'], {
            'instanceId': 'pi', 'model': 'cliproxy/claude/claude-opus-5-5',
            'options': [{'id': 'thinking', 'value': 'high'}]})
        self.assertEqual(data['textGenerationModelSelection'], {'instanceId': 'codex', 'model': 'mine'})
        self.assertEqual(data['userKey'], 3)
        before = self.path.read_bytes()
        self.settings.migrate(self.home, 'http://other.test')
        self.assertEqual(self.path.read_bytes(), before)

    def test_migration_enables_existing_pi_then_preserves_later_user_choices(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({
            'providerInstances': {'opencode': {'driver': 'opencode'},
                                  'pi': {'driver': 'old', 'enabled': False, 'displayName': 'Pi (test)',
                                         'config': {'binaryPath': '/other/pi'}}},
            'defaultModelSelection': {'instanceId': 'opencode', 'model': 'old'},
            'textGenerationModelSelection': {'instanceId': 'opencode', 'model': 'old'}}))
        self.settings.migrate(self.home, '')
        data = json.loads(self.path.read_text())
        self.assertNotIn('opencode', data['providerInstances'])
        self.assertEqual(data['providerInstances']['pi'], {
            'driver': 'pi', 'enabled': True, 'displayName': 'Pi',
            'config': {'binaryPath': '/scripts/pi.sh'}})
        for key in ['defaultModelSelection', 'textGenerationModelSelection']:
            self.assertEqual(data[key], {'instanceId': 'pi', 'model': 'cliproxy/claude/claude-opus-5-5'})
        data['providerInstances']['pi'].update(enabled=False, displayName='My Pi', driver='old')
        data['providerInstances']['pi']['config']['binaryPath'] = '/other/pi'
        self.path.write_text(json.dumps(data))
        self.settings.migrate(self.home, '')
        data = json.loads(self.path.read_text())
        self.assertEqual(data['providerInstances']['pi'], {
            'driver': 'pi', 'enabled': False, 'displayName': 'My Pi',
            'config': {'binaryPath': '/scripts/pi.sh'}})

    def test_without_opencode_preserves_existing_pi_choices(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({
            'providerInstances': {'pi': {'driver': 'old', 'enabled': False, 'displayName': 'My Pi',
                                         'config': {'binaryPath': '/other/pi', 'userKey': 1}}}}))
        self.settings.migrate(self.home, '')
        data = json.loads(self.path.read_text())
        self.assertEqual(data['providerInstances']['pi'], {
            'driver': 'pi', 'enabled': False, 'displayName': 'My Pi',
            'config': {'binaryPath': '/scripts/pi.sh', 'userKey': 1}})

    def test_cache_reset_happens_once_and_text_selection_migrates(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({'textGenerationModelSelection': {
            'instanceId': 'opencode', 'model': 'old', 'options': [{'id': 'variant', 'value': 'low'}]}}))
        cache = self.home / 'caches/pi.json'
        cache.parent.mkdir()
        cache.write_text('stale')
        self.settings.migrate(self.home, '')
        self.assertFalse(cache.exists())
        self.assertTrue((self.home / 'userdata/.pi-cache-reset').is_file())
        selection = json.loads(self.path.read_text())['textGenerationModelSelection']
        self.assertEqual(selection['instanceId'], 'pi')
        self.assertEqual(selection['model'], 'cliproxy/claude/claude-opus-5-5')
        self.assertEqual(selection['options'], [{'id': 'thinking', 'value': 'low'}])
        cache.write_text('new')
        self.settings.migrate(self.home, '')
        self.assertEqual(cache.read_text(), 'new')

    def test_variant_maps_only_pi_levels_and_keeps_other_options(self):
        self.path.parent.mkdir()
        for level in ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'invalid', True]:
            with self.subTest(level=level):
                self.path.write_text(json.dumps({'defaultModelSelection': {
                    'instanceId': 'opencode', 'model': 'old',
                    'options': [{'id': 'variant', 'value': level}, {'id': 'custom', 'value': 'keep'}]}}))
                self.settings.migrate(self.home, '')
                options = json.loads(self.path.read_text())['defaultModelSelection']['options']
                expected = [{'id': 'custom', 'value': 'keep'}]
                if level not in ['invalid', True]:
                    expected.insert(0, {'id': 'thinking', 'value': level})
                self.assertEqual(options, expected)

    def test_variant_filter_keeps_non_dict_options_unchanged(self):
        self.path.parent.mkdir()
        options = [None, 'keep', True, 7, ['keep'], {'id': 'variant', 'value': 'high'},
                   {'id': 'variant', 'value': 'invalid'}, {'id': 'custom', 'value': 'keep'}]
        self.path.write_text(json.dumps({key: {
            'instanceId': 'opencode', 'model': 'old', 'options': options}
            for key in ['defaultModelSelection', 'textGenerationModelSelection']}))
        self.settings.migrate(self.home, '')
        data = json.loads(self.path.read_text())
        for key in ['defaultModelSelection', 'textGenerationModelSelection']:
            self.assertEqual(data[key]['options'], [None, 'keep', True, 7, ['keep'],
                             {'id': 'thinking', 'value': 'high'}, {'id': 'custom', 'value': 'keep'}])


class PackageTests(unittest.TestCase):
    def test_list_requires_every_registered_package_as_a_complete_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            agent = home / '.pi/agent'
            agent.mkdir(parents=True)
            package = home / 'pkg'
            package.mkdir()
            safety = home / 'index.js'
            safety.touch()
            (agent / 'settings.json').write_text(json.dumps({'packages': [str(package), str(safety)]}))
            command = ['python3', str(ROOT / 'pi-setup.py'), 'check-packages', str(home)]
            for listing, success in [(f'User packages:\n  {package}\n    {package}\n  {safety}\n    {safety}\n', True),
                                     (f'  {package}-extra\n  {safety}\n', False),
                                     (f'  {package}\n', False)]:
                result = subprocess.run(command, input=listing, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, success, result.stderr)
                if not success:
                    missing = package if '-extra' in listing else safety
                    self.assertIn(str(missing), result.stderr)
                    self.assertLess(result.stderr.index(str(missing)),
                                    result.stderr.index('pi-setup: configuration failed'))

    def test_real_pi_listing_rejects_missing_directory_or_file(self):
        listing = '''User packages:
  /tmp/opencode/pi-test/node_modules/pi-cliproxyapi-provider
    /tmp/opencode/pi-test/node_modules/pi-cliproxyapi-provider
  /tmp/opencode/rv4.2zFz/pkgs/real
    /tmp/opencode/rv4.2zFz/pkgs/real
  /tmp/opencode/rv4.2zFz/pkgs/missing
'''
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            agent = home / '.pi/agent'
            agent.mkdir(parents=True)
            provider = home / 'provider'
            provider.mkdir()
            real = home / 'real'
            real.mkdir()
            for missing in [home / 'missing', home / 'index.js']:
                with self.subTest(missing=missing.name):
                    fixture = listing.replace('/tmp/opencode/pi-test/node_modules/pi-cliproxyapi-provider', str(provider))
                    fixture = fixture.replace('/tmp/opencode/rv4.2zFz/pkgs/real', str(real))
                    fixture = fixture.replace('/tmp/opencode/rv4.2zFz/pkgs/missing', str(missing))
                    (agent / 'settings.json').write_text(json.dumps({
                        'packages': [str(provider), str(real), str(missing)]}))
                    command = ['python3', str(ROOT / 'pi-setup.py'), 'check-packages', str(home)]
                    result = subprocess.run(command, input=fixture, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(str(missing), result.stderr)
                    self.assertLess(result.stderr.index(str(missing)),
                                    result.stderr.index('pi-setup: configuration failed'))
                    if missing.suffix:
                        missing.touch()
                    else:
                        missing.mkdir()
                    result = subprocess.run(command, input=fixture, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
