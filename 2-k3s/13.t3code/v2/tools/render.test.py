#!/usr/bin/env python3
"""Render v2 with synthetic secrets; never decrypt production bundles."""
import pathlib
import shutil
import subprocess
import tempfile
import yaml

root = pathlib.Path(__file__).parents[1]
with tempfile.TemporaryDirectory() as temp:
    target = pathlib.Path(temp)
    for source in root.glob('*.yaml'):
        if not source.name.endswith('.enc.yaml'):
            shutil.copy2(source, target / source.name)
    configuration = yaml.safe_load((target / 'kustomization.yaml').read_text())
    configuration.pop('generators')
    configuration['resources'].append('synthetic.yaml')
    (target / 'kustomization.yaml').write_text(yaml.safe_dump(configuration))
    secrets = [{'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name}, 'stringData': {key: 'synthetic'}} for name, key in [('t3code-v2-provision', 'bundle.json'), ('t3code-v2-mcp', 'token')]]
    (target / 'synthetic.yaml').write_text(yaml.safe_dump_all(secrets))
    documents = list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(target)], text=True)))
    names = [(document['kind'], document['metadata']['name']) for document in documents]
    assert len(names) == len(set(names))
    assert ('StatefulSet', 't3code-v2') in names
    assert ('StatefulSet', 't3code') not in names
    assert all(document['metadata'].get('namespace') == 't3code' for document in documents)
    print('PASS: isolated v2 render with unique resources and synthetic secrets')
