#!/usr/bin/env python3
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('forgejo_grant', Path(__file__).with_name('forgejo-grant.py'))
grant = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grant)
TOKEN = b'0123456789abcdef0123456789abcdef01234567'


def encoded(value):
    return base64.b64encode(value).decode()


class FakeCluster(grant.Cluster):
    def __init__(self, pod=False, reviewed=None, users=('skills-work-bot',)):
        super().__init__('synthetic')
        grants = {'model-token': b'synthetic-model', 'hub-token': b'work.synthetic'}
        runtime = {'environment': 'work', 'grantSha256': reviewed or {name: grant.digest(value) for name, value in grants.items()},
                   'sha256': {}}
        self.objects = {
            ('statefulset', 'work'): {'spec': {'replicas': 0}},
            ('pod', 'work-0'): {'metadata': {}} if pod else None,
            ('secret', 't3-runtime-grants'): {'metadata': {'resourceVersion': '1'},
                                              'data': {name: encoded(value) for name, value in grants.items()}},
            ('secret', 't3-runtime-inputs'): {'metadata': {'resourceVersion': '2'},
                                              'data': {'runtime.json': encoded(json.dumps(runtime).encode())}}}
        self.users, self.minted = list(users), 0

    def get(self, namespace, kind, name):
        assert namespace == 't3code-work'
        return self.objects[(kind, name)]

    def patch(self, namespace, secret, version, operations):
        obj = self.objects[('secret', secret)]
        assert obj['metadata']['resourceVersion'] == version
        for operation in operations:
            obj['data'][operation['path'].removeprefix('/data/')] = operation['value']
        obj['metadata']['resourceVersion'] += '+'

    def forgejo(self, *args):
        if args[0] == 'list':
            return ' '.join(['ID', 'Username', *self.users]).encode()
        self.minted += 1
        assert args[args.index('--username') + 1] in self.users
        return TOKEN + b'\n'


class ForgejoGrantTests(unittest.TestCase):
    def test_mints_once_and_binds_the_digest_preserving_key_order(self):
        cluster = FakeCluster()
        for _ in range(2):
            grant.install(cluster, 'work')
        self.assertEqual(cluster.minted, 1)
        data = cluster.objects[('secret', 't3-runtime-grants')]['data']
        self.assertEqual(base64.b64decode(data['forgejo-token']), TOKEN)
        raw = base64.b64decode(cluster.objects[('secret', 't3-runtime-inputs')]['data']['runtime.json'])
        runtime = json.loads(raw)
        self.assertEqual(runtime['grantSha256']['forgejo-token'], hashlib.sha256(TOKEN).hexdigest())
        self.assertEqual(json.dumps(runtime).encode(), raw)

    def test_refuses_a_running_backend_unreviewed_grants_or_a_missing_bot(self):
        for cluster in (FakeCluster(pod=True), FakeCluster(reviewed={'model-token': 'stale'}), FakeCluster(users=())):
            with self.subTest(), self.assertRaises(ValueError):
                grant.install(cluster, 'work')
            self.assertEqual(cluster.minted, 0)

    def test_refuses_a_stored_token_that_differs_from_its_review(self):
        for stored in (b'f' * 40, b'not-a-token', None):
            cluster = FakeCluster()
            grant.install(cluster, 'work')
            data = cluster.objects[('secret', 't3-runtime-grants')]['data']
            if stored is None:
                del data['forgejo-token']
            else:
                data['forgejo-token'] = encoded(stored)
            runtime = cluster.objects[('secret', 't3-runtime-inputs')]['data']['runtime.json']
            with self.subTest(stored=stored), self.assertRaises(ValueError):
                grant.install(cluster, 'work')
            self.assertEqual((cluster.minted, cluster.objects[('secret', 't3-runtime-inputs')]['data']['runtime.json']), (1, runtime))


if __name__ == '__main__':
    unittest.main()
