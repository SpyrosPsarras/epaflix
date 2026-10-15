#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('check-github-scope.py')


class GithubScopeTest(unittest.TestCase):
    def test_backend_accepts_only_a_real_404_and_never_discloses_private_target_or_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            gh = Path(temporary) / 'gh'
            gh.write_text('#!/usr/bin/env python3\nimport os,sys\nassert sys.argv[1:5] == ["api", "--method", "HEAD", "--include"]\nstatus=os.environ["FIXTURE_STATUS"]\nprint("HTTP/2.0 "+status+" synthetic")\nprint("private-fixture-diagnostic",file=sys.stderr)\nsys.exit(0 if status=="200" else 1)\n')
            gh.chmod(0o700)
            for status in ('404', '200', '403', '500'):
                result = subprocess.run([sys.executable, str(SCRIPT), '--backend'], input='fixture/private\n',
                    text=True, capture_output=True, env={**os.environ, 'PATH': temporary + ':' + os.environ['PATH'], 'FIXTURE_STATUS': status})
                with self.subTest(status=status):
                    self.assertEqual(result.returncode, 0 if status == '404' else 1)
                    self.assertNotIn('fixture/private', result.stdout + result.stderr)
                    self.assertNotIn('private-fixture-diagnostic', result.stdout + result.stderr)

    def test_owner_discovery_is_refused_inside_a_pod(self):
        result = subprocess.run([sys.executable, str(SCRIPT)], text=True, capture_output=True,
                                env={**os.environ, 'KUBERNETES_SERVICE_HOST': 'fixture'})
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
