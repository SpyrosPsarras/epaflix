#!/usr/bin/env python3
"""Encrypt skills/instructions for Git, or install a mounted private bundle."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile


def bundle(home):
    files = {}
    source = home / '.claude/skills'
    for path in sorted(source.rglob('*')):
        if path.is_symlink():
            raise ValueError('Skill source must contain real files')
        if path.is_file():
            files['skills/' + path.relative_to(source).as_posix()] = {
                'data': base64.b64encode(path.read_bytes()).decode(),
                'executable': bool(path.stat().st_mode & 0o111)}
    if not any(p.endswith('/SKILL.md') for p in files):
        raise ValueError('No skills found')
    files['instructions.md'] = {
        'data': base64.b64encode((home / '.claude/CLAUDE.md').read_bytes()).decode(),
        'executable': False}
    return {'sourceHome': str(home), 'files': files}


def install(payload, home):
    obj = json.loads(payload)
    decoded = {}
    for name, item in obj['files'].items():
        p = PurePosixPath(name)
        if p.is_absolute() or '..' in p.parts or str(p) != name:
            raise ValueError('Unsafe bundle path')
        if name != 'instructions.md' and not name.startswith('skills/'):
            raise ValueError('Unexpected bundle entry')
        data = base64.b64decode(item['data'], validate=True)
        if p.suffix == '.md':
            data = data.replace(obj['sourceHome'].encode() + b'/', str(home).encode() + b'/')
        decoded[name] = (data, 0o700 if item['executable'] else 0o600)
    if 'instructions.md' not in decoded or not any(p.endswith('/SKILL.md') for p in decoded):
        raise ValueError('Incomplete private configuration')
    root = home / '.local/share/t3-private-config'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / hashlib.sha256(payload).hexdigest()
    if not target.exists():
        stage = Path(tempfile.mkdtemp(dir=root))
        try:
            for name, (data, mode) in decoded.items():
                dest = stage / name
                dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                dest.write_bytes(data)
                dest.chmod(mode)
            stage.rename(target)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    links = {'.claude/skills': 'skills', '.agents/skills': 'skills',
             '.claude/CLAUDE.md': 'instructions.md', '.codex/AGENTS.md': 'instructions.md',
             '.config/opencode/AGENTS.md': 'instructions.md'}
    for name, relative in links.items():
        link = home / name
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() and not link.is_symlink():
            backup = link.with_name(link.name + '.before-git')
            if backup.exists():
                raise ValueError('Existing backup prevents migration')
            link.rename(backup)
        temp = link.with_name(link.name + '.new')
        temp.unlink(missing_ok=True)
        temp.symlink_to(target / relative)
        temp.replace(link)
    print('private agent configuration installed')


def pack(home, output):
    payload = json.dumps(bundle(home), sort_keys=True, separators=(',', ':')).encode()
    if len(payload) > 900_000:
        raise ValueError('Private configuration exceeds Secret size budget')
    secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {
        'name': 't3code-private-config', 'namespace': 't3code',
        'annotations': {
            'argocd.argoproj.io/sync-options': 'ServerSideApply=true',
            't3code.epaflix.com/private-revision': hashlib.sha256(payload).hexdigest()}},
        'type': 'Opaque', 'data': {'bundle.json': base64.b64encode(payload).decode()}}
    result = subprocess.run(['sops', '--encrypt', '--input-type', 'json', '--output-type', 'yaml',
                             '--filename-override', str(output), '/dev/stdin'],
                            input=json.dumps(secret).encode(), capture_output=True)
    if result.returncode:
        raise RuntimeError('SOPS encryption failed; destination unchanged')
    # Only ciphertext is written to the repository.
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as f:
        f.write(result.stdout)
        name = f.name
    os.replace(name, output)
    print('encrypted private configuration updated')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['pack', 'install'])
    parser.add_argument('path', type=Path)
    parser.add_argument('--home', type=Path, default=Path.home())
    args = parser.parse_args()
    if args.action == 'pack':
        pack(args.home.resolve(), args.path)
    else:
        install(args.path.read_bytes(), args.home.resolve())
