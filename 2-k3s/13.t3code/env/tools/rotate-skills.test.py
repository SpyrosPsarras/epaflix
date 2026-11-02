#!/usr/bin/env python3
import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).with_name('rotate-skills.py')
SPEC = importlib.util.spec_from_file_location('rotate_skills', SCRIPT)


def json_bytes(value):
    return json.dumps(value, sort_keys=True).encode()


def encoded(value):
    return base64.b64encode(value).decode()


def entry(value, executable=False):
    return {'data': encoded(value.encode()), 'executable': executable}


def fixture(environment, sort_keys=True):
    bundle = {'sourceHome': '/synthetic', 'files': {
        'instructions.md': entry('Synthetic instructions'),
        'skills/keep/SKILL.md': entry('Keep this skill'),
        'skills/replace/SKILL.md': entry('Old skill'),
        'skills/replace/old.txt': entry('Remove stale files'),
        'skills/remove/SKILL.md': entry('Remove this skill')}}
    inputs = {'bundle.json': json_bytes(bundle), 'policies.json': b'synthetic policies',
              'services.json': b'synthetic services', 'kubeconfig': b'synthetic kubeconfig',
              'authorized_keys': b'synthetic login keys'}
    if environment == 'work':
        inputs['work-auth.json'] = b'synthetic work authentication'
    runtime = {'environment': environment, 'approvals': {'policies': 'synthetic-review'},
               'grantSha256': {'model-token': 'synthetic-grant-digest'},
               'sha256': {name: hashlib.sha256(value).hexdigest() for name, value in inputs.items()}}
    inputs['runtime.json'] = json.dumps(runtime, sort_keys=sort_keys).encode()
    return {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
            'metadata': {'name': 't3-runtime-inputs', 'namespace': 't3code-' + environment,
                         'resourceVersion': '42', 'annotations': {'synthetic': 'preserve'}},
            'data': {name: encoded(value) for name, value in inputs.items()}}


class FakeKubectl:
    def __init__(self, environment='homelab', sort_keys=True):
        self.resource = fixture(environment, sort_keys)
        self.calls = []
        self.patches = []
        self.fail = False
        self.conflict = False
        self.violations = []

    def __call__(self, argv, *, input=None, capture_output=False, timeout=None):
        self.calls.append(argv)
        expected = ['kubectl', '--context', 'synthetic-context', '-n', self.resource['metadata']['namespace']]
        if argv[:5] != expected or not capture_output or not timeout:
            self.violations.append('Missing explicit scope or captured output')
            raise AssertionError('Missing explicit scope or captured output')
        if self.fail:
            return subprocess.CompletedProcess(argv, 1, b'synthetic-private-output', b'synthetic-private-error')
        args = argv[5:]
        if args == ['get', 'secret', 't3-runtime-inputs', '-o', 'json']:
            return subprocess.CompletedProcess(argv, 0, json_bytes(self.resource), b'private warning')
        if args == ['patch', 'secret', 't3-runtime-inputs', '--type', 'json', '--patch-file', '/dev/stdin']:
            operations = json.loads(input)
            self.patches.append(operations)
            if self.conflict or operations[0] != {
                    'op': 'test', 'path': '/metadata/resourceVersion', 'value': self.resource['metadata']['resourceVersion']}:
                return subprocess.CompletedProcess(argv, 1, b'synthetic-private-output', b'synthetic-private-error')
            for operation in operations[1:]:
                if operation['op'] != 'replace' or operation['path'] not in {'/data/bundle.json', '/data/runtime.json'}:
                    self.violations.append('Unexpected patch target')
                    raise AssertionError('Unexpected patch target')
                self.resource['data'][operation['path'].removeprefix('/data/')] = operation['value']
            return subprocess.CompletedProcess(argv, 0, b'synthetic-private-output', b'synthetic-private-error')
        self.violations.append('Unexpected operation')
        raise AssertionError('Unexpected operation')


class RotateSkillsTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.exists(), 'The skills-only rotation helper is missing')
        self.module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(self.module)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.skills = self.root / 'skills'
        self.skills.mkdir()
        self.stage('replace', 'New skill')
        self.stage('add', 'Added skill')
        executable = self.skills / 'replace/run.sh'
        executable.write_text('true\n')
        executable.chmod(0o744)
        group_executable = self.skills / 'replace/group.txt'
        group_executable.write_text('Not owner executable')
        group_executable.chmod(0o650)
        self.backup = self.root / 'backup.json'
        self.cluster = FakeKubectl()

    def tearDown(self):
        if hasattr(self, 'cluster'):
            self.assertEqual(self.cluster.violations, [])

    def stage(self, name, content):
        directory = self.skills / name
        directory.mkdir()
        (directory / 'SKILL.md').write_text(content)

    def invoke(self, *extra, environment='homelab', arguments=None):
        args = ['--context', 'synthetic-context', '--environment', environment, '--skills', str(self.skills)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', [str(SCRIPT), *(args if arguments is None else arguments), *extra]), \
                patch.object(self.module.subprocess, 'run', side_effect=self.cluster), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                self.module.main()
            except SystemExit as error:
                code = error.code
                if isinstance(code, str):
                    stderr.write(code)
                    code = 1
            else:
                code = 0
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_refused(self, *extra, **kwargs):
        before = copy.deepcopy(self.cluster.resource)
        code, stdout, stderr = self.invoke(*extra, **kwargs)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, '')
        self.assertEqual(stderr, 'Skills rotation failed. No captured diagnostics or secret values were printed.')
        self.assertEqual(self.cluster.resource, before)
        self.assertEqual(self.cluster.patches, [])

    def test_replace_add_remove_preserves_other_inputs_and_any_executable_bit(self):
        for environment, sort_keys in (('homelab', True), ('work', False)):
            with self.subTest(environment=environment):
                self.cluster = FakeKubectl(environment, sort_keys)
                before = copy.deepcopy(self.cluster.resource)
                backup = self.root / (environment + '.json')
                code, stdout, stderr = self.invoke('--remove', 'remove', '--backup', str(backup), environment=environment)
                self.assertEqual((code, stderr), (0, ''))
                self.assertEqual(stdout, 'Added skills: add\nReplaced skills: replace\nRemoved skills: remove\nUnchanged skills: 1\nSkills rotation applied. Restart required.\n')
                self.assertEqual(json.loads(backup.read_bytes()), before)
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                after = self.cluster.resource
                self.assertEqual({key: value for key, value in after.items() if key != 'data'},
                                 {key: value for key, value in before.items() if key != 'data'})
                self.assertEqual(set(after['data']), set(before['data']))
                for key in before['data'].keys() - {'bundle.json', 'runtime.json'}:
                    self.assertEqual(after['data'][key], before['data'][key])
                old_bundle = json.loads(base64.b64decode(before['data']['bundle.json']))
                bundle_raw = base64.b64decode(after['data']['bundle.json'])
                bundle = json.loads(bundle_raw)
                self.assertEqual(bundle['sourceHome'], '/synthetic')
                self.assertEqual(bundle['files']['instructions.md'], old_bundle['files']['instructions.md'])
                self.assertEqual(bundle['files']['skills/keep/SKILL.md'], old_bundle['files']['skills/keep/SKILL.md'])
                self.assertEqual(set(bundle['files']), {'instructions.md', 'skills/keep/SKILL.md',
                    'skills/add/SKILL.md', 'skills/replace/SKILL.md', 'skills/replace/run.sh', 'skills/replace/group.txt'})
                self.assertEqual(bundle['files']['skills/replace/SKILL.md'], entry('New skill'))
                self.assertEqual(bundle['files']['skills/add/SKILL.md'], entry('Added skill'))
                self.assertEqual(bundle['files']['skills/replace/run.sh'], entry('true\n', True))
                self.assertEqual(bundle['files']['skills/replace/group.txt'], entry('Not owner executable', True))
                runtime = json.loads(base64.b64decode(after['data']['runtime.json']))
                self.assertEqual(runtime['sha256']['bundle.json'], hashlib.sha256(bundle_raw).hexdigest())
                runtime['sha256']['bundle.json'] = json.loads(base64.b64decode(before['data']['runtime.json']))['sha256']['bundle.json']
                self.assertEqual(json.dumps(runtime, sort_keys=sort_keys).encode(),
                                 base64.b64decode(before['data']['runtime.json']))
                self.assertEqual(self.cluster.patches[0][0], {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '42'})
                self.assertEqual([operation['path'] for operation in self.cluster.patches[0][1:]],
                                 ['/data/bundle.json', '/data/runtime.json'])

    def test_check_does_not_patch_or_write_backup(self):
        before = copy.deepcopy(self.cluster.resource)
        code, stdout, stderr = self.invoke('--remove', 'remove', '--backup', str(self.backup), '--check')
        self.assertEqual((code, stderr), (0, ''))
        self.assertEqual(stdout, 'Added skills: add\nReplaced skills: replace\nRemoved skills: remove\nUnchanged skills: 1\n')
        self.assertEqual(self.cluster.resource, before)
        self.assertEqual(len(self.cluster.calls), 1)
        self.assertFalse(self.backup.exists())

    def test_missing_backup_is_refused(self):
        self.assert_refused()

    def test_existing_backup_is_not_overwritten(self):
        self.backup.write_text('Keep existing backup')
        self.assert_refused('--backup', str(self.backup))
        self.assertEqual(self.backup.read_text(), 'Keep existing backup')

    def test_backup_in_repository_is_refused(self):
        repository = self.root / 'repository'
        repository.mkdir()
        (repository / '.git').write_text('synthetic git worktree marker')
        backup = repository / 'backup.json'
        self.assert_refused('--backup', str(backup))
        self.assertFalse(backup.exists())

    def test_backup_symlink_is_not_followed(self):
        self.backup.symlink_to(self.root / 'missing')
        self.assert_refused('--backup', str(self.backup))
        self.assertFalse((self.root / 'missing').exists())

    def test_unknown_remove_is_refused(self):
        self.assert_refused('--remove', 'unknown', '--check')

    def test_unsafe_remove_names_are_refused(self):
        for name in ('..', '.', '.hidden', '../keep', 'keep/other', '/keep', 'keep\\other', 'keep\nother'):
            with self.subTest(name=name):
                self.assert_refused('--remove', name, '--check')

    def test_unsafe_staged_names_are_refused(self):
        for name in ('.hidden', 'bad\\name', 'bad\nname'):
            with self.subTest(name=name):
                self.stage(name, 'Unsafe name')
                self.assert_refused('--check')
                (self.skills / name / 'SKILL.md').unlink()
                (self.skills / name).rmdir()

    def test_removing_all_skills_is_refused_by_bundle_validation(self):
        empty = self.root / 'empty'
        empty.mkdir()
        self.skills = empty
        self.assert_refused('--remove', 'keep', 'replace', 'remove', '--check')

    def test_staged_skill_requires_skill_md(self):
        (self.skills / 'add/SKILL.md').unlink()
        self.assert_refused('--check')

    def test_symlinks_in_staging_are_refused(self):
        for name, target in [('linked', self.skills / 'add'), ('replace/link', self.skills / 'add'),
                             ('replace/link.md', self.skills / 'add/SKILL.md')]:
            with self.subTest(name=name):
                link = self.skills / name
                link.symlink_to(target)
                self.assert_refused('--check')
                link.unlink()
        link = self.root / 'linked-skills'
        link.symlink_to(self.skills)
        original = self.skills
        self.skills = link
        self.assert_refused('--check')
        self.skills = original

    @unittest.skipIf(os.geteuid() == 0, 'Root bypasses directory permissions')
    def test_unreadable_staged_directory_is_refused(self):
        directory = self.skills / 'replace/private'
        directory.mkdir()
        (directory / 'keep.txt').write_text('Do not silently omit this file')
        directory.chmod(0)
        try:
            self.assert_refused('--check')
        finally:
            directory.chmod(0o700)

    def test_staged_and_removed_skill_overlap_is_refused(self):
        self.assert_refused('--remove', 'replace', '--check')

    def test_existing_bundle_rules_are_enforced(self):
        for mutate in (lambda bundle: bundle.pop('sourceHome'),
                       lambda bundle: bundle['files'].update({'skills/../escape': entry('unsafe')}),
                       lambda bundle: bundle['files']['instructions.md'].update({'data': 'not base64'})):
            with self.subTest(mutate=mutate):
                self.cluster = FakeKubectl()
                bundle = json.loads(base64.b64decode(self.cluster.resource['data']['bundle.json']))
                mutate(bundle)
                self.replace_input('bundle.json', json_bytes(bundle))
                self.assert_refused('--check')

    def replace_input(self, name, raw):
        data = self.cluster.resource['data']
        data[name] = encoded(raw)
        runtime = json.loads(base64.b64decode(data['runtime.json']))
        runtime['sha256'][name] = hashlib.sha256(raw).hexdigest()
        data['runtime.json'] = encoded(json_bytes(runtime))

    def test_empty_staging_without_removals_is_refused(self):
        self.skills = self.root / 'empty'
        self.skills.mkdir()
        self.assert_refused('--backup', str(self.backup))
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.cluster.calls, [])

    def test_remove_without_staged_skills_is_allowed(self):
        self.skills = self.root / 'empty'
        self.skills.mkdir()
        code, stdout, stderr = self.invoke('--remove', 'remove', '--backup', str(self.backup))
        self.assertEqual((code, stderr), (0, ''))
        bundle = json.loads(base64.b64decode(self.cluster.resource['data']['bundle.json']))
        self.assertNotIn('skills/remove/SKILL.md', bundle['files'])
        self.assertIn('skills/keep/SKILL.md', bundle['files'])
        self.assertIn('Removed skills: remove\n', stdout)

    def test_plain_file_at_skill_root_is_refused(self):
        for name in ('add', 'replace', 'remove'):
            with self.subTest(name=name):
                self.cluster = FakeKubectl()
                bundle = json.loads(base64.b64decode(self.cluster.resource['data']['bundle.json']))
                bundle['files']['skills/' + name] = entry('Conflicting plain file')
                self.replace_input('bundle.json', json_bytes(bundle))
                extra = ('--remove', name) if name == 'remove' else ()
                self.assert_refused(*extra, '--check')

    def test_hidden_directories_are_skipped_but_hidden_files_are_packed(self):
        hidden = self.skills / 'replace/.cache'
        hidden.mkdir()
        (hidden / 'ignored.txt').write_text('Not a skill input')
        (self.skills / 'replace/.metadata').write_text('Included hidden file')
        code, stdout, stderr = self.invoke('--backup', str(self.backup))
        self.assertEqual((code, stderr), (0, ''))
        bundle = json.loads(base64.b64decode(self.cluster.resource['data']['bundle.json']))
        self.assertNotIn('skills/replace/.cache/ignored.txt', bundle['files'])
        self.assertEqual(bundle['files']['skills/replace/.metadata'], entry('Included hidden file'))

    def test_noncanonical_runtime_serialization_is_refused(self):
        data = self.cluster.resource['data']
        runtime = json.loads(base64.b64decode(data['runtime.json']))
        data['runtime.json'] = encoded(json.dumps(runtime, indent=2).encode())
        self.assert_refused('--check')

    def test_existing_input_digest_mismatch_is_refused(self):
        self.cluster.resource['data']['policies.json'] = encoded(b'changed policies')
        self.assert_refused('--check')

    def test_environment_mismatch_is_refused(self):
        runtime = json.loads(base64.b64decode(self.cluster.resource['data']['runtime.json']))
        runtime['environment'] = 'work'
        self.cluster.resource['data']['runtime.json'] = encoded(json_bytes(runtime))
        self.assert_refused('--check')

    def test_explicit_context_is_required(self):
        self.assert_refused(arguments=['--environment', 'homelab', '--skills', str(self.skills), '--check'])
        self.assertEqual(self.cluster.calls, [])

    def test_kubectl_failure_does_not_print_diagnostics(self):
        self.cluster.fail = True
        self.assert_refused('--check')

    def test_resource_version_conflict_keeps_backup_and_does_not_change_secret(self):
        before = copy.deepcopy(self.cluster.resource)
        self.cluster.conflict = True
        code, stdout, stderr = self.invoke('--backup', str(self.backup))
        self.assertEqual(code, 1)
        self.assertEqual(stdout, '')
        self.assertNotIn('synthetic-private', stderr)
        self.assertEqual(self.cluster.resource, before)
        self.assertEqual(json.loads(self.backup.read_bytes()), before)


if __name__ == '__main__':
    unittest.main()
