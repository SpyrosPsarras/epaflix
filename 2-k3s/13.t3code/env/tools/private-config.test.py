#!/usr/bin/env python3
"""Synthetic private config fixtures. Never reads real private content in CI."""
import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('private_config', ROOT / 'env/files/private-config.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture():
    files = {'skills/implement/SKILL.md': '---\nname: implement\ndescription: Synthetic CI skill\n---\nRun the test.\n',
             'instructions.md': 'Synthetic instructions. Read /fixture/.claude/skills/implement/SKILL.md.\n'}
    return {'sourceHome': '/fixture', 'files': {
        k: {'data': base64.b64encode(v.encode()).decode(), 'executable': False} for k, v in files.items()}}


def overlay(root):
    import yaml
    path = root / 'kustomization.yaml'
    cfg = yaml.safe_load(path.read_text())
    cfg.pop('generators')
    cfg['resources'].append('synthetic-private.yaml')
    path.write_text(yaml.safe_dump(cfg))
    secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {
        'name': 't3env-private-config', 'annotations': {'t3code.epaflix.com/private-revision': 'synthetic-v1'}},
        'type': 'Opaque', 'data': {'bundle.json': base64.b64encode(json.dumps(fixture()).encode()).decode()}}
    (root / 'synthetic-private.yaml').write_text(yaml.safe_dump(secret))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'overlay':
        overlay(Path(sys.argv[2]))
    elif len(sys.argv) == 3 and sys.argv[1] == 'fixture':
        Path(sys.argv[2]).write_text(json.dumps(fixture()))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            old = home / '.claude/skills/local'
            old.mkdir(parents=True)
            payload = json.dumps(fixture()).encode()
            module.install(payload, home)
            module.install(payload, home)
            assert (home / '.claude/skills.before-git/local').is_dir()
            assert (home / '.agents/skills/implement/SKILL.md').is_file()
            assert str(home) in (home / '.codex/AGENTS.md').read_text()
            updated = fixture()
            updated['files']['skills/implement/SKILL.md']['data'] = base64.b64encode(b'changed').decode()
            module.install(json.dumps(updated).encode(), home)
            assert (home / '.claude/skills/implement/SKILL.md').read_text() == 'changed'
            bad = fixture()
            bad['files']['../escape'] = bad['files']['instructions.md']
            try:
                module.install(json.dumps(bad).encode(), home)
                raise AssertionError('unsafe bundle accepted')
            except ValueError:
                pass
            assert (home / '.claude/skills/implement/SKILL.md').read_text() == 'changed'
        print('ok: private config migration, updates, native paths, and rejection')
