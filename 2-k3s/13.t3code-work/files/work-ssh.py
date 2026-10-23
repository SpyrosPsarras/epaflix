#!/usr/bin/env python3
import base64
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import tempfile

INPUTS = Path('/run/t3-work-ssh')
# Pinned from https://api.github.com/meta ssh_keys (fingerprints:
# https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints).
GITHUB_KNOWN_HOSTS = '''\
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=
'''


def hosts(path):
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    if len(data) > 65536:
        raise ValueError
    if not data.strip():
        return []
    entries = json.loads(data)
    if not isinstance(entries, list) or len(entries) > 32:
        raise ValueError
    aliases = set()
    for entry in entries:
        if set(entry) != {'alias', 'address', 'user', 'port', 'hostKey'}:
            raise ValueError
        alias = entry['alias']
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', alias) or alias in aliases:
            raise ValueError
        aliases.add(alias)
        address = ipaddress.ip_address(entry['address'])
        if str(address) != entry['address'] or '%' in entry['address'] or address.is_loopback or address.is_multicast or address.is_unspecified or address.is_link_local:
            raise ValueError
        if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,63}', entry['user']):
            raise ValueError
        if type(entry['port']) is not int or not 1 <= entry['port'] <= 65535:
            raise ValueError
        if not re.fullmatch(r'(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/]+={0,2}', entry['hostKey']):
            raise ValueError
        base64.b64decode(entry['hostKey'].split()[1], validate=True)
        with tempfile.TemporaryDirectory(prefix='work-host-key-') as directory:
            key = Path(directory) / 'host.pub'
            key.write_text(entry['hostKey'] + '\n')
            result = subprocess.run(['ssh-keygen', '-l', '-f', str(key)], capture_output=True, timeout=5)
            if result.returncode:
                raise ValueError
    return entries


def write(path, content):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as output:
            output.write(content)
            os.fchmod(output.fileno(), 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(entries, homes):
    for home in set(homes):
        directory = home / '.ssh'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink():
            raise ValueError
        directory.chmod(0o700)
        config = ''
        known = ''
        for entry in entries:
            config += (f"Host {entry['alias']}\n  HostName {entry['address']}\n  User {entry['user']}\n"
                       f"  Port {entry['port']}\n"
                       f"Match originalhost {entry['alias']} host {entry['address']}\n"
                       '  ProxyCommand none\n  IdentityAgent /run/t3-vault/ssh-agent.sock\n'
                       f'  UserKnownHostsFile {directory}/work_known_hosts\n')
            address = entry['address'] if entry['port'] == 22 else f"[{entry['address']}]:{entry['port']}"
            known += address + ' ' + entry['hostKey'] + '\n'
        # The one hostname target, owner-approved; the Match refuses a HostName override.
        config += ('Host github.com\n  HostName github.com\n  User git\n  Port 22\n'
                   'Match originalhost github.com host github.com\n'
                   '  ProxyCommand none\n  IdentityAgent /run/t3-vault/ssh-agent.sock\n  IdentitiesOnly no\n'
                   f'  StrictHostKeyChecking yes\n  UserKnownHostsFile {directory}/github_known_hosts\n')
        config += ('Host *\n  IdentityAgent none\n  IdentityFile none\n'
                   '  PreferredAuthentications publickey\n  PasswordAuthentication no\n'
                   '  KbdInteractiveAuthentication no\n  BatchMode yes\n  ForwardAgent no\n'
                   '  ConnectTimeout 10\n  StrictHostKeyChecking yes\n  GlobalKnownHostsFile /dev/null\n'
                   '  UserKnownHostsFile /dev/null\n  ProxyCommand false\n')
        write(directory / 'work_known_hosts', known)
        write(directory / 'github_known_hosts', GITHUB_KNOWN_HOSTS)
        write(directory / 'config', config)


def policy(entries):
    return {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
            'metadata': {'name': 't3code-work-ssh', 'namespace': 't3code-work'},
            'spec': {'podSelector': {'matchLabels': {'app.kubernetes.io/name': 't3code'}},
                     'policyTypes': ['Egress'], 'egress': [
                         {'to': [{'ipBlock': {'cidr': str(ipaddress.ip_network(entry['address']))}}],
                          'ports': [{'protocol': 'TCP', 'port': entry['port']}]} for entry in entries]}}


def main(argv):
    try:
        if argv == ['install']:
            entries = hosts(INPUTS / 'hosts.json')
            install(entries, [Path.home(), Path(pwd.getpwuid(os.getuid()).pw_dir)])
        elif len(argv) == 3 and argv[0] == 'policy':
            entries = hosts(Path(argv[1]))
            write(Path(argv[2]), json.dumps(policy(entries)) + '\n')
        else:
            raise ValueError
        print('work SSH: private configuration prepared' if entries else 'work SSH: no approved hosts; SSH disabled')
        return 0
    except Exception:
        print('work SSH: invalid or unavailable private input', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
