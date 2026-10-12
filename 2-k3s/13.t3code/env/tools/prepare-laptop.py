#!/usr/bin/env python3
"""Prepare a dedicated laptop SSH identity and pinned backend configuration, offline."""
import argparse
import base64
from pathlib import Path
import re
import subprocess


def prepare(directory, host, user, port, host_key):
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
    directory.chmod(0o700)
    identity = directory / 'id_ed25519'
    if any((directory / name).is_symlink() for name in ('id_ed25519', 'id_ed25519.pub', 'known_hosts', 'ssh_config')):
        raise ValueError('Dedicated SSH files must not be symlinks')
    if not identity.exists():
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 't3-homelab-laptop', '-f', str(identity)], check=True)
    if identity.is_symlink() or not identity.with_suffix('.pub').is_file():
        raise ValueError('Invalid dedicated login identity')
    identity.chmod(0o600)
    known = directory / 'known_hosts'
    known.write_text((host if port == 22 else f'[{host}]:{port}') + ' ' + ' '.join(fields[:2]) + '\n')
    known.chmod(0o600)
    config = directory / 'ssh_config'
    config.write_text(f'''Host t3-homelab
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
    config.chmod(0o600)
    print(f'Prepared {config}. Provision only {identity}.pub as an approved backend login key.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--host', required=True)
    parser.add_argument('--user', default='node')
    parser.add_argument('--port', type=int, default=22)
    parser.add_argument('--host-key-file', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.directory.absolute(), args.host, args.user, args.port, args.host_key_file)
