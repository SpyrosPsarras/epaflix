#!/usr/bin/env python3
"""Import an owner-supplied AKS kubeconfig into a new private input directory, offline."""
import argparse
import base64
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / '13.t3code/env/files', ROOT / '13.t3code/one/files'):
    sys.path.insert(0, str(path))
auth = importlib.import_module('work-aks-auth')
hub_services = importlib.import_module('hub_services')
INPUTS = {'bundle.json', 'policies.json', 'services.json', 'kubeconfig', 'authorized_keys', 'work-auth.json'}
OLD_ENVIRONMENT = 'Environment: this is the work backend. Kubernetes access is the AKS dev cluster only. Never request or use a production context.'
APPROVED_ENVIRONMENT = ('Environment: this is the work backend. Kubernetes access is limited to the approved non-production work clusters in /run/t3-inputs/work-auth.json. '
                        'List the approved contexts with kubectl config get-contexts. Select each cluster per command using its own context or a generated kubeconfig under /home/t3/.kube/work/. Never request or use a production context.')


class ImportError(ValueError):
    pass


def source_entries(kube, group):
    entries = kube.get(group, [])
    key = 'context' if group == 'contexts' else group[:-1]
    result = {entry['name']: entry[key] for entry in entries}
    if len(result) != len(entries):
        raise ImportError('Duplicate source kubeconfig names are forbidden')
    return result


def source_scope(user):
    if not user:
        return {}
    if set(user) == {'auth-provider'}:
        provider = user['auth-provider']
        config = provider.get('config', {})
        if set(provider) != {'name', 'config'} or provider['name'] != 'azure' or not set(config).issubset({
                'tenant-id', 'apiserver-id', 'subscription-id', 'client-id', 'environment', 'config-mode'}):
            raise ImportError('Source Azure provider must not contain credentials or external configuration')
        return {key: config[field] for key, field in (
            ('tenantId', 'tenant-id'), ('audience', 'apiserver-id'), ('subscriptionId', 'subscription-id')) if field in config}
    if set(user) != {'exec'}:
        raise ImportError('Source static credentials and external credential files are forbidden')
    command = user['exec']
    if not set(command).issubset({'apiVersion', 'command', 'args', 'env', 'installHint', 'interactiveMode', 'provideClusterInfo'}) or (
            command.get('command') != 'kubelogin' or command.get('env') not in (None, []) or
            command.get('apiVersion') not in {'client.authentication.k8s.io/v1', 'client.authentication.k8s.io/v1beta1'}):
        raise ImportError('Only an inert AKS kubelogin source without plugin environment is supported')
    args = command.get('args', [])
    flags = {'--tenant-id': 'tenantId', '-t': 'tenantId', '--server-id': 'audience', '-s': 'audience',
             '--client-id': 'clientId', '-c': 'clientId', '--environment': 'cloud', '-e': 'cloud',
             '--login': 'login', '-l': 'login'}
    if not isinstance(args, list) or not args or args[0] != 'get-token' or len(args) % 2 != 1:
        raise ImportError('Unsupported AKS source plugin arguments')
    values = {}
    for flag, value in zip(args[1::2], args[2::2]):
        key = flags.get(flag)
        if key is None or key in values or not isinstance(value, str) or not value or value.startswith('-'):
            raise ImportError('Unsupported or ambiguous AKS source plugin arguments')
        values[key] = value
    return {key: value for key, value in values.items() if key in auth.SCOPE_FIELDS}


def imported_entry(source, selector, name, metadata):
    contexts = source_entries(source, 'contexts')
    selected = selector if selector is not None else source.get('current-context')
    if not isinstance(selected, str) or selected not in contexts:
        raise ImportError('Select one existing source context explicitly or set its current-context')
    context = contexts[selected]
    if not set(context).issubset({'cluster', 'user', 'namespace'}):
        raise ImportError('Unsupported source context fields')
    cluster = source_entries(source, 'clusters')[context['cluster']]
    user = source_entries(source, 'users')[context['user']]
    endpoint = urlsplit(cluster.get('server', ''))
    if set(cluster) != {'server', 'certificate-authority-data'} or not (
            endpoint.scheme == 'https' and endpoint.hostname and not endpoint.username and not endpoint.password
            and not endpoint.query and not endpoint.fragment):
        raise ImportError('Source API requires HTTPS and an inline CA without TLS overrides or external files')
    ca = base64.b64decode(cluster['certificate-authority-data'], validate=True)
    if not ca:
        raise ImportError('Source API CA is empty')
    scope = source_scope(user)
    for key, value in metadata.items():
        if value is not None:
            if key in scope and scope[key] != value:
                raise ImportError('Supplied authentication metadata conflicts with the source kubeconfig')
            scope[key] = value
    auth.validate_scope(scope)
    pin = {'name': name, 'cluster': name, 'user': name, 'server': cluster['server'],
           'certificateAuthoritySha256': hashlib.sha256(ca).hexdigest(), 'authentication': scope}
    normalized = {'cluster': name, 'user': name}
    if 'namespace' in context:
        normalized['namespace'] = context['namespace']
    return pin, cluster, normalized


