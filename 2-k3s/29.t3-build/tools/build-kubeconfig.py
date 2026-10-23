#!/usr/bin/env python3
"""Owner-only: give one T3 pod a kubeconfig scoped to its t3-build-<pod> namespace.

Modelled on 13.t3code/one/tools/manager-kubeconfig.py. Creates the
builder-token Secret in t3-build-<pod>, waits for the token controller, and
writes Secret t3-build-kubeconfig into t3code-<pod>. The context's namespace is
how the pod's helpers find their namespace. Never prints values.
"""
import argparse
import base64
import json
import subprocess
import sys
import time

PODS = {'homelab': 't3code-homelab', 'work': 't3code-work'}
GRANT = 't3-build-kubeconfig'


def kubeconfig(token: bytes, ca: bytes, user: str, namespace: str) -> bytes:
    if not token or not ca:
        raise ValueError('Missing account data')
    return json.dumps({'apiVersion': 'v1', 'kind': 'Config', 'current-context': 't3-build',
        'clusters': [{'name': 't3-build', 'cluster': {'server': 'https://kubernetes.default.svc',
            'certificate-authority-data': base64.b64encode(ca).decode()}}],
        'users': [{'name': user, 'user': {'token': token.decode()}}],
        'contexts': [{'name': 't3-build', 'context': {'cluster': 't3-build', 'user': user,
                                                      'namespace': namespace}}]},
        sort_keys=True).encode()


def provision(context: str, pod: str, run=subprocess.run) -> None:
    if not context.strip():
        raise ValueError('Explicit owner context required')
    target, builder = PODS[pod], f't3-build-{pod}'
    account, token_secret = 'builder', 'builder-token'

    def command(namespace, *args, resource=None):
        result = run(['kubectl', '--context', context, '-n', namespace, *args],
                     input=json.dumps(resource).encode() if resource is not None else None,
                     capture_output=True, timeout=10)
        if result.returncode:
            raise RuntimeError('Kubernetes operation failed')
        return result.stdout

    def get(namespace, kind, name):
        raw = command(namespace, 'get', kind, name, '--ignore-not-found', '-o', 'json')
        return json.loads(raw) if raw.strip() else None

    def create(namespace, name, secret_type, data, annotations=None):
        metadata = {'name': name, 'namespace': namespace}
        if annotations:
            metadata['annotations'] = annotations
        command(namespace, 'create', '-f', '-', resource={
            'apiVersion': 'v1', 'kind': 'Secret', 'metadata': metadata,
            'type': secret_type, 'data': data})

    if not get(builder, 'serviceaccount', account):
        raise ValueError('Tracked account required')
    secret = get(builder, 'secret', token_secret)
    if secret is None:
        create(builder, token_secret, 'kubernetes.io/service-account-token', {},
               {'kubernetes.io/service-account.name': account})
    for attempt in range(30):
        if attempt or secret is None:
            secret = get(builder, 'secret', token_secret)
        if (not secret or secret.get('type') != 'kubernetes.io/service-account-token'
                or secret.get('metadata', {}).get('annotations', {}).get(
                    'kubernetes.io/service-account.name') != account):
            raise ValueError('Unexpected account grant')
        data = secret.get('data', {})
        if data.get('token') and data.get('ca.crt'):
            config = kubeconfig(base64.b64decode(data['token'], validate=True),
                                base64.b64decode(data['ca.crt'], validate=True), account, builder)
            break
        if attempt < 29:
            time.sleep(1)
    else:
        raise RuntimeError('Account grant was not populated')
    current = get(target, 'secret', GRANT)
    if current is not None:
        data = current.get('data', {})
        if (current.get('type') != 'Opaque' or set(data) != {'kubeconfig'}
                or json.loads(base64.b64decode(data['kubeconfig'], validate=True)) != json.loads(config)):
            raise ValueError('Existing grant requires owner intervention')
        return
    create(target, GRANT, 'Opaque', {'kubeconfig': base64.b64encode(config).decode()})


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('Invalid arguments')


def main():
    parser = Parser(description='Owner-only t3-build kubeconfig provisioning. No secrets are displayed.')
    parser.add_argument('--context', required=True, help='Explicit owner desktop admin context')
    parser.add_argument('--pod', required=True, choices=sorted(PODS))
    try:
        args = parser.parse_args()
        provision(args.context, args.pod)
    except (Exception, KeyboardInterrupt):
        sys.exit('t3-build kubeconfig provisioning failed. Check resource presence and existing grants privately before retrying.')
    print(f't3-build kubeconfig prepared for {args.pod}.')


if __name__ == '__main__':
    main()
