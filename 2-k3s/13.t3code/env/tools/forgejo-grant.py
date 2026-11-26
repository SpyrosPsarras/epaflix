#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import time

SCOPES = 'write:repository,write:issue,read:user,read:organization'


def json_bytes(value, sort_keys=True):
    return json.dumps(value, sort_keys=sort_keys).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


class Cluster:
    def __init__(self, context):
        if not context.strip():
            raise ValueError('Explicit context required')
        self.context = context

    def run(self, namespace, *args, value=None):
        result = subprocess.run(['kubectl', '--context', self.context, '-n', namespace, *args],
                                input=value, capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError('Kubernetes operation failed')
        return result.stdout

    def get(self, namespace, kind, name):
        raw = self.run(namespace, 'get', kind, name, '--ignore-not-found', '-o', 'json')
        return json.loads(raw) if raw.strip() else None

    def patch(self, namespace, secret, version, operations):
        operations = [{'op': 'test', 'path': '/metadata/resourceVersion', 'value': version}, *operations]
        self.run(namespace, 'patch', 'secret', secret, '--type', 'json', '--patch-file', '/dev/stdin',
                 value=json_bytes(operations))

    def forgejo(self, *args):
        return self.run('forgejo', 'exec', 'deploy/forgejo', '--', 'forgejo', 'admin', 'user', *args)


def mint(cluster, user):
    if user not in cluster.forgejo('list').decode().split():
        raise ValueError('The Forgejo bot does not exist')
    token = cluster.forgejo('generate-access-token', '--username', user, '--token-name',
                            f'{user}-{time.strftime("%Y%m%d%H%M%S")}', '--scopes', SCOPES, '--raw').strip()
    if not re.fullmatch(rb'[0-9a-f]{40}', token):
        raise ValueError('Forgejo returned no token')
    return token


def install(cluster, environment):
    namespace = 't3code-' + environment
    workload = cluster.get(namespace, 'statefulset', environment)
    if cluster.get(namespace, 'pod', environment + '-0') or (workload and workload['spec'].get('replicas', 1) != 0):
        raise ValueError('The backend must be stopped and scaled to zero first')
    grants_secret = cluster.get(namespace, 'secret', 't3-runtime-grants')
    inputs_secret = cluster.get(namespace, 'secret', 't3-runtime-inputs')
    grants = {name: base64.b64decode(value, validate=True) for name, value in grants_secret['data'].items()}
    runtime_raw = base64.b64decode(inputs_secret['data']['runtime.json'], validate=True)
    runtime = json.loads(runtime_raw)
    sort_keys = json_bytes(runtime) == runtime_raw
    if runtime['environment'] != environment or (not sort_keys and json_bytes(runtime, sort_keys=False) != runtime_raw):
        raise ValueError('Reviewed runtime environment and serialization required')
    reviewed = {name: value for name, value in runtime['grantSha256'].items() if name != 'forgejo-token'}
    if reviewed != {name: digest(value) for name, value in grants.items() if name != 'forgejo-token'}:
        raise ValueError('Existing grants do not match their reviews')
    token = grants.get('forgejo-token')
    bound = runtime['grantSha256'].get('forgejo-token')
    if token is not None and not re.fullmatch(rb'[0-9a-f]{40}', token):
        raise ValueError('The stored Forgejo grant is malformed')
    if bound is not None and (token is None or digest(token) != bound):
        raise ValueError('The stored Forgejo grant does not match its review')
    if token is None:
        token = mint(cluster, f'skills-{environment}-bot')
        cluster.patch(namespace, 't3-runtime-grants', grants_secret['metadata']['resourceVersion'],
                      [{'op': 'add', 'path': '/data/forgejo-token', 'value': base64.b64encode(token).decode()}])
    updated = {**runtime, 'grantSha256': {**runtime['grantSha256'], 'forgejo-token': digest(token)}}
    if updated != runtime:
        cluster.patch(namespace, 't3-runtime-inputs', inputs_secret['metadata']['resourceVersion'],
                      [{'op': 'replace', 'path': '/data/runtime.json',
                        'value': base64.b64encode(json_bytes(updated, sort_keys=sort_keys)).decode()}])
    print(f'Forgejo grant for skills-{environment}-bot installed. Advance the input revision before starting the backend.')


def main():
    parser = argparse.ArgumentParser(description='Mint a Forgejo token for one isolated backend without displaying it.')
    parser.add_argument('--context', required=True)
    parser.add_argument('--environment', choices=('homelab', 'work'), required=True)
    args = parser.parse_args()
    try:
        install(Cluster(args.context), args.environment)
    except (Exception, KeyboardInterrupt):
        sys.exit('Forgejo grant failed. No captured diagnostics or secret values were printed.')


if __name__ == '__main__':
    main()
