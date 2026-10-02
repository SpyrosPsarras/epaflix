import base64
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
    for directory in [*(str(Path(home) / '.ssh') for home in homes), '/run/t3-github-ssh', '/run/t3-vaultwarden', '/run/t3-bw']:
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


def public_hosts(home):
    source = Path(home) / '.ssh/known_hosts'
    destination = Path(home) / '.config/ssh/known_hosts'
    lines = []
    try:
        # No symlink exception: never publish a private file through known_hosts.
        with open(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), 'r') as file:
            for line in file:
                fields = line.split()
                marker = []
                if fields and fields[0].startswith('@'):
                    if fields[0] not in {'@revoked', '@cert-authority'}:
                        continue
                    marker = fields[:1]
                    fields = fields[1:]
                if len(fields) < 3:
                    continue
                try:
                    key = base64.b64decode(fields[2], validate=True)
                    parts = []
                    while key:
                        if len(key) < 4:
                            raise ValueError('truncated key')
                        size = int.from_bytes(key[:4], 'big')
                        if size > len(key) - 4:
                            raise ValueError('truncated field')
                        parts.append(key[4:4 + size])
                        key = key[4 + size:]
                    # Pi-hole uses Ed25519. Reject other formats rather than
                    # publish bytes whose public-key structure we cannot validate.
                    if fields[1] != 'ssh-ed25519' or len(parts) != 2 or parts[0] != b'ssh-ed25519' or len(parts[1]) != 32:
                        continue
                except (ValueError, UnicodeError):
                    continue
                # Strip arbitrary comments. Publish only public host-key fields.
                lines.append(' '.join(marker + fields[:3]) + '\n')
    except (OSError, UnicodeError):
        lines = []
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', dir=destination.parent, delete=False) as file:
        file.writelines(lines)
    os.chmod(file.name, 0o600)
    os.replace(file.name, destination)


if __name__ == '__main__':
    update(sys.argv[1], sys.argv[2:])
    public_hosts(sys.argv[2])
