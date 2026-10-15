import json
from pathlib import Path
import re
import secrets
import subprocess


def kubectl(arguments, data=None):
    result = subprocess.run(['kubectl', *arguments], input=data, text=True, capture_output=True)
    if result.returncode:
        raise SystemExit('Secret preparation command failed')
    return result.stdout.strip()


identities = json.loads((Path(__file__).resolve().parent / 'identities.json').read_text())
vault_namespace = identities['vaultNamespace']
kubectl(['get', 'namespace', vault_namespace, '-o', 'name'])
for organization in identities['organizations']:
    namespace = organization['namespace']
    if not kubectl(['get', 'namespace', namespace, '--ignore-not-found', '-o', 'name']):
        kubectl(['create', 'namespace', namespace])
targets = [(vault_namespace, 'vaultwarden-org-setup')]
targets.extend((organization['namespace'], 'vaultwarden-agent') for organization in identities['organizations'])
for namespace, name in targets:
    existing = kubectl(['-n', namespace, 'get', 'secret', name, '--ignore-not-found', '-o', 'name'])
    if existing:
        print(namespace + '/' + name + ' already exists; left unchanged.')
        continue
    fields = {'BW_PASSWORD': secrets.token_urlsafe(48)}
    if name == 'vaultwarden-org-setup':
        deployment = json.loads(kubectl(['-n', namespace, 'get', 'deployment', 'vaultwarden', '-o', 'json']))
        container = next(item for item in deployment['spec']['template']['spec']['containers'] if item['name'] == 'vaultwarden')
        owner = next(item.get('value', '') for item in container['env'] if item['name'] == 'ORG_CREATION_USERS').strip().lower()
        if not re.fullmatch(r'[^\s,@]+@[^\s,@]+', owner) or owner == identities['setupEmail']:
            raise SystemExit('Prepare requires the original single-owner configuration')
        fields.update({'ADMIN_TOKEN': secrets.token_urlsafe(48), 'BW_EMAIL': identities['setupEmail'], 'OWNER_EMAIL': owner, 'stage': identities['checkpoints']['prepared']})
    resource = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'namespace': namespace, 'name': name}, 'type': 'Opaque', 'stringData': fields}
    print(kubectl(['create', '-f', '-', '-o', 'name'], json.dumps(resource)))
