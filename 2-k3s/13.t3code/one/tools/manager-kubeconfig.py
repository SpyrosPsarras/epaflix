#!/usr/bin/env python3
import argparse
import base64
import json
import subprocess
import sys
import time


def kubeconfig(token: bytes, ca: bytes) -> bytes:
    if not token or not ca:
        raise ValueError('Missing account data')
    return json.dumps({'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'manager',
        'clusters': [{'name': 'manager', 'cluster': {'server': 'https://kubernetes.default.svc',
            'certificate-authority-data': base64.b64encode(ca).decode()}}],
        'users': [{'name': 'manager-agent', 'user': {'token': token.decode()}}],
        'contexts': [{'name': 'manager', 'context': {'cluster': 'manager', 'user': 'manager-agent'}}]},
        sort_keys=True).encode()


def provision(context: str, run=subprocess.run) -> None:
    if not context.strip():
        raise ValueError('Explicit owner context required')

    def command(*args, resource=None):
        result = run(['kubectl', '--context', context, '-n', 't3code', *args],
                     input=json.dumps(resource).encode() if resource is not None else None,
                     capture_output=True, timeout=10)
        if result.returncode:
            raise RuntimeError('Kubernetes operation failed')
        return result.stdout

    def get(kind, name):
        raw = command('get', kind, name, '--ignore-not-found', '-o', 'json')
        return json.loads(raw) if raw.strip() else None

    def create(name, secret_type, data, annotations=None):
        metadata = {'name': name, 'namespace': 't3code'}
        if annotations:
            metadata['annotations'] = annotations
        command('create', '-f', '-', resource={
            'apiVersion': 'v1', 'kind': 'Secret', 'metadata': metadata,
            'type': secret_type, 'data': data})

    if not get('serviceaccount', 'manager-agent'):
        raise ValueError('Tracked account required')
    annotation = {'kubernetes.io/service-account.name': 'manager-agent'}
    account = get('secret', 'manager-agent-kubeconfig')
    if account is None:
        create('manager-agent-kubeconfig', 'kubernetes.io/service-account-token', {}, annotation)
    for attempt in range(30):
        if attempt or account is None:
            account = get('secret', 'manager-agent-kubeconfig')
        if (not account or account.get('type') != 'kubernetes.io/service-account-token'
                or account.get('metadata', {}).get('annotations', {}).get(
                    'kubernetes.io/service-account.name') != 'manager-agent'):
            raise ValueError('Unexpected account grant')
        data = account.get('data', {})
        if data.get('token') and data.get('ca.crt'):
            config = kubeconfig(base64.b64decode(data['token'], validate=True),
                                base64.b64decode(data['ca.crt'], validate=True))
            break
        if attempt < 29:
            time.sleep(1)
    else:
        raise RuntimeError('Account grant was not populated')
    current = get('secret', 'manager-kubeconfig')
    if current is not None:
        data = current.get('data', {})
        if (current.get('type') != 'Opaque' or set(data) != {'kubeconfig'}
                or json.loads(base64.b64decode(data['kubeconfig'], validate=True)) != json.loads(config)):
            raise ValueError('Existing grant requires owner intervention')
        return
    create('manager-kubeconfig', 'Opaque', {'kubeconfig': base64.b64encode(config).decode()})


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('Invalid arguments')


def main():
    parser = Parser(description='Owner-only manager Kubernetes grant provisioning. No secrets are displayed.')
    parser.add_argument('--context', required=True, help='Explicit owner desktop admin context')
    try:
        args = parser.parse_args()
        provision(args.context)
    except (Exception, KeyboardInterrupt):
        sys.exit('Manager kubeconfig provisioning failed. Check resource presence and existing grants privately before retrying.')
    print('Manager kubeconfig prepared.')


if __name__ == '__main__':
    main()
