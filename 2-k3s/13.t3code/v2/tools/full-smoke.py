#!/usr/bin/env python3
"""Run inside the candidate image, with a synthetic home and no credentials."""
import json
import os
import pathlib
import subprocess
import tempfile
import time
import urllib.request

with tempfile.TemporaryDirectory(prefix='v2-full-smoke-') as temp:
    home = pathlib.Path(temp)
    project = home / 'project'
    project.mkdir()
    subprocess.run(['git', 'init', '-q', str(project)], check=True)
    # Invoke the supervisor after managed startup is checked separately by CI tests.
    env = {'PATH': os.environ['PATH'], 'HOME': str(home), 'T3CODE_HOME': str(home / '.t3')}
    pathlib.Path('/v2/projects.json').write_text('[]')
    server = subprocess.Popen(['python3', '/v2/supervise.py', 'start', '--no-browser', '--host', '127.0.0.1', '--port', '3773', str(project)], env=env)
    try:
        monitor = home / '.t3/monitor.json'
        for _ in range(120):
            if server.poll() is not None:
                raise RuntimeError('Supervisor exited')
            if monitor.exists():
                break
            time.sleep(1)
        else:
            raise RuntimeError('Monitoring bootstrap timed out')
        session = json.loads(monitor.read_text())
        assert set(session['scope'].split()) == {'access:read', 'orchestration:read'}
        subprocess.run(['python3', '/v2/rollout-ready.py', '--home', str(home / '.t3'), '--credential', str(monitor)], check=True)
        subprocess.run(['python3', '/v2/backup.py', '--home', str(home / '.t3'), '--label', 'smoke'], check=True)
        print('PASS: supervisor, scoped monitoring, idle observation and backup')
    finally:
        server.terminate()
        server.wait(timeout=40)
