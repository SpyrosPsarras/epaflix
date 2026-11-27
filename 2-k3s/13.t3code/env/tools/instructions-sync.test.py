#!/usr/bin/env python3
import fcntl
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

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
            if path.is_file():
                path.unlink()
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

    def test_missing_file_falls_back(self):
        self.publish(None)
        self.assertEqual(self.sync(), 'fallback')
        self.assertFalse(self.target.exists())

    def test_directory_named_agents_falls_back(self):
        (self.work / 'AGENTS.md').mkdir()
        (self.work / 'AGENTS.md/x').write_text('listing\n')
        self.publish(None)
        self.assertEqual(self.sync(), 'fallback')
        self.assertFalse(self.target.exists())

    def test_unreachable_without_good_copy_falls_back(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text('previous\n')
        self.assertEqual(self.sync(UNREACHABLE), 'fallback')
        self.assertEqual(self.target.read_text(), 'previous\n')

    def test_tag_named_main_ignored(self):
        self.publish(b'# Branch\n')
        (self.work / 'AGENTS.md').write_bytes(b'# Tag\n')
        git('commit', '-q', '-am', 'tag', cwd=self.work)
        git('tag', 'main', cwd=self.work)
        git('push', '-q', str(self.remote), 'refs/tags/main', cwd=self.work)
        self.sync()
        self.assertTrue(self.target.read_text().startswith('# Branch\n'))

    def test_restart_offline_restores_last_good(self):
        self.publish(b'# Rules\n')
        self.sync()
        good = self.target.read_bytes()
        self.target.write_text('bundle\n')
        self.assertEqual(self.sync(UNREACHABLE), 'kept')
        self.assertEqual(self.target.read_bytes(), good)

    def test_overlapping_invalid_fetch_never_becomes_good(self):
        self.publish(b'# Rules\n')
        bad = self.remote.parent / 'bad.git'
        git('clone', '-q', '--bare', str(self.remote), str(bad))
        bad_work = self.remote.parent / 'bad-work'
        git('clone', '-q', str(bad), str(bad_work))
        (bad_work / 'AGENTS.md').write_text('  \n')
        git('commit', '-q', '-am', 'blank', cwd=bad_work)
        git('push', '-q', 'origin', 'main', cwd=bad_work)
        cache = self.home / '.local/share/t3-instructions'
        original = sync_module.read
        calls = []

        def overlapping(cache_dir, ref):
            result = original(cache_dir, ref)
            if not calls:
                calls.append(ref)
                sync_module.git(cache, 'fetch', '-q', bad.as_uri(), '+refs/heads/main:refs/remotes/origin/main')
            return result

        with patch.object(sync_module, 'read', overlapping):
            self.sync()
        self.assertEqual(sync_module.read(cache, 'refs/heads/good'), '# Rules\n')
        self.target.write_text('bundle\n')
        self.assertEqual(self.sync(UNREACHABLE), 'kept')
        self.assertTrue(self.target.read_text().startswith('# Rules\n'))

    def test_restart_after_invalid_push_restores_last_good(self):
        self.publish(b'# Rules\n')
        self.sync()
        good = self.target.read_bytes()
        self.publish(b'  \n')
        self.target.write_text('bundle\n')
        self.assertEqual(self.sync(), 'kept')
        self.assertEqual(self.target.read_bytes(), good)

    def run_once(self):
        env = {'HOME': str(self.home), 'PATH': os.environ['PATH'], 'MCP_HUB_INSTANCE': 'homelab'}
        return subprocess.run(['python3', str(SOURCE), 'once'], env=env, capture_output=True, text=True, timeout=60)

    def test_cli_once_quiet_on_failure(self):
        (self.home / '.config/t3-runtime').mkdir(parents=True)
        (self.home / '.config/t3-runtime/environment').write_text('homelab\n')
        (self.home / '.gitconfig').write_text(
            f'[url "{UNREACHABLE.removesuffix("x.git")}"]\n\tinsteadOf = https://git.epaflix.com/spypsarras/\n')
        result = self.run_once()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'instructions: fetch failed or file invalid; bundle instructions in use\n')
        self.assertEqual(result.stderr, '')

    def test_runs_are_serialized(self):
        (self.home / '.config/t3-runtime').mkdir(parents=True)
        (self.home / '.config/t3-runtime/environment').write_text('homelab\n')
        (self.home / '.gitconfig').write_text(
            f'[url "{UNREACHABLE.removesuffix("x.git")}"]\n\tinsteadOf = https://git.epaflix.com/spypsarras/\n')
        cache = self.home / '.local/share/t3-instructions'
        cache.mkdir(parents=True)
        env = {'HOME': str(self.home), 'PATH': os.environ['PATH'], 'MCP_HUB_INSTANCE': 'homelab'}
        with open(cache / 'lock', 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            run = subprocess.Popen(['python3', str(SOURCE), 'once'], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            with self.assertRaises(subprocess.TimeoutExpired):
                run.wait(timeout=2)
        out, _ = run.communicate(timeout=60)
        self.assertEqual(run.returncode, 0)
        self.assertEqual(out, b'instructions: fetch failed or file invalid; bundle instructions in use\n')

    def test_cli_unusable_cache_keeps_file(self):
        (self.home / '.config/t3-runtime').mkdir(parents=True)
        (self.home / '.config/t3-runtime/environment').write_text('homelab\n')
        (self.home / '.local/share').mkdir(parents=True)
        (self.home / '.local/share/t3-instructions').write_text('not a directory\n')
        self.target.parent.mkdir(parents=True)
        self.target.write_text('previous\n')
        result = self.run_once()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'instructions: cache unavailable; current file kept\n')
        self.assertEqual(result.stderr, '')
        self.assertEqual(self.target.read_text(), 'previous\n')

    def test_cli_waits_for_prepared_home(self):
        result = self.run_once()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'instructions: home not prepared yet\n')
        self.assertEqual(list(self.home.iterdir()), [])

    def test_repo_per_environment(self):
        self.assertEqual(sync_module.REPOS, {
            'homelab': 'https://git.epaflix.com/spypsarras/skills.git',
            'work': 'https://git.epaflix.com/spypsarras/skills-work.git'})


if __name__ == '__main__':
    unittest.main()
