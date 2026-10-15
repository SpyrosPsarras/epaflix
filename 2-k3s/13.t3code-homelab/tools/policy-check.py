#!/usr/bin/env python3
import json
from pathlib import Path
import subprocess
import sys

home = Path.home()
cli = home / sys.argv[1]
for command, denied in [('git status', False), ('git push --force origin main', True), ('rm -rf /', True)]:
    payload = {'hook_event_name': 'PreToolUse', 'session_id': 'homelab-policy-check', 'cwd': str(home),
               'tool_name': 'Bash', 'tool_input': {'command': command}}
    result = subprocess.run(['node', str(cli), 'hook', '--coding-cli'], input=json.dumps(payload),
                            text=True, capture_output=True, timeout=10)
    decision = {}
    if result.stdout.strip():
        decision = json.loads(result.stdout).get('hookSpecificOutput', {})
    blocked = result.returncode == 2 or decision.get('permissionDecision') == 'deny'
    if blocked != denied or result.returncode not in (0, 2):
        raise SystemExit(1)
