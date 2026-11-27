#!/usr/bin/env python3
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'files/instructions-sync.py'
spec = importlib.util.spec_from_file_location('instructions_sync', SOURCE)
sync_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync_module)

UNREACHABLE = 'http://127.0.0.1:9/x.git'


def git(*args, cwd=None):
    subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', *args],
                   cwd=cwd, check=True, capture_output=True)


class InstructionsSyncTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.home = root / 'home'
        self.home.mkdir()
        self.remote = root / 'remote.git'
        self.work = root / 'work'
        git('init', '-q', '--bare', '-b', 'main', str(self.remote))
        git('init', '-q', '-b', 'main', str(self.work))
        self.guidance = root / 'guidance.md'
        self.guidance.write_text('# Runtime facts\n')
        self.ssh = root / 'ssh.md'
        self.ssh.write_text('# SSH\n')
        self.target = self.home / '.pi/agent/AGENTS.md'

    def publish(self, content):
        path = self.work / 'AGENTS.md'
        if content is None:
            path.unlink(missing_ok=True)
            (self.work / 'skill').mkdir(exist_ok=True)
            (self.work / 'skill/SKILL.md').write_text('skill\n')
        else:
            path.write_bytes(content)
        git('add', '-A', cwd=self.work)
        git('commit', '-q', '--allow-empty', '-m', 'change', cwd=self.work)
        git('push', '-q', '-f', str(self.remote), 'main', cwd=self.work)

    def sync(self, repo=None):
        return sync_module.sync(self.home, repo or self.remote.as_uri(), self.ssh, self.guidance)

    def test_valid_file_written(self):
        self.publish(b'# Rules\n')
        self.assertRegex(self.sync(), r'^updated [0-9a-f]{7,}$')
        self.assertEqual(self.target.read_text(), '# Rules\n' + '\n' + '# Runtime facts\n' + '\n' + '# SSH\n')
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_new_push_replaces_file(self):
        self.publish(b'# Rules\n')
        self.sync()
        self.publish(b'# Rules v2\n')
        self.assertRegex(self.sync(), r'^updated [0-9a-f]{7,}$')
        self.assertTrue(self.target.read_text().startswith('# Rules v2\n'))

    def test_unchanged_not_rewritten(self):
        self.publish(b'# Rules\n')
        self.sync()
        before = self.target.stat().st_mtime_ns
        self.assertEqual(self.sync(), 'unchanged')
        self.assertEqual(self.target.stat().st_mtime_ns, before)

    def test_invalid_kept(self):
        self.publish(b'# Rules\n')
        self.sync()
        before = self.target.read_bytes()
        for content in (b' \n\t\n', b'\xff\xfe rules', b'a' * 65537):
            with self.subTest(size=len(content)):
                self.publish(content)
                self.assertEqual(self.sync(), 'kept')
                self.assertEqual(self.target.read_bytes(), before)

    def test_limit_is_inclusive(self):
        self.publish(b'a' * 65536)
        self.assertRegex(self.sync(), r'^updated [0-9a-f]{7,}$')

    def test_missing_file_kept(self):
        self.publish(None)
        self.assertEqual(self.sync(), 'kept')
        self.assertFalse(self.target.exists())

    def test_unreachable_kept(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text('previous\n')
        self.assertEqual(self.sync(UNREACHABLE), 'kept')
        self.assertEqual(self.target.read_text(), 'previous\n')

    def test_cli_once_quiet_on_failure(self):
        (self.home / '.gitconfig').write_text(
            f'[url "{UNREACHABLE.removesuffix("x.git")}"]\n\tinsteadOf = https://git.epaflix.com/spypsarras/\n')
        env = {'HOME': str(self.home), 'PATH': os.environ['PATH'], 'MCP_HUB_INSTANCE': 'homelab'}
        result = subprocess.run(['python3', str(SOURCE), 'once'], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'instructions: fetch failed or file invalid; current file kept\n')
        self.assertEqual(result.stderr, '')

    def test_repo_per_environment(self):
        self.assertEqual(sync_module.REPOS, {
            'homelab': 'https://git.epaflix.com/spypsarras/skills.git',
            'work': 'https://git.epaflix.com/spypsarras/skills-work.git'})


if __name__ == '__main__':
    unittest.main()
