#!/usr/bin/env python3
"""Offline imports use synthetic kubeconfigs and never request credentials."""
import base64
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


importer = module('cluster_importer', Path(__file__).with_name('add-cluster.py'))
fixture = module('work_fixture', importer.ROOT / '13.t3code/env/tools/work-runtime.test.py')


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inputs, self.output, self.source = self.root / 'inputs', self.root / 'staged', self.root / 'aks.yaml'
        fixture.fixture(self.inputs)
        self.kube = json.loads((self.inputs / 'kubeconfig').read_bytes())
        self.kube['clusters'] = self.kube['clusters'][:1]
        self.kube['users'] = self.kube['users'][:1]
        self.kube['contexts'] = self.kube['contexts'][:1]
        self.kube['users'][0]['user'] = {'exec': {
            'apiVersion': 'client.authentication.k8s.io/v1beta1', 'command': 'kubelogin', 'env': None,
            'args': ['get-token', '--tenant-id', '44444444-4444-4444-4444-444444444444',
                     '--server-id', '22222222-2222-2222-2222-222222222222',
                     '--client-id', '66666666-6666-6666-6666-666666666666',
                     '--environment', 'AzurePublicCloud', '--login', 'devicecode']}}
        self.enterContext(patch.object(importer.auth, 'az', side_effect=AssertionError('Azure must not run')))

    def stage(self, **kwargs):
        self.source.write_text(json.dumps(self.kube))
        options = {'directory': self.inputs, 'kubeconfig': self.source, 'output': self.output,
                   'name': 'k8s-dh-sales-uksouth', 'subscription': '55555555-5555-5555-5555-555555555555',
                   'account': 'sales@work.example.test'}
        options.update(kwargs)
        importer.stage(**options)

    def decoded_instructions(self, directory):
        entry = json.loads((directory / 'bundle.json').read_text())['files']['instructions.md']
        return base64.b64decode(entry['data']).decode()

    def change_bundle(self, change):
        path = self.inputs / 'bundle.json'
        bundle = json.loads(path.read_text())
        entry = bundle['files']['instructions.md']
        entry['data'] = base64.b64encode(change(base64.b64decode(entry['data']).decode()).encode()).decode()
        path.write_text(json.dumps(bundle))
        fixture.refresh(self.inputs)

    def test_import_pins_context_scope_preserves_inputs_and_future_services_and_is_private(self):
        services = self.inputs / 'services.json'
        services.write_text(json.dumps({'environment': 'work', 'services': ['searxng', 'notion']}))
        fixture.refresh(self.inputs)
        before = {path.name: path.read_bytes() for path in self.inputs.iterdir()}
        self.stage()
        self.assertEqual({path.name: path.read_bytes() for path in self.inputs.iterdir()}, before)
        for name in ('services.json', 'policies.json', 'authorized_keys'):
            self.assertEqual((self.output / name).read_bytes(), before[name])
        runtime = json.loads((self.output / 'runtime.json').read_text())
        previous = json.loads(before['runtime.json'])
        self.assertEqual({k: v for k, v in runtime.items() if k != 'sha256'},
                         {k: v for k, v in previous.items() if k != 'sha256'})
        reviewed = importer.auth.validate(self.output)
        self.assertEqual(reviewed['contexts'][:-1], json.loads(before['work-auth.json'])['contexts'])
        pin = reviewed['contexts'][-1]
        self.assertEqual(pin['authentication']['account']['name'], 'sales@work.example.test')
        self.assertEqual(pin['authentication']['tenantId'], '44444444-4444-4444-4444-444444444444')
        self.assertEqual(pin['name'], 'k8s-dh-sales-uksouth')
        config = importer.auth.load_kube(self.output / 'kubeconfig')
        self.assertEqual(config['current-context'], 'work-first')
        self.assertEqual([entry for entry in config['clusters'] if entry['name'] != pin['name']],
                         importer.auth.load_kube(self.inputs / 'kubeconfig')['clusters'])
        user = next(entry['user'] for entry in config['users'] if entry['name'] == pin['user'])
        cluster = next(entry['cluster'] for entry in config['clusters'] if entry['name'] == pin['cluster'])
        self.assertEqual(user['exec']['args'], ['token', pin['name']])
        self.assertEqual(cluster, self.kube['clusters'][0]['cluster'])
        self.assertEqual(self.decoded_instructions(self.output),
                         self.decoded_instructions(self.inputs).replace(importer.OLD_ENVIRONMENT, importer.APPROVED_ENVIRONMENT))
        old_bundle, new_bundle = (json.loads((d / 'bundle.json').read_text()) for d in (self.inputs, self.output))
        old_bundle['files'].pop('instructions.md')
        new_bundle['files'].pop('instructions.md')
        self.assertEqual(old_bundle, new_bundle)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        for path in self.output.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            if path.name != 'runtime.json':
                self.assertEqual(importer.hashlib.sha256(path.read_bytes()).hexdigest(), runtime['sha256'][path.name])
        self.assertFalse((self.root / '.azure').exists())

    def test_repeated_additions_namespace_conflicting_source_names_and_keep_existing_guidance(self):
        self.stage()
        next_output = self.root / 'next'
        self.stage(directory=self.output, output=next_output, name='another-approved-work-cluster')
        reviewed = importer.auth.validate(next_output)
        self.assertEqual(len(reviewed['contexts']), 4)
        self.assertEqual(self.decoded_instructions(self.output), self.decoded_instructions(next_output))
        with self.assertRaisesRegex(importer.ImportError, 'collides'):
            self.stage(directory=next_output, output=self.root / 'duplicate')
        self.assertFalse((self.root / 'duplicate').exists())

    def test_existing_output_and_names_across_any_reference_group_never_overwrite(self):
        self.output.mkdir()
        sentinel = self.output / 'keep'
        sentinel.write_text('unchanged')
        with self.assertRaisesRegex(importer.ImportError, 'new private directory'):
            self.stage()
        self.assertEqual(sentinel.read_text(), 'unchanged')
        for name in ('work-first', '../escape', 'with/slash'):
            with self.assertRaises(importer.ImportError):
                self.stage(output=self.root / 'unused', name=name)
        self.assertFalse((self.root / 'unused').exists())
        with self.assertRaisesRegex(importer.ImportError, 'outside'):
            self.stage(output=self.inputs / 'nested')
        self.assertFalse((self.inputs / 'nested').exists())

    def test_missing_conflicting_metadata_and_unknown_source_selector_refuse(self):
        for options in ({'account': None}, {'subscription': None}, {'tenant': '99999999-9999-9999-9999-999999999999'},
                        {'audience': 'https://wrong.example.test'}, {'source_context': 'missing'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.stage(**options)
            self.assertFalse(self.output.exists())
        self.kube['current-context'] = ''
        with self.assertRaises(importer.ImportError):
            self.stage()
        self.stage(source_context='work-first')

    def test_explicit_metadata_can_import_an_empty_user_and_legacy_azure_provider(self):
        for user in ({}, {'auth-provider': {'name': 'azure', 'config': {
                'tenant-id': '44444444-4444-4444-4444-444444444444',
                'apiserver-id': '22222222-2222-2222-2222-222222222222'}}}):
            with self.subTest(user=user):
                self.kube['users'][0]['user'] = user
                self.stage(output=self.root / ('empty' if not user else 'legacy'),
                           tenant='44444444-4444-4444-4444-444444444444',
                           audience='22222222-2222-2222-2222-222222222222')

    def test_source_tls_overrides_external_files_static_credentials_and_plugin_env_refuse(self):
        original = copy.deepcopy(self.kube)
        mutations = [
            lambda obj: obj['clusters'][0]['cluster'].update({'insecure-skip-tls-verify': True}),
            lambda obj: obj['clusters'][0]['cluster'].update({'certificate-authority': '/private/ca'}),
            lambda obj: obj['clusters'][0]['cluster'].update({'proxy-url': 'http://proxy.example.test'}),
            lambda obj: obj['clusters'][0]['cluster'].update({'tls-server-name': 'other.example.test'}),
            lambda obj: obj['clusters'][0]['cluster'].update(server='http://insecure.example.test'),
            lambda obj: obj['users'][0]['user'].update(token='synthetic-static-token'),
            lambda obj: obj['users'][0]['user'].update({'client-key': '/private/key'}),
            lambda obj: obj['users'][0]['user']['exec'].update(env=[{'name': 'AZURE_CONFIG_DIR', 'value': '/foreign'}]),
            lambda obj: obj['users'][0]['user']['exec']['args'].extend(['--tenant-id', 'duplicate']),
            lambda obj: obj['users'][0]['user']['exec']['args'].extend(['--token-cache-dir', '/foreign']),
            lambda obj: obj['contexts'].append(copy.deepcopy(obj['contexts'][0]))]
        for index, mutation in enumerate(mutations):
            self.kube = copy.deepcopy(original)
            mutation(self.kube)
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.stage()
            self.assertFalse(self.output.exists())

    def test_arbitrary_source_exec_is_never_executed_even_when_rejected(self):
        marker = self.root / 'executed'
        executable = self.root / 'source-exec'
        executable.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 1\n')
        executable.chmod(0o700)
        self.kube['users'][0]['user']['exec']['command'] = str(executable)
        with self.assertRaises(importer.ImportError):
            self.stage()
        self.assertFalse(marker.exists())

    def test_missing_or_ambiguous_environment_sentence_and_stale_digests_refuse(self):
        for replacement in ('missing', importer.OLD_ENVIRONMENT * 2,
                            importer.OLD_ENVIRONMENT + importer.APPROVED_ENVIRONMENT):
            fixture.fixture(self.inputs)
            self.change_bundle(lambda text: text.replace(importer.OLD_ENVIRONMENT, replacement))
            with self.assertRaisesRegex(importer.ImportError, 'exactly one'):
                self.stage()
            self.assertFalse(self.output.exists())
        fixture.fixture(self.inputs)
        (self.inputs / 'policies.json').write_text('{}')
        with self.assertRaisesRegex(importer.ImportError, 'digest mismatch'):
            self.stage()

    def test_cli_stages_offline_without_printing_private_scope(self):
        self.source.write_text(json.dumps(self.kube))
        command = ['python3', str(Path(__file__).with_name('add-cluster.py')),
                   '--directory', str(self.inputs), '--kubeconfig', str(self.source),
                   '--output', str(self.output), '--name', 'k8s-dh-sales-uksouth',
                   '--subscription', '55555555-5555-5555-5555-555555555555',
                   '--account', 'sales@work.example.test']
        result = subprocess.run(command, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b'')
        self.assertEqual(result.stdout, b'New private work inputs staged. No live credentials, Secrets, defaults or pods changed.\n')
        result = subprocess.run(command, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(b'sales@work.example.test', result.stderr)
        self.assertNotIn(b'55555555', result.stderr)

    def test_loader_runs_only_local_config_view_with_clean_environment_and_timeout(self):
        with patch.object(importer.auth.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'{}')) as run:
            self.assertEqual(importer.auth.load_kube(self.source), {})
        args, options = run.call_args
        self.assertEqual(args[0][1:], ['--kubeconfig', str(self.source), 'config', 'view', '--raw', '-o', 'json'])
        self.assertEqual(options['env'], {'PATH': '/usr/bin:/bin'})
        self.assertEqual(options['timeout'], 30)


if __name__ == '__main__':
    unittest.main()
