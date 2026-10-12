#!/usr/bin/env python3
"""Prepare a dedicated laptop SSH identity and pinned backend configuration, offline."""
import argparse
import base64
import os
from pathlib import Path
import tempfile
import re
import subprocess


def write_private(path, content):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.t3-laptop-')
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare(directory, host, user, port, host_key, environment='homelab'):
    if environment not in {'homelab', 'work'}:
        raise ValueError('Only dedicated homelab and work configurations are supported')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', host) or not re.fullmatch(r'[a-z_][a-z0-9_-]*', user) or not 1 <= port <= 65535:
        raise ValueError('Invalid SSH destination')
    if any(character in str(directory) for character in ('\n', '\r', '"', '%')):
        raise ValueError('Invalid configuration path')
    lines = host_key.read_text().strip().splitlines()
    fields = lines[0].split() if len(lines) == 1 else []
    if len(fields) < 2 or fields[0] != 'ssh-ed25519':
        raise ValueError('An independently verified Ed25519 host public key is required')
    raw = base64.b64decode(fields[1], validate=True)
    if len(raw) != 51 or raw[:19] != b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20':
        raise ValueError('Invalid Ed25519 host public key')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ValueError('Dedicated SSH directory must not be a symlink')
    allowed = {'environment', 'id_ed25519', 'id_ed25519.pub', 'known_hosts', 'ssh_config'}
    if any(path.name not in allowed for path in directory.iterdir()):
        raise ValueError('Use a dedicated directory, not broader SSH settings')
    marker = directory / 'environment'
    if marker.is_symlink() or (marker.exists() and marker.read_text() != environment + '\n'):
        raise ValueError('Dedicated directory belongs to another environment')
    existing = directory / 'ssh_config'
    if existing.exists() and f'Host t3-{environment}\n' not in existing.read_text():
        raise ValueError('Cannot reuse another environment configuration')
    if not marker.exists() and not existing.exists() and (directory / 'id_ed25519').exists():
        raise ValueError('Cannot adopt an unclassified existing login identity')
    directory.chmod(0o700)
    write_private(marker, environment + '\n')
    identity = directory / 'id_ed25519'
    if any((directory / name).is_symlink() for name in ('id_ed25519', 'id_ed25519.pub', 'known_hosts', 'ssh_config')):
        raise ValueError('Dedicated SSH files must not be symlinks')
    if not identity.exists():
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', f't3-{environment}-laptop', '-f', str(identity)], check=True)
    if identity.is_symlink() or not identity.with_suffix('.pub').is_file():
        raise ValueError('Invalid dedicated login identity')
    identity.chmod(0o600)
    known = directory / 'known_hosts'
    write_private(known, (host if port == 22 else f'[{host}]:{port}') + ' ' + ' '.join(fields[:2]) + '\n')
    config = directory / 'ssh_config'
    write_private(config, f'''Host t3-{environment}
  HostName {host}
  User {user}
  Port {port}
  IdentityFile "{identity}"
  IdentityAgent none
  IdentitiesOnly yes
  ForwardAgent no
  BatchMode yes
  PreferredAuthentications publickey
  PasswordAuthentication no
  KbdInteractiveAuthentication no
  StrictHostKeyChecking yes
  UserKnownHostsFile "{known}"
  GlobalKnownHostsFile /dev/null
  ExitOnForwardFailure yes
  ConnectTimeout 10
  ServerAliveInterval 15
  ServerAliveCountMax 3
''')
    print(f'Prepared {config}. Provision only {identity}.pub as an approved backend login key.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--environment', choices=('homelab', 'work'), default='homelab')
    parser.add_argument('--host', required=True)
    parser.add_argument('--user', default='node')
    parser.add_argument('--port', type=int, default=22)
    parser.add_argument('--host-key-file', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.directory.absolute(), args.host, args.user, args.port, args.host_key_file, args.environment)
