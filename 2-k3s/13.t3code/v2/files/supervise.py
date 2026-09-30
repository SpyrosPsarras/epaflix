#!/usr/bin/env python3
"""Start native T3; bootstrap projects and maintain a read-only monitoring session."""
import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request

home = pathlib.Path(os.environ['T3CODE_HOME'])
monitor = home / 'monitor.json'
origin = 'http://127.0.0.1:3773'


def request(path, token=None, payload=None, form=False):
    headers = {'x-t3-orchestration-protocol': '2'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = None
    if payload is not None:
        data = urllib.parse.urlencode(payload).encode() if form else json.dumps(payload).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
    with urllib.request.urlopen(urllib.request.Request(origin + path, data=data, headers=headers), timeout=30) as response:
        return json.load(response)


def bootstrap(register_projects=False):
    credential = home / 'bootstrap.json'
    admin = json.loads(subprocess.check_output(['/runtime/t3', 'auth', 'session', 'issue', '--base-dir', str(home), '--ttl', '1m', '--json'], text=True))
    try:
        fd = os.open(credential, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(admin, output)
        if register_projects:
            subprocess.run(['python3', '/v2/bootstrap-projects.py', '--manifest', '/v2/projects.json', '--origin', origin, '--credential', str(credential), '--register-only'], check=True)
        pairing = request('/api/auth/pairing-token', admin['token'], {'label': 'v2-rollout-monitor', 'scopes': ['access:read', 'orchestration:read']})
        session = request('/oauth/token', payload={'grant_type': 'urn:ietf:params:oauth:grant-type:token-exchange', 'subject_token': pairing['credential'], 'subject_token_type': 'urn:t3:params:oauth:token-type:environment-bootstrap', 'requested_token_type': 'urn:ietf:params:oauth:token-type:access_token', 'scope': 'access:read orchestration:read'}, form=True)
        session['renewAt'] = time.time() + min(session['expires_in'] / 2, 7 * 86400)
        spec = importlib.util.spec_from_file_location('provision', '/v2/provision.py')
        provision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(provision)
        provision.atomic(monitor, json.dumps(session).encode())
    finally:
        subprocess.run(['/runtime/t3', 'auth', 'session', 'revoke', '--base-dir', str(home), admin['sessionId']], check=True, stdout=subprocess.DEVNULL)
        credential.unlink(missing_ok=True)


server = subprocess.Popen(['/runtime/t3', *sys.argv[1:]])
signal.signal(signal.SIGTERM, lambda *_: server.terminate())
signal.signal(signal.SIGINT, lambda *_: server.terminate())
try:
    for _ in range(120):
        if server.poll() is not None:
            raise RuntimeError('T3 exited before bootstrap')
        try:
            request('/.well-known/t3/environment')
            break
        except OSError:
            time.sleep(1)
    else:
        raise RuntimeError('T3 startup timeout')
    projects_marker = home / 'projects-bootstrapped'
    if not projects_marker.exists():
        try:
            subprocess.run(['python3', '/v2/bootstrap-projects.py', '--manifest', '/v2/projects.json'], check=True)
            bootstrap(register_projects=True)
            projects_marker.write_text('done\n')
        except (OSError, ValueError, subprocess.SubprocessError):
            print('v2: initial project bootstrap failed; server stays available, acceptance is blocked', file=sys.stderr)
            bootstrap()
    else:
        bootstrap()
    while server.poll() is None:
        time.sleep(15)
        try:
            session = json.loads(monitor.read_text())
            if time.time() >= session['renewAt']:
                bootstrap()
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            print('v2: monitoring renewal failed; rollout reads will fail closed on expiry', file=sys.stderr)
finally:
    if server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
raise SystemExit(server.returncode)
