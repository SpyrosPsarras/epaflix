import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import yaml

root = Path(__file__).resolve().parents[2]
identities = json.loads((root / 'org-setup/identities.json').read_text())
backend_namespaces = {organization['namespace'] for organization in identities['organizations']}
with tempfile.TemporaryDirectory() as temporary:
    directory = Path(temporary) / 'vaultwarden'
    shutil.copytree(root, directory, ignore=shutil.ignore_patterns('node_modules', '__pycache__'))
    file = directory / 'kustomization.yaml'
    base = yaml.safe_load(file.read_text())
    base.pop('generators')
    base.pop('components', None)
    generator = yaml.safe_load((root / 'ksops-generator.yaml').read_text())
    metadata = []
    for name in generator['files']:
        for resource in yaml.safe_load_all((root / name).read_text()):
            metadata.append({key: resource[key] for key in ['apiVersion', 'kind', 'metadata']})
    (directory / 'secret-metadata.yaml').write_text(yaml.safe_dump_all(metadata))
    base['resources'].append('secret-metadata.yaml')
    original_owner = None
    for setup in [False, True, False]:
        config = copy.deepcopy(base)
        if setup:
            config['components'] = ['org-setup']
        file.write_text(yaml.safe_dump(config))
        rendered = subprocess.check_output(['kustomize', 'build', str(directory)], text=True)
        resources = list(yaml.safe_load_all(rendered))
        deployment = next(resource for resource in resources if resource['kind'] == 'Deployment')
        settings = {entry['name']: entry for entry in deployment['spec']['template']['spec']['containers'][0]['env']}
        if setup:
            assert settings['ORG_CREATION_USERS']['valueFrom']['secretKeyRef'] == {'name': 'vaultwarden-org-setup', 'key': 'BW_EMAIL'}
            assert settings['_ENABLE_SMTP']['value'] == 'false'
        else:
            original_owner = original_owner or settings['ORG_CREATION_USERS']['value']
            assert settings['ORG_CREATION_USERS']['value'] == original_owner
            assert '_ENABLE_SMTP' not in settings
        assert ('ADMIN_TOKEN' in settings) == setup
        scripts = [resource for resource in resources if resource['kind'] == 'ConfigMap' and resource['metadata']['name'] == 'vaultwarden-org-setup-scripts']
        assert bool(scripts) == setup
        roles = [resource for resource in resources if resource['kind'] in ['Role', 'RoleBinding', 'ServiceAccount'] and resource['metadata']['name'] == 'vaultwarden-org-setup']
        assert bool(roles) == setup
        role_namespaces = {resource['metadata'].get('namespace') for resource in roles}
        assert role_namespaces - {identities['vaultNamespace']} == (backend_namespaces if setup else set())
        if setup:
            assert set(scripts[0]['data']) == {'setup.cjs', 'identities.json', 'package.json', 'package-lock.json'}
        print('Metadata-only Kustomize render passed: ' + ('setup component' if setup else 'base'))

spec = importlib.util.spec_from_file_location('job_renderer', root / 'org-setup/render-job.py')
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)
job = renderer.render_job()
assert job['spec']['backoffLimit'] == 0
assert job['metadata']['name'] == 'vault-org-setup'
pod = job['spec']['template']['spec']
assert [container['name'] for container in pod['initContainers']] == ['kubectl']
assert {entry['name'] for entry in pod['containers'][0]['env']} == {'HOME', 'PATH', 'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD', 'NODE_ENV', 'npm_config_cache'}
print('Job environment checks passed')
