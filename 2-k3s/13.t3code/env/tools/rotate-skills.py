#!/usr/bin/env python3
import argparse
import base64
import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'env/files'), str(ROOT / 'one/files')]
validator = importlib.import_module('isolated-config')


def json_bytes(value, sort_keys=True):
    return json.dumps(value, sort_keys=sort_keys).encode()


def skill_name(name):
    if not name or name.startswith('.') or '/' in name or '\\' in name or not name.isprintable() or name.strip() != name:
        raise ValueError('A plain skill name is required')
    return name


def staged_skills(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('A skill directory is required')
    skills = {}
    for skill in sorted(directory.iterdir()):
        name = skill_name(skill.name)
        if skill.is_symlink() or not skill.is_dir() or not (skill / 'SKILL.md').is_file():
            raise ValueError('Each skill requires SKILL.md and no symlinks')
        files = {}
        pending = [skill]
        while pending:
            for path in sorted(pending.pop().iterdir()):
                mode = path.lstat().st_mode
                if stat.S_ISDIR(mode):
                    if not path.name.startswith('.'):
                        pending.append(path)
                elif stat.S_ISREG(mode):
                    files[f'skills/{name}/{path.relative_to(skill).as_posix()}'] = {
                        'data': base64.b64encode(path.read_bytes()).decode(),
                        'executable': bool(mode & 0o111)}
                else:
                    raise ValueError('Only regular skill files and directories are supported')
        skills[name] = files
    return skills


class Cluster:
    def __init__(self, context, environment):
        if not context.strip() or environment not in {'homelab', 'work'}:
            raise ValueError('Explicit context and environment required')
        self.context = context
        self.namespace = 't3code-' + environment

    def command(self, *args, value=None):
        result = subprocess.run(['kubectl', '--context', self.context, '-n', self.namespace, *args],
                                input=value, capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError('Kubernetes operation failed')
        return result.stdout


def rotate(cluster, environment, directory, remove, backup=None, check=False):
    if not check:
        if backup is None or backup.exists() or backup.is_symlink() or any(
                (parent / '.git').exists() for parent in backup.resolve().parents):
            raise ValueError('A new backup outside Git is required')
    staged = staged_skills(directory)
    removed = {skill_name(name) for name in remove}
    if not staged and not removed:
        raise ValueError('At least one staged or removed skill is required')
    if removed & staged.keys():
        raise ValueError('A skill cannot be staged and removed together')
    raw = cluster.command('get', 'secret', 't3-runtime-inputs', '-o', 'json')
    secret = json.loads(raw)
    data = secret['data']
    runtime_raw = base64.b64decode(data['runtime.json'], validate=True)
    runtime = json.loads(runtime_raw)
    inputs = validator.INPUTS | ({'work-auth.json'} if environment == 'work' else set())
    sort_keys = json_bytes(runtime) == runtime_raw
    if runtime['environment'] != environment or (
            not sort_keys and json_bytes(runtime, sort_keys=False) != runtime_raw) or set(runtime['sha256']) != inputs:
        raise ValueError('Reviewed runtime environment and serialization required')
    if any(hashlib.sha256(base64.b64decode(data[name], validate=True)).hexdigest() != digest
           for name, digest in runtime['sha256'].items()):
        raise ValueError('Reviewed input digest mismatch')
    bundle = json.loads(base64.b64decode(data['bundle.json'], validate=True))
    validator.validate_bundle(bundle)
    files = bundle['files'].copy()
    existing = {name.split('/')[1] for name in files if name.startswith('skills/') and len(name.split('/')) > 2}
    for name in removed | staged.keys():
        if f'skills/{name}' in files:
            raise ValueError('A plain file conflicts with the skill directory')
        prefix = f'skills/{name}/'
        matches = [path for path in files if path.startswith(prefix)]
        if name in removed and not matches:
            raise ValueError('Removed skill does not exist')
        for path in matches:
            del files[path]
        files.update(staged.get(name, {}))
    updated_bundle = {**bundle, 'files': files}
    validator.validate_bundle(updated_bundle)
    bundle_raw = json_bytes(updated_bundle)
    updated_runtime = {**runtime, 'sha256': {
        **runtime['sha256'], 'bundle.json': hashlib.sha256(bundle_raw).hexdigest()}}
    updated_data = {**data, 'bundle.json': base64.b64encode(bundle_raw).decode(),
                    'runtime.json': base64.b64encode(json_bytes(updated_runtime, sort_keys=sort_keys)).decode()}
    preserved_runtime = {**updated_runtime, 'sha256': {
        **updated_runtime['sha256'], 'bundle.json': runtime['sha256']['bundle.json']}}
    if json_bytes(preserved_runtime, sort_keys=sort_keys) != runtime_raw:
        raise ValueError('Unrelated runtime fields changed')
    if {name: value for name, value in updated_data.items() if name not in {'bundle.json', 'runtime.json'}} != {
            name: value for name, value in data.items() if name not in {'bundle.json', 'runtime.json'}}:
        raise ValueError('Unrelated Secret keys changed')
    summary = {'Added': staged.keys() - existing, 'Replaced': staged.keys() & existing, 'Removed': removed}
    if not check:
        with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        operations = [{'op': 'test', 'path': '/metadata/resourceVersion', 'value': secret['metadata']['resourceVersion']}]
        operations += [{'op': 'replace', 'path': '/data/' + name, 'value': updated_data[name]}
                       for name in ('bundle.json', 'runtime.json')]
        cluster.command('patch', 'secret', 't3-runtime-inputs', '--type', 'json', '--patch-file', '/dev/stdin',
                        value=json_bytes(operations))
    for action, names in summary.items():
        print(f'{action} skills: ' + (', '.join(sorted(names)) or 'none'))
    print(f'Unchanged skills: {len(existing - staged.keys() - removed)}')
    if not check:
        print('Skills rotation applied. Restart required.')


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('Invalid arguments')


def main():
    parser = Parser(description='Rotate only isolated runtime skills without displaying private input values.')
    parser.add_argument('--context', required=True)
    parser.add_argument('--environment', choices=('homelab', 'work'), required=True)
    parser.add_argument('--skills', type=Path, required=True)
    parser.add_argument('--remove', nargs='+', action='extend', default=[])
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--check', action='store_true')
    try:
        args = parser.parse_args()
        rotate(Cluster(args.context, args.environment), args.environment, args.skills, args.remove, args.backup, args.check)
    except (Exception, KeyboardInterrupt):
        sys.exit('Skills rotation failed. No captured diagnostics or secret values were printed.')


if __name__ == '__main__':
    main()
