#!/usr/bin/env python3
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

CONTEXT = 'epaflix'


def backend_check():
    target = sys.stdin.read().strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', target):
        raise ValueError('Private repository metadata is required')
    result = subprocess.run(['gh', 'api', '--method', 'HEAD', '--include', 'repos/' + target],
                            text=True, capture_output=True)
    statuses = re.findall(r'^HTTP/[0-9.]+\s+(\d{3})\b', result.stdout, re.MULTILINE)
    if not statuses or statuses[-1] != '404' or result.returncode == 0:
        raise ValueError('The private repository did not return the required 404')


def owner_check():
    if os.environ.get('KUBERNETES_SERVICE_HOST'):
        raise ValueError('Run the owner step on a desktop, not in the mixed pod')
    result = subprocess.run(['gh', 'api', '--paginate',
        'user/repos?affiliation=organization_member&visibility=private&per_page=100',
        '--jq', '.[] | select(.private and .owner.type == "Organization") | .full_name'],
        text=True, capture_output=True)
    targets = result.stdout.splitlines()
    if result.returncode or not targets:
        raise ValueError('Owner GitHub access must identify a private organization repository')
    result = subprocess.run(['kubectl', '--context', CONTEXT, '-n', 't3code-homelab',
        'exec', '-i', 'homelab-0', '-c', 't3', '--', 'python3', '-c', Path(__file__).read_text(), '--backend'],
        input=targets[0], text=True, capture_output=True)
    if result.returncode:
        raise ValueError('Backend GitHub scope check failed')


def main():
    parser = argparse.ArgumentParser(description='Owner-only private metadata check. No credentials or organization names are displayed.')
    parser.add_argument('--backend', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.backend:
            backend_check()
        else:
            owner_check()
    except (Exception, KeyboardInterrupt):
        sys.exit('GitHub scope check failed; private target and diagnostics were not displayed')
    print('Private organization repository access denied with HTTP 404: PASS')


if __name__ == '__main__':
    main()
