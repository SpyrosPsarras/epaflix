#!/usr/bin/env python3
"""Clone initial repositories without replacing existing work; register through HTTP."""
import argparse
import json
import pathlib
import subprocess
import urllib.request
import uuid


def clone_projects(projects):
    destinations = [project['destination'] for project in projects]
    if len(destinations) != len(set(destinations)):
        raise ValueError('Duplicate project destination')
    for project in projects:
        destination = pathlib.Path(project['destination'])
        remote = project['remoteUrl']
        if not remote or not destination.is_absolute():
            raise ValueError('Invalid project record')
        if destination.exists():
            if not (destination / '.git').exists():
                raise ValueError('Existing destination is not a repository')
            current = subprocess.check_output(['git', '-C', str(destination), 'remote', 'get-url', 'origin'], text=True).strip()
            if current.rstrip('/').removesuffix('.git') != remote.rstrip('/').removesuffix('.git'):
                raise ValueError('Existing repository remote differs')
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(['git', 'clone', '--quiet', remote, str(destination)], check=True)


def register(projects, origin, token):
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', 'x-t3-orchestration-protocol': '2'}
    with urllib.request.urlopen(urllib.request.Request(origin + '/api/projects', headers=headers), timeout=10) as response:
        snapshot = json.load(response)
    existing = {project['workspaceRoot'] for project in snapshot['projects'] if not project.get('deletedAt')}
    for project in projects:
        if project['destination'] in existing:
            continue
        payload = {'type': 'project.create', 'commandId': str(uuid.uuid4()), 'projectId': str(uuid.uuid4()),
                   'title': pathlib.Path(project['destination']).name, 'workspaceRoot': project['destination']}
        request = urllib.request.Request(origin + '/api/projects/mutate', data=json.dumps(payload).encode(), headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise ValueError('Project registration failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=pathlib.Path, required=True)
    parser.add_argument('--origin')
    parser.add_argument('--credential', type=pathlib.Path)
    parser.add_argument('--register-only', action='store_true')
    args = parser.parse_args()
    projects = json.loads(args.manifest.read_text())
    if not args.register_only:
        clone_projects(projects)
    if args.origin:
        if args.credential is None:
            parser.error('--credential is required with --origin')
        register(projects, args.origin.rstrip('/'), json.loads(args.credential.read_text())['token'])
    print(json.dumps({'projects': len(projects), 'registered': bool(args.origin)}))


if __name__ == '__main__':
    main()
