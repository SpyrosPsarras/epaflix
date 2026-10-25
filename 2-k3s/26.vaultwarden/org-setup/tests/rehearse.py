import base64
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.parse

import yaml

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = 'vault-org-1838-test'
SERVER = os.environ.get('VAULT_REHEARSAL_URL', '')
INGRESS_IP = os.environ.get('VAULT_REHEARSAL_INGRESS_IP', '')
HOST = urllib.parse.urlsplit(SERVER).hostname
OWNER = 'owner@example.invalid'
IDENTITIES = json.loads((ROOT / 'identities.json').read_text())
NAMESPACE_MAP = {IDENTITIES['vaultNamespace']: NAMESPACE}
IDENTITIES['vaultNamespace'] = NAMESPACE
for organization in IDENTITIES['organizations']:
    scratch_namespace = 'vault-org-1838-' + organization['name']
    NAMESPACE_MAP[organization['namespace']] = scratch_namespace
    organization['namespace'] = scratch_namespace
ORGANIZATIONS = IDENTITIES['organizations']
NAMESPACES = tuple(NAMESPACE_MAP.values())
CHECKPOINT = IDENTITIES['checkpoints']

spec = importlib.util.spec_from_file_location('renderer', ROOT / 'render-job.py')
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


def call(arguments, data=None):
    result = subprocess.run(arguments, input=data, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('Rehearsal command failed: ' + arguments[0])
    return result.stdout.strip()


def remap(resource):
    resource['metadata']['namespace'] = NAMESPACE_MAP.get(resource['metadata'].get('namespace'), NAMESPACE)
    for subject in resource.get('subjects', []):
        subject['namespace'] = NAMESPACE
    assert resource['metadata']['namespace'] in NAMESPACES
    return resource


def apply(resource):
    scratch_namespace = resource['kind'] == 'Namespace' and resource['metadata']['name'] in NAMESPACES
    scratch_resource = resource['metadata'].get('namespace') in NAMESPACES
    assert scratch_namespace or scratch_resource
    call(['kubectl', 'apply', '-f', '-'], json.dumps(resource))


def get(kind, name, namespace=NAMESPACE):
    return json.loads(call(['kubectl', '-n', namespace, 'get', kind, name, '-o', 'json']))


def secret(name, namespace=NAMESPACE):
    return {key: base64.b64decode(encoded).decode() for key, encoded in get('secret', name, namespace)['data'].items()}


def config(setup=False):
    deployment = next(yaml.safe_load_all((ROOT.parent / 'server.yaml').read_text()))
    deployment['metadata']['namespace'] = NAMESPACE
    pod_spec = deployment['spec']['template']['spec']
    container = pod_spec['containers'][0]
    pins = yaml.safe_load((ROOT.parent / 'kustomization.yaml').read_text())['images']
    pin = next(image for image in pins if image['name'] == 'vaultwarden/server')
    container['image'] = pin['name'] + ':' + pin['newTag'] + '@' + pin['digest']
    container.pop('envFrom', None)
    container['env'] = [entry for entry in container['env'] if entry['name'] != 'DATABASE_URL']
    settings = {'DOMAIN': SERVER, 'ORG_CREATION_USERS': OWNER, 'LOG_LEVEL': 'off', 'ADMIN_RATELIMIT_SECONDS': '1', 'LOGIN_RATELIMIT_SECONDS': '1', 'UNAUTHENTICATED_RATELIMIT_SECONDS': '1', 'SMTP_HOST': '127.0.0.1', 'SMTP_PORT': '9', 'SMTP_SECURITY': 'off', 'SMTP_FROM': 'setup@example.invalid'}
    for key, setting in settings.items():
        container['env'] = [entry for entry in container['env'] if entry['name'] != key] + [{'name': key, 'value': setting}]
    pod_spec['volumes'][0]['persistentVolumeClaim']['claimName'] = 'scratch-data'
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        (directory / 'server.yaml').write_text(yaml.safe_dump(deployment))
        build = {'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization', 'resources': ['server.yaml']}
        if setup:
            shutil.copytree(ROOT, directory / 'org-setup', ignore=shutil.ignore_patterns('node_modules', '__pycache__'))
            (directory / 'org-setup/identities.json').write_text(json.dumps(IDENTITIES))
            build['components'] = ['org-setup']
        (directory / 'kustomization.yaml').write_text(yaml.safe_dump(build))
        rendered = call(['kustomize', 'build', str(directory)])
    for resource in yaml.safe_load_all(rendered):
        apply(remap(resource))
    call(['kubectl', '-n', NAMESPACE, 'rollout', 'status', 'deployment/vaultwarden', '--timeout=180s'])


def job(expected_stage):
    previous = subprocess.run(['kubectl', '-n', NAMESPACE, 'get', 'job', 'vault-org-setup', '-o', 'json'], capture_output=True, text=True)
    if not previous.returncode:
        conditions = json.loads(previous.stdout).get('status', {}).get('conditions', [])
        finished = any(condition.get('type') in ['Complete', 'Failed'] and condition.get('status') == 'True' for condition in conditions)
        if not finished:
            raise RuntimeError('Previous rehearsal Job is still active')
        call(['kubectl', '-n', NAMESPACE, 'delete', 'job', 'vault-org-setup', '--wait=true'])
    manifest = renderer.render_job()
    manifest['metadata']['namespace'] = NAMESPACE
    manifest['spec']['template']['spec']['hostAliases'] = [{'ip': INGRESS_IP, 'hostnames': [HOST]}]
    call(['kubectl', 'create', '-f', '-'], json.dumps(manifest))
    safe_prefixes = tuple(organization['name'] + ':' for organization in ORGANIZATIONS) + ('Vault organization setup failed.',)
    for _ in range(360):
        conditions = {condition['type']: condition['status'] for condition in get('job', 'vault-org-setup').get('status', {}).get('conditions', [])}
        if conditions.get('Complete') == 'True' or conditions.get('Failed') == 'True':
            if conditions.get('Complete') != 'True':
                logs = call(['kubectl', '-n', NAMESPACE, 'logs', 'job/vault-org-setup', '-c', 'setup'])
                for line in logs.splitlines():
                    if line.startswith(safe_prefixes):
                        print(line, flush=True)
                raise RuntimeError('Setup Job failed')
            assert secret('vaultwarden-org-setup')['stage'] == expected_stage
            print('Setup Job passed: ' + expected_stage, flush=True)
            return
        time.sleep(5)
    raise RuntimeError('Setup Job timed out')


def check(action):
    agents = [{'name': organization['name'], 'credentials': secret('vaultwarden-agent', organization['namespace'])} for organization in ORGANIZATIONS]
    payload = {'action': action, 'server': SERVER, 'admin': secret('vaultwarden-org-setup').get('ADMIN_TOKEN'), 'credentials': secret('rehearsal-owner'), 'agents': agents}
    result = subprocess.run(['kubectl', '-n', NAMESPACE, 'exec', '-i', 'owner-tools', '--', 'bash', '-c', 'cd /work && PATH=/work/node_modules/.bin:$PATH node tests/owner.cjs'], input=json.dumps(payload), text=True, capture_output=True)
    if result.returncode:
        for line in result.stderr.splitlines():
            if line.startswith('Synthetic check failed at '):
                print(line, flush=True)
        raise RuntimeError('Synthetic account check failed')
    print('Synthetic check passed: ' + action, flush=True)


def main():
    url = urllib.parse.urlsplit(SERVER)
    valid_origin = url.scheme == 'https' and url.path in ['', '/'] and not (url.query or url.fragment or url.username or url.password)
    isolated_host = HOST and HOST.startswith('vault-org-1838-test.')
    if not valid_origin or not isolated_host or not INGRESS_IP:
        raise RuntimeError('Set VAULT_REHEARSAL_URL to an isolated vault-org-1838-test HTTPS host and VAULT_REHEARSAL_INGRESS_IP to its internal ingress IP')
    ipaddress.ip_address(INGRESS_IP)
    if call(['kubectl', 'config', 'current-context']) != 'epaflix':
        raise RuntimeError('Expected homelab context')
    for namespace in NAMESPACES:
        if subprocess.run(['kubectl', 'get', 'namespace', namespace], capture_output=True).returncode == 0:
            raise RuntimeError('Scratch namespace already exists; inspect it before removing it')
    try:
        apply({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': NAMESPACE, 'labels': {'purpose': 'vault-org-rehearsal'}}})
        apply({'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': {'name': 'scratch-data', 'namespace': NAMESPACE}, 'spec': {'accessModes': ['ReadWriteOnce'], 'storageClassName': 'local-path', 'resources': {'requests': {'storage': '1Gi'}}}})
        apply(remap(list(yaml.safe_load_all((ROOT.parent / 'server.yaml').read_text()))[1]))
        apply({'apiVersion': 'traefik.io/v1alpha1', 'kind': 'IngressRoute', 'metadata': {'name': 'vault-rehearsal', 'namespace': NAMESPACE}, 'spec': {'entryPoints': ['internal'], 'routes': [{'match': 'Host(`' + HOST + '`)', 'kind': 'Rule', 'services': [{'name': 'vaultwarden', 'port': 80}]}], 'tls': {}}})
        apply({'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': {'name': 'server', 'namespace': NAMESPACE}, 'spec': {'podSelector': {'matchLabels': {'app': 'vaultwarden'}}, 'policyTypes': ['Ingress'], 'ingress': [{'from': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'traefik-system'}}}], 'ports': [{'port': 8080}]}]}})
        apply({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'rehearsal-owner', 'namespace': NAMESPACE}, 'stringData': {'BW_EMAIL': OWNER, 'BW_PASSWORD': secrets.token_urlsafe(48)}})
        image = renderer.render_job()['spec']['template']['spec']['containers'][0]['image']
        apply({'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'owner-tools', 'namespace': NAMESPACE}, 'spec': {'automountServiceAccountToken': False, 'hostAliases': [{'ip': INGRESS_IP, 'hostnames': [HOST]}], 'containers': [{'name': 'browser', 'image': image, 'command': ['sleep', 'infinity'], 'volumeMounts': [{'name': 'work', 'mountPath': '/work'}]}], 'volumes': [{'name': 'work', 'emptyDir': {'medium': 'Memory'}}]}})
        call(['kubectl', '-n', NAMESPACE, 'wait', '--for=condition=Ready', 'pod/owner-tools', '--timeout=180s'])
        for file in ['setup.cjs', 'package.json', 'package-lock.json', 'tests/owner.cjs']:
            call(['kubectl', '-n', NAMESPACE, 'exec', 'owner-tools', '--', 'mkdir', '-p', str(Path('/work') / Path(file).parent)])
            call(['kubectl', '-n', NAMESPACE, 'cp', str(ROOT / file), 'owner-tools:/work/' + file])
        call(['kubectl', '-n', NAMESPACE, 'exec', '-i', 'owner-tools', '--', 'sh', '-c', 'cat > /work/identities.json'], json.dumps(IDENTITIES))
        call(['kubectl', '-n', NAMESPACE, 'exec', 'owner-tools', '--', 'bash', '-c', 'cd /work && npm ci --ignore-scripts --no-audit --no-fund >/dev/null 2>&1'])
        config()
        with tempfile.TemporaryDirectory() as directory:
            prepare = Path(directory) / 'prepare.py'
            shutil.copy(ROOT / 'prepare.py', prepare)
            (Path(directory) / 'identities.json').write_text(json.dumps(IDENTITIES))
            call(['python3', str(prepare)])
            passwords = [secret('vaultwarden-agent', organization['namespace'])['BW_PASSWORD'] for organization in ORGANIZATIONS]
            call(['python3', str(prepare)])
            assert passwords == [secret('vaultwarden-agent', organization['namespace'])['BW_PASSWORD'] for organization in ORGANIZATIONS]
        config(setup=True)
        job(CHECKPOINT['waitingForOwner'])
        check('waiting')
        check('register')
        job(CHECKPOINT['finalized'])
        check('verify')
        credentials = [secret('vaultwarden-agent', organization['namespace']) for organization in ORGANIZATIONS]
        job(CHECKPOINT['finalized'])
        assert credentials == [secret('vaultwarden-agent', organization['namespace']) for organization in ORGANIZATIONS]
        print('Setup rerun preserved credentials, organizations and collections', flush=True)
        config()
        settings = {entry['name']: entry.get('value') for entry in get('deployment', 'vaultwarden')['spec']['template']['spec']['containers'][0]['env']}
        assert settings['SMTP_HOST'] and '_ENABLE_SMTP' not in settings and 'ADMIN_TOKEN' not in settings
        call(['kubectl', '-n', NAMESPACE, 'patch', 'secret', 'vaultwarden-org-setup', '--type=merge', '--patch', json.dumps({'data': {'ADMIN_TOKEN': None, 'BW_PASSWORD': None}})])
        check('fresh-agents')
        print('SMTP-enabled fresh agent logins passed without email verification', flush=True)
        print('Ordinary agent emergency-access invitations refused', flush=True)
        job(CHECKPOINT['finalized'])
        for resource in yaml.safe_load_all((ROOT / 'access.yaml').read_text()):
            call(['kubectl', 'delete', '-f', '-', '--ignore-not-found'], json.dumps(remap(resource)))
        call(['kubectl', '-n', NAMESPACE, 'delete', 'configmap', 'vaultwarden-org-setup-scripts'])
        print('Temporary setup permissions and ConfigMap removed', flush=True)
        print('End-to-end rehearsal passed', flush=True)
    finally:
        for namespace in NAMESPACES:
            call(['kubectl', 'delete', 'namespace', namespace, '--wait=true', '--timeout=180s', '--ignore-not-found'])
        print('Scratch namespaces deleted', flush=True)


if __name__ == '__main__':
    main()
