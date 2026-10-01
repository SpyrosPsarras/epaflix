#!/usr/bin/env python3
"""Run inside the candidate image, with a synthetic home and no credentials."""
import json
import base64
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
    remote = home / 'remote.git'
    subprocess.run(['git', 'init', '-q', '--bare', str(remote)], check=True)
    pathlib.Path('/v2-private').mkdir(exist_ok=True)
    pathlib.Path('/v2-private/bundle.json').write_text(json.dumps({'files': {}}))
    pathlib.Path('/private-agent-config').mkdir(exist_ok=True)
    pathlib.Path('/private-agent-config/bundle.json').write_text(json.dumps({'sourceHome': '/fixture', 'files': {'instructions.md': {'data': base64.b64encode(b'Synthetic instructions').decode(), 'executable': False}, 'skills/probe/SKILL.md': {'data': base64.b64encode(b'---\nname: probe\ndescription: synthetic\n---\n').decode(), 'executable': False}}}))
    env = {'PATH': os.environ['PATH'], 'HOME': str(home), 'T3CODE_HOME': str(home / '.t3'),
           'T3_PROJECT_DIR': str(project), 'T3_PROJECT_REPO': str(remote),
           'ANTHROPIC_BASE_URL': 'http://127.0.0.1:9', 'ANTHROPIC_AUTH_TOKEN': 'synthetic'}
    pathlib.Path('/v2/projects.json').write_text('[]')
    server = subprocess.Popen(['bash', '/v2/entrypoint.sh'], env=env)
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
        server.terminate()
        server.wait(timeout=40)
        backup = home / '.t3/backups/smoke'
        import shutil
        recovery_home = home / 'recovery'
        (recovery_home / 'userdata').mkdir(parents=True)
        shutil.copy2(backup / 'statev2.sqlite', recovery_home / 'userdata/statev2.sqlite')
        import sqlite3
        with sqlite3.connect(recovery_home / 'userdata/statev2.sqlite') as database:
            assert database.execute('select count(*) from projection_projects').fetchone()[0] == 1
        restored = subprocess.Popen(['/runtime/t3', 'start', '--no-browser', '--host', '127.0.0.1', '--port', '3773', '--base-dir', str(recovery_home), str(project)], env={**env, 'T3CODE_HOME': str(recovery_home)})
        try:
            for _ in range(60):
                try:
                    with urllib.request.urlopen('http://127.0.0.1:3773/.well-known/t3/environment', timeout=2) as response:
                        assert json.load(response)['orchestrationProtocolVersion'] == 2
                    break
                except OSError:
                    time.sleep(1)
            else:
                raise RuntimeError('Restored database did not boot')
        finally:
            restored.terminate()
            restored.wait(timeout=40)
        assert (home / '.codex/AGENTS.md').exists()
        assert (home / '.config/opencode/plugins/jev-auto.js').exists()
        assert (home / '.local/bin/kubelogin').resolve() == pathlib.Path('/usr/local/bin/kubelogin')
        print('PASS: shared entrypoint, supervisor, private instructions, plugins, scoped monitoring, idle observation, backup and restored boot')
    finally:
        if server.poll() is None:
            server.terminate()
            server.wait(timeout=40)
