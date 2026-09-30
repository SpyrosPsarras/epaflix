#!/usr/bin/env python3
"""Apply an explicit private-file allowlist, preserving v2 edits and auth refresh."""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import tempfile

MANAGED = {
    '.kube/config', '.kube/davidhornkubeconfig', '.config/gh/config.yml',
    '.config/gh/hosts.yml', '.config/opencode/opencode.json', '.codex/config.toml',
    '.claude/settings.json', '.claude.json', '.claude/.claude.json',
    '.local/bin/aks-auth', '.local/bin/aks-auth-central',
}
AUTH = {
    '.azure/msal_token_cache.json', '.azure/msal_http_cache.bin',
    '.azure/azureProfile.json', '.azure/config', '.azure/clouds.config',
    '.claude/.credentials.json', '.local/share/opencode/auth.json',
}
SEED = {'.config/opencode/opencode.json', '.codex/config.toml', '.claude.json', '.claude/.claude.json', '.claude/settings.json'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(home, files):
    home = home.resolve()
    state = home / '.local/state/t3-v2/provision.json'
    for parent in [state, *state.parents]:
        if parent == home:
            break
        if parent.is_symlink():
            raise ValueError('Symlink in provisioning metadata path')
    previous = json.loads(state.read_text()) if state.exists() else {}
    conflicts = []
    validated = {}
    for name, value in files.items():
        if name not in MANAGED | AUTH:
            raise ValueError('Path is outside provisioning allowlist')
        path = home / name
        for parent in [path, *path.parents]:
            if parent == home:
                break
            if parent.is_symlink():
                raise ValueError('Symlink in provisioning path')
        validated[name] = value.encode() if isinstance(value, str) else value
    for name, data in validated.items():
        path = home / name
        if path.exists():
            if name in AUTH | SEED:
                continue
            current = digest(path.read_bytes())
            if current != previous.get(name) and current != digest(data):
                conflicts.append(name)
                continue
        atomic(path, data, 0o700 if name.startswith('.local/bin/') else 0o600)
        previous[name] = digest(data)
    atomic(state, json.dumps(previous, sort_keys=True).encode())
    return conflicts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=pathlib.Path, required=True)
    parser.add_argument('--manifest', type=pathlib.Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    files = {name: base64.b64decode(value, validate=True) for name, value in manifest['files'].items()}
    conflicts = install(args.home, files)
    print(json.dumps({'conflicts': conflicts, 'installedOrPreserved': len(files)}))


if __name__ == '__main__':
    main()
