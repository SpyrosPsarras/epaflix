#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL_FIXTURE = '''
set -eu
cd() { test "$1" = /tools; }
npx() {
  case " $* " in
    *" ci "*)
      if [ "$BW_DEPENDENCY" = 1 ]; then touch "$IMAGE_ROOT/bw"; fi
      ;;
    *" uninstall "*)
      case " $* " in
        *" @bitwarden/cli "*) /bin/rm -f "$IMAGE_ROOT/bw" ;;
        *) exit 1 ;;
      esac
      ;;
    *) exit 1 ;;
  esac
}
npm() { test "$*" = 'cache clean --force'; }
install() {
  test "$*" = '-m 0555 /tmp/vault-client.py /usr/local/bin/vault'
  /usr/bin/install -m 0555 "$VAULT_SOURCE" "$IMAGE_ROOT/vault"
}
rm() { test "$*" = /tmp/vault-client.py; }
'''


class ImageCommandTests(unittest.TestCase):
    def test_rendered_vault_commands_have_runner_and_cli_in_their_image_variant(self):
        dockerfile = (ROOT / 'env/Dockerfile').read_text().replace('\\\n', ' ')
        instructions = [line[4:] for line in dockerfile.splitlines() if line.startswith('RUN ') and
                        any(word in line for word in ('npm', 'npx', '/tmp/vault-client.py'))]
        dependencies = json.loads((ROOT / 'env/tools/package.json').read_text())['dependencies']
        for variant, overlay in (('mixed', ROOT / 'one'), ('homelab', ROOT.parent / '13.t3code-homelab'),
                                 ('work', ROOT.parent / '13.t3code-work')):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                image = Path(temporary)
                result = subprocess.run(['/bin/sh', '-c', INSTALL_FIXTURE + '\n'.join(instructions)],
                    env={'PATH': '/usr/bin:/bin', 'T3_RUNTIME_VARIANT': variant, 'IMAGE_ROOT': str(image),
                         'VAULT_SOURCE': str(ROOT / 'env/files/vault-client.py'),
                         'BW_DEPENDENCY': '1' if '@bitwarden/cli' in dependencies else '0'},
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                if variant == 'mixed':
                    self.assertTrue((image / 'bw').is_file())
                    self.assertFalse((image / 'vault').exists())
                    continue
                docs = yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(overlay)]))
                pod = next(doc for doc in docs if doc and doc['kind'] == 'StatefulSet')['spec']['template']['spec']
                for container in [*pod.get('initContainers', []), *pod['containers']]:
                    if container.get('command', [None])[0] == 'vault':
                        self.assertTrue(os.access(image / 'vault', os.X_OK),
                                        f'{variant} image lacks vault required by {container["name"]}')
                        self.assertTrue((image / 'bw').is_file(),
                                        f'{variant} image lacks bw required by vault serve')


if __name__ == '__main__':
    unittest.main()
