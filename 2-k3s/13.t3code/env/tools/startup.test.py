#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


class StartupTest(unittest.TestCase):
    def test_init_creates_owned_userdata_before_kubelet_mounts_runtime_file(self):
        for variant in ('homelab', 'work'):
            with self.subTest(variant=variant):
                overlay = Path(__file__).resolve().parents[3] / f'13.t3code-{variant}'
                docs = list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(overlay)])))
                sts = next(doc for doc in docs if doc and doc['kind'] == 'StatefulSet')
                init = next(container for container in sts['spec']['template']['spec']['initContainers'] if container['name'] == 'runtime-state')
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    home, state = root / 'home', root / 'runtime-state'
                    home.mkdir()
                    state.mkdir()
                    command = [arg.replace('/home/t3', str(home)).replace('/runtime-state', str(state)) for arg in init['command']]
                    subprocess.run(command, check=True)
                    directory = home / '.t3/userdata'
                    self.assertTrue(directory.is_dir(), 'Init must create userdata before the main-container file mount')
                    self.assertEqual(directory.stat().st_uid, os.getuid())
                    self.assertEqual((state / 'server-runtime.json').read_bytes(), b'')
                    mounts = {mount['mountPath']: mount['name'] for mount in init['volumeMounts']}
                    self.assertEqual(mounts.get('/home/t3'), 'home')
                    self.assertEqual(mounts.get('/runtime-state'), 'runtime-state')


if __name__ == '__main__':
    unittest.main()
