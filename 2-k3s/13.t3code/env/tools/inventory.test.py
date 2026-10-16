#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class InventoryTests(unittest.TestCase):
    def test_missing_tool_reports_the_failed_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / 'dirname').symlink_to('/usr/bin/dirname')
            result = subprocess.run(['/bin/bash', str(ROOT / 'env/tools/inventory.sh')],
                env={**os.environ, 'PATH': temporary}, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertRegex(result.stderr, r'inventory:.*failed.*command -v')

    def test_explicit_host_key_rejection_reports_its_location(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = root / 'environment.sh'
            environment.write_text('command() { return 0; }\n')
            sshd = root / 'sshd'
            sshd.touch(mode=0o755)
            (root / 'ssh_host_test').touch()
            script = root / 'env/tools/inventory.sh'
            script.parent.mkdir(parents=True)
            script.write_text((ROOT / 'env/tools/inventory.sh').read_text()
                .replace('/usr/sbin/sshd', str(sshd))
                .replace('/etc/ssh/ssh_host_*', str(root / 'ssh_host_*')))
            result = subprocess.run(['/bin/bash', str(script)],
                env={**os.environ, 'BASH_ENV': str(environment)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertRegex(result.stderr, r'inventory:.*line \d+.*failed.*exit 1')


if __name__ == '__main__':
    unittest.main()
