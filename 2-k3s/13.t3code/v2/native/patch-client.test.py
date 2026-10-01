#!/usr/bin/env python3
import pathlib
import subprocess
import tempfile

script = pathlib.Path(__file__).with_name('patch-client.py')
with tempfile.TemporaryDirectory() as temp:
    root = pathlib.Path(temp)
    directory = root / 'apps/desktop/src/app'
    directory.mkdir(parents=True)
    (directory / 'DesktopPreReadyPlatform.ts').write_text('        NodeFS.writeFileSync(\n')
    (directory / 'DesktopLinuxUrlHandler.ts').write_text('    if (environment.platform !== "linux") {\n')
    subprocess.run(['python3', str(script), str(root)], check=True)
    assert all('T3CODE_V2_NO_URI_REGISTRATION' in path.read_text() for path in directory.iterdir())
    assert subprocess.run(['python3', str(script), str(root)], capture_output=True).returncode != 0
print('PASS: two URI registration paths patched; source drift rejected')
