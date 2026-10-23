#!/usr/bin/env python3
"""Build a container image with kaniko in the t3-build namespace and push it.

usage: t3-build <context-dir> --destination <registry/repo:tag> [--dockerfile path] [--docker-config file]

The Job template is the kaniko-job ConfigMap from 2-k3s/29.t3-build. The Job,
its context ConfigMap and registry-auth Secret are deleted when the build ends.
"""
import argparse
import base64
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import subprocess
import sys
import tarfile
import time

KUBECONFIG = os.environ.get('T3_BUILD_KUBECONFIG', '/run/t3-build/kubeconfig')
MISSING = ('{tool}: no {path}; the owner runs 2-k3s/29.t3-build/tools/build-kubeconfig.py '
           'and restarts the pod (see 2-k3s/29.t3-build/README.md).')
# shortcut: the context travels in one ConfigMap, so its gzipped tar must stay under ~1 MB;
# switch to a kaniko tar://stdin pod or an object store when bigger contexts are needed.
LIMIT = 1_000_000
REFERENCE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/:@+-]*')
LABELS = {'app.kubernetes.io/name': 't3-build'}


def kubectl(*args, data=None, capture=True):
    return subprocess.run(['kubectl', '--kubeconfig', KUBECONFIG, '-n', 't3-build', *args],
                          input=data, capture_output=capture, check=True)


def pack(context: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        tar.add(context, arcname='.', filter=lambda info: None if info.name == './.git' else info)
    return buffer.getvalue()


def create(resource, quiet=False):
    try:
        kubectl('create', '-f', '-', data=json.dumps(resource).encode())
    except subprocess.CalledProcessError as error:
        detail = '' if quiet else ': ' + error.stderr.decode(errors='replace').strip()
        raise SystemExit(f't3-build: creating {resource["kind"]} failed{detail}')


def parse(argv):
    parser = argparse.ArgumentParser(prog='t3-build', description=__doc__.split('\n')[0])
    parser.add_argument('context', type=Path)
    parser.add_argument('--destination', required=True, help='registry/repo:tag to push')
    parser.add_argument('--dockerfile', default='Dockerfile', help='path inside the context')
    parser.add_argument('--docker-config', type=Path, help='docker config.json with registry auth')
    args = parser.parse_args(argv)
    if not os.path.isfile(KUBECONFIG):
        sys.exit(MISSING.format(tool='t3-build', path=KUBECONFIG))
    dockerfile = PurePosixPath(args.dockerfile)
    if not REFERENCE.fullmatch(args.destination):
        parser.error('invalid --destination')
    if dockerfile.is_absolute() or '..' in dockerfile.parts or not REFERENCE.fullmatch(args.dockerfile):
        parser.error('--dockerfile must be a relative path inside the context')
    if not args.context.is_dir():
        parser.error('context must be a directory')
    docker_config = None
    if args.docker_config:
        try:
            docker_config = args.docker_config.read_bytes()
            if not isinstance(json.loads(docker_config), dict):
                raise ValueError
        except (OSError, ValueError):
            parser.error('--docker-config must be a readable docker config JSON file')
    context = pack(args.context)
    if len(context) > LIMIT:
        parser.error(f'gzipped context is {len(context)} bytes; the limit is {LIMIT}')
    return args, dockerfile, context, docker_config


def main(argv=None):
    args, dockerfile, context, docker_config = parse(argv)
    name = 't3-build-' + secrets.token_hex(4)
    template = kubectl('get', 'configmap', 'kaniko-job', '-o', r'jsonpath={.data.kaniko-job\.yaml}').stdout.decode()
    for placeholder, value in (('@NAME@', name), ('@DOCKERFILE@', str(dockerfile)),
                               ('@DESTINATION@', args.destination)):
        if f'"{placeholder}"' not in template:
            sys.exit('t3-build: the kaniko-job template lacks ' + placeholder)
        template = template.replace(f'"{placeholder}"', json.dumps(value))
    metadata = {'name': name, 'labels': LABELS}
    try:
        create({'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': metadata,
                'binaryData': {'context.tar.gz': base64.b64encode(context).decode()}})
        if docker_config is not None:
            create({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': metadata, 'type': 'Opaque',
                    'data': {'config.json': base64.b64encode(docker_config).decode()}}, quiet=True)
        try:
            kubectl('create', '-f', '-', data=template.encode())
        except subprocess.CalledProcessError as error:
            sys.exit('t3-build: creating Job failed: ' + error.stderr.decode(errors='replace').strip())
        subprocess.run(['kubectl', '--kubeconfig', KUBECONFIG, '-n', 't3-build', 'logs', '-f',
                        f'job/{name}', '--pod-running-timeout=10m'], stdin=subprocess.DEVNULL)
        # The template's activeDeadlineSeconds (1800) ends any build, so this loop ends too.
        while True:
            succeeded, _, failed = kubectl('get', 'job', name, '-o',
                                           'jsonpath={.status.succeeded},{.status.failed}').stdout.decode().partition(',')
            if succeeded or failed:
                break
            time.sleep(2)
        print(f't3-build: {name} {"pushed " + args.destination if succeeded else "failed"}', file=sys.stderr)
        return 0 if succeeded else 1
    finally:
        subprocess.run(['kubectl', '--kubeconfig', KUBECONFIG, '-n', 't3-build', 'delete', f'job/{name}',
                        f'configmap/{name}', f'secret/{name}', '--ignore-not-found', '--wait=false'],
                       capture_output=True)


if __name__ == '__main__':
    sys.exit(main())