def stage(directory, kubeconfig, output, name, source_context=None, tenant=None, subscription=None, account=None, audience=None):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name):
        raise ImportError('Target context must be a simple name of at most 128 characters')
    directory, output = Path(directory), Path(output)
    if output.exists() or output.is_symlink():
        raise ImportError('Output must be a new private directory; existing output is never overwritten')
    if output.resolve().is_relative_to(directory.resolve()):
        raise ImportError('Output must be outside the existing input directory')
    if {path.name for path in directory.iterdir() if not path.name.startswith('.')} != INPUTS | {'runtime.json'}:
        raise ImportError('Supply exactly the seven reviewed work input files')
    data = {name: (directory / name).read_bytes() for name in INPUTS | {'runtime.json'}}
    runtime = json.loads(data['runtime.json'])
    if set(runtime['sha256']) != INPUTS or any(
            hashlib.sha256(data[name]).hexdigest() != runtime['sha256'][name] for name in INPUTS):
        raise ImportError('Reviewed input digest mismatch')
    hub_services.load_services(directory / 'services.json', 'work')
    kube = auth.load_kube(directory / 'kubeconfig')
    reviewed = auth.validate(directory, kube)
    if any(name == entry['name'] for group in ('contexts', 'clusters', 'users') for entry in kube[group]):
        raise ImportError('Target name collides with an existing reviewed entry; no input was changed')
    pin, cluster, context = imported_entry(auth.load_kube(Path(kubeconfig)), source_context, name, {
        'tenantId': tenant, 'subscriptionId': subscription, 'audience': audience,
        'account': {'name': account, 'type': 'user'} if account is not None else None})
    bundle = json.loads(data['bundle.json'])
    instructions = bundle['files']['instructions.md']
    text = base64.b64decode(instructions['data'], validate=True).decode()
    if text.count(OLD_ENVIRONMENT) + text.count(APPROVED_ENVIRONMENT) != 1:
        raise ImportError('Expected exactly one reviewed work environment sentence in the instruction bundle')
    instructions['data'] = base64.b64encode(text.replace(OLD_ENVIRONMENT, APPROVED_ENVIRONMENT).encode()).decode()
    reviewed['contexts'].append(pin)
    kube['clusters'].append({'name': name, 'cluster': cluster})
    kube['users'].append({'name': name, 'user': {'exec': {**auth.EXEC, 'args': ['token', name]}}})
    kube['contexts'].append({'name': name, 'context': context})
    for filename, value in (('work-auth.json', reviewed), ('kubeconfig', kube), ('bundle.json', bundle)):
        data[filename] = json.dumps(value).encode()
    runtime['sha256'] = {name: hashlib.sha256(data[name]).hexdigest() for name in INPUTS}
    data['runtime.json'] = json.dumps(runtime).encode()
    output.mkdir(mode=0o700)
    try:
        for filename, value in data.items():
            descriptor = os.open(output / filename, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(value)
        auth.validate(output, kube)
    except BaseException:
        shutil.rmtree(output)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('directory', 'kubeconfig', 'output', 'name'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--source-context')
    parser.add_argument('--tenant')
    parser.add_argument('--subscription')
    parser.add_argument('--account')
    parser.add_argument('--audience')
    args = parser.parse_args()
    try:
        stage(**vars(args))
    except (ImportError, auth.AuthError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print('Work cluster import refused or failed. Private diagnostics suppressed.', file=sys.stderr)
        return 1
    print('New private work inputs staged. No live credentials, Secrets, defaults or pods changed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
