#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys
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
    def test_t3_image_installs_a_direct_executable_for_bundled_and_release_layouts(self):
        self.check_t3_image_layouts()

    @unittest.skipUnless(os.geteuid() == 0, 'Non-owner executable check needs root; permission bits are always checked')
    def test_t3_image_executable_runs_as_uid_1000(self):
        self.check_t3_image_layouts(non_owner=True)

    def check_t3_image_layouts(self, non_owner=False):
        dockerfile = (ROOT / 'env/Dockerfile').read_text().replace('\\\n', ' ')
        instruction = next(line[4:] for line in dockerfile.splitlines()
                           if line.startswith('RUN ') and 'ln -sf' in line)
        for layout in ('bundled', 'release', 'download-failed'):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as temporary:
                image = Path(temporary)
                image.chmod(0o755)
                tools = image / 'tools'
                cli = tools / 'node_modules/.bin/t3'
                cli.parent.mkdir(parents=True)
                (tools / 'package.json').write_text(json.dumps({'dependencies': {'t3': '^0.1.1-test'}}))
                package = tools / 'node_modules/t3/package.json'
                package.parent.mkdir()
                package.write_text(json.dumps({'version': '0.1.1-test'}))
                bundled = tools / 'node_modules/@t3code/t3-linux-x64/t3'
                release = image / 'opt/t3/runtime/versions/0.1.1-test/t3'
                native = bundled if layout == 'bundled' else release
                if layout == 'bundled':
                    bundled.parent.mkdir(parents=True)
                    bundled.write_text('#!/bin/sh\necho 0.1.1-test\n')
                    bundled.chmod(0o755)
                cli.write_text('''#!/bin/sh
set -eu
test "$1" = --version
if [ "$LAYOUT" = download-failed ]; then exit 23; fi
mkdir -p "$T3CODE_HOME/runtime/versions/0.1.1-test"
printf '#!/bin/sh\\necho 0.1.1-test\\n' > "$T3CODE_HOME/runtime/versions/0.1.1-test/t3"
chmod 700 "$T3CODE_HOME/runtime/versions/0.1.1-test" "$T3CODE_HOME/runtime/versions/0.1.1-test/t3"
''')
                cli.chmod(0o755)
                command = instruction.replace('/tools', str(tools)).replace('/opt/t3', str(image / 'opt/t3'))
                result = subprocess.run(['/bin/sh', '-ec', command], capture_output=True, text=True,
                    env={**os.environ, 'LAYOUT': layout})
                if layout == 'download-failed':
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(cli.is_symlink())
                    continue
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(cli.is_symlink())
                self.assertEqual(cli.resolve(), native)
                directory = native.parent
                while directory != image:
                    self.assertEqual(directory.stat().st_mode & 0o005, 0o005, str(directory))
                    directory = directory.parent
                self.assertEqual(native.stat().st_mode & 0o005, 0o005, str(native))
                runtime_user = {'user': 1000, 'group': 1000, 'extra_groups': []} if non_owner else {}
                self.assertEqual(subprocess.check_output([str(cli), '--version'], text=True,
                                                        **runtime_user).strip(), '0.1.1-test')

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

    @unittest.skipIf(os.geteuid() == 0, 'Smoke staging permission check needs an unprivileged user')
    def test_isolated_smoke_stages_each_variants_guide_without_overwriting_readonly_files(self):
        smoke = (ROOT / 'env/tools/isolated-smoke.sh').read_text()
        staging = smoke.split("python3 - <<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        homelab = ROOT.parent / '13.t3code-homelab/files'
        work = ROOT.parent / '13.t3code-work/files'
        for variant, guide in (('homelab', homelab / 'homelab-ssh.md'),
                               ('work', ROOT / 'env/files/isolated-ssh.md')):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                scripts = Path(temporary)
                code = staging
                for source, destination in (('/src', ROOT), ('/scripts', scripts),
                                            ('/homelab-src/files', homelab),
                                            ('/work-src/files', work),
                                            ('/scripts/work-aks-auth.py', scripts / 'work-aks-auth.py')):
                    code = code.replace(repr(source), repr(str(destination)))
                result = subprocess.run([sys.executable, '-c', code],
                    env={**os.environ, 'MCP_HUB_INSTANCE': variant}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((scripts / 'homelab-ssh.md').read_bytes(), guide.read_bytes())
                self.assertEqual((scripts / 'ssh-agent-ready.sh').read_bytes(),
                                 (ROOT / 'env/files/ssh-agent-ready.sh').read_bytes())
                for script in scripts.iterdir():
                    self.assertEqual(script.stat().st_mode & 0o777, 0o555, script.name)
                if variant == 'homelab':
                    self.assertEqual((scripts / 'ssh-config.sh').read_bytes(),
                                     (homelab / 'ssh-config.sh').read_bytes())
                    self.assertTrue((scripts / 'ssh-agent-ready.sh').is_file())
                    self.assertFalse((scripts / 'work-aks-auth.py').exists())
                else:
                    for name in ('work-ssh.md', 'work-ssh.py'):
                        self.assertEqual((scripts / name).read_bytes(), (work / name).read_bytes())
                    self.assertEqual((scripts / 'work-aks-auth.py').read_bytes(),
                                     (ROOT / 'env/files/work-aks-auth.py').read_bytes())
                    self.assertFalse((scripts / 'ssh-config.sh').exists())
                    self.assertFalse((scripts / 'homelab-ssh.conf').exists())


if __name__ == '__main__':
    unittest.main()
