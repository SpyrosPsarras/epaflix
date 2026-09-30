import json
import os
from pathlib import Path
import sys
import tempfile


def update(path, homes):
    path = Path(path)
    config = json.loads(path.read_text()) if path.exists() else {'version': 1}
    secret = config.setdefault('secret_protection', {})
    # The basename is also a vault attachment identifier, not necessarily a file.
    secret.setdefault('overrides', {})['secret.basename.id-ed25519'] = 'off'
    secret['overrides']['secret.pattern.ssh-key-basename'] = 'off'
    denied = secret.setdefault('deny_paths', [])
    for directory in [*(str(Path(home) / '.ssh') for home in homes), '/run/t3-github-ssh']:
        if directory not in denied:
            denied.append(directory)
    content = json.dumps(config, indent=2) + '\n'
    if path.exists() and path.read_text() == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as file:
        file.write(content)
    os.chmod(file.name, 0o600)
    os.replace(file.name, path)


if __name__ == '__main__':
    update(sys.argv[1], sys.argv[2:])
