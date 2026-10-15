#!/usr/bin/env python3
import argparse
from pathlib import Path
import re
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1] / 'rbac'
EXCLUDED = {'t3code-work', 'mcp-hub-work', 'mcp-hub', 'remote-pi', 'searxng', 't3code', 'vaultwarden'}


def render(names):
    if not names or names != sorted(set(names)) or any(
            not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', name) or name in EXCLUDED
            or name.endswith('-work') or name.startswith('vault-org-') for name in names):
        raise ValueError('Use a sorted, explicit homelab namespace allowlist without work or credential namespaces')
    return yaml.safe_dump_all([{
        'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding',
        'metadata': {'name': 'homelab-agent-read', 'namespace': name},
        'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': 'view'},
        'subjects': [{'kind': 'ServiceAccount', 'name': 'homelab-agent', 'namespace': 't3code-homelab'}]
    } for name in names], sort_keys=False)


def main():
    parser = argparse.ArgumentParser(description='Regenerate explicit homelab namespace read bindings without cluster discovery.')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    result = render((ROOT / 'namespaces.txt').read_text().splitlines())
    target = ROOT / 'namespace-bindings.yaml'
    if args.check:
        if not target.exists() or target.read_text() != result:
            sys.exit('Scoped RBAC is stale; run tools/sync-rbac.py')
    else:
        target.write_text(result)


if __name__ == '__main__':
    main()
