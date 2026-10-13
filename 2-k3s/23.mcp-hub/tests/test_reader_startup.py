import asyncio
import importlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'files'))
import httpx2
from vaultwarden_reader import RestrictedVaultReader, private_bytes
from vaultwarden_reader_http import http_app, load_config
import test_vaultwarden_reader as fixture


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'projection'
        self.source.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.startup = importlib.import_module('reader_startup')
        private = self.state / 'private'
        config = {'environment': 'homelab', 'reader': {
            'server_url': 'https://vault.homelab.example.test', 'user_id': fixture.USER,
            'organization_id': fixture.ORG, 'collection_ids': [fixture.COLLECTION],
            'credentials_dir': str(private / 'credentials'), 'scratch_dir': str(private / 'scratch'),
            'executable': '/tools/reader_bw_bridge.cjs'},
            'metadata_token_file': str(private / 'metadata-token'),
            'broker_token_file': str(private / 'broker-token')}
        self.values = {'config.json': json.dumps(config).encode(),
                       'client-id': ('user.' + fixture.USER).encode(), 'client-secret': b'synthetic-client-secret',
                       'password': b' \t synthetic-password \r\n',
                       'metadata-token': b'synthetic-independent-metadata-token-0000',
                       'broker-token': b'synthetic-independent-broker-token-000000'}
        data = self.source / '..data'
        data.mkdir()
        for name, value in self.values.items():
            (data / name).write_bytes(value)
            (data / name).chmod(0o440)
            (self.source / name).symlink_to('..data/' + name)

    def stage(self):
        return self.startup.stage(self.source, self.state)

    def test_projection_copied_exactly_private_without_login(self):
        lock, config = self.stage()
        self.addCleanup(lock.close)
        self.assertEqual(load_config(config)[0].user_id, fixture.USER)
        private = self.state / 'private'
        for name, value in self.values.items():
            target = private / ('credentials/' + name if name in {'client-id', 'client-secret', 'password'} else name)
            self.assertEqual(private_bytes(target, 65536), value)
            self.assertEqual(target.stat().st_uid, os.getuid())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        for path in [private, private / 'credentials', private / 'scratch']:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(list((private / 'scratch').iterdir()), [])

    def test_changed_projection_latches_routes_without_relogin_or_health_restart(self):
        lock, config_path = self.stage()
        self.addCleanup(lock.close)
        config, metadata, broker = load_config(config_path)
        reader = RestrictedVaultReader(config)
        self.addCleanup(reader.close)
        expected = self.startup.projected_values(self.source)
        app = self.startup.ProjectionGuard(http_app(reader, metadata, broker), self.source, expected, reader)

        async def check():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url='http://reader.example.test') as client:
                self.assertEqual((await client.get('/metadata/list', headers={'Authorization': 'Bearer ' + metadata})).status_code, 405)
                path = self.source / '..data/password'
                path.chmod(0o600)
                path.write_bytes(b'changed synthetic password')
                self.assertEqual((await client.get('/metadata/list', headers={'Authorization': 'Bearer ' + metadata})).status_code, 503)
                path.write_bytes(self.values['password'])
                self.assertEqual((await client.get('/metadata/list', headers={'Authorization': 'Bearer ' + metadata})).status_code, 503)
                self.assertEqual((await client.get('/healthz')).status_code, 200)
                self.assertEqual(list((self.state / 'private/scratch').iterdir()), [])
        asyncio.run(check())

    def test_single_owner_restart_removes_only_dedicated_orphans(self):
        lock, _ = self.stage()
        with self.assertRaises(Exception):
            self.stage()
        unrelated = self.state / 'unrelated'
        unrelated.write_bytes(b'keep')
        orphan = self.state / 'private/scratch/orphan'
        orphan.mkdir()
        (orphan / 'data').write_bytes(b'synthetic-old-session')
        outside = self.root / 'outside'
        outside.write_bytes(b'keep-outside')
        (orphan / 'link').symlink_to(outside)
        lock.close()
        self.values['password'] = b'new synthetic password '
        (self.source / '..data/password').chmod(0o600)
        (self.source / '..data/password').write_bytes(self.values['password'])
        (self.source / '..data/password').chmod(0o440)
        lock, _ = self.stage()
        self.addCleanup(lock.close)
        self.assertFalse(orphan.exists())
        self.assertEqual(unrelated.read_bytes(), b'keep')
        self.assertEqual(outside.read_bytes(), b'keep-outside')
        self.assertEqual(private_bytes(self.state / 'private/credentials/password', 16384), self.values['password'])

    def test_bad_projection_or_path_refuses_without_old_state(self):
        public = self.source / '..data/password'
        public.chmod(0o444)
        with self.assertRaises(Exception):
            self.stage()
        public.chmod(0o440)
        for name, data in [('password', b'x' * 16385), ('config.json', b'{}')]:
            with self.subTest(name=name):
                (self.source / '..data' / name).chmod(0o600)
                (self.source / '..data' / name).write_bytes(data)
                with self.assertRaises(Exception):
                    self.stage()
                self.assertFalse((self.state / 'private').exists())
                (self.source / '..data' / name).write_bytes(self.values[name])
                (self.source / '..data' / name).chmod(0o440)


if __name__ == '__main__':
    unittest.main()
