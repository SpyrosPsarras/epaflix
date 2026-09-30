#!/usr/bin/env python3
"""Read-only v2 observation. Errors are blockers, never idle evidence."""
import argparse
import json
import os
import pathlib
import sqlite3
import time
import urllib.request

ACTIVE = {'preparing', 'starting', 'running', 'waiting'}
TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted', 'rolled_back'}


def advance(idle_since, blockers, now, last_sample=None):
    if blockers:
        return {'idleSince': None, 'eligible': False}
    if idle_since is None or now < idle_since or (last_sample is not None and now - last_sample > 30):
        idle_since = now
    return {'idleSince': idle_since, 'eligible': now - idle_since >= 900}


def run_blockers(rows):
    blockers = []
    for row in rows:
        status = row['status']
        if status not in ACTIVE | TERMINAL | {'queued'}:
            raise ValueError('Unknown run status')
        payload = json.loads(row['payload_json'])
        if not isinstance(payload, dict):
            raise ValueError('Invalid run payload')
        held = payload.get('queueHeld', False)
        if type(held) is not bool:
            raise ValueError('Invalid queue hold')
        if status in ACTIVE:
            blockers.append('runs')
        if status == 'queued' and not held:
            blockers.append('queue')
    return blockers


def client_blockers(clients):
    if not isinstance(clients, list):
        raise ValueError('Invalid client list')
    for client in clients:
        if not isinstance(client, dict) or type(client.get('connected')) is not bool:
            raise ValueError('Invalid client connection state')
    return ['clients'] if any(client['connected'] for client in clients) else []


def monitor_headers(credential):
    if set(credential['scope'].split()) != {'access:read', 'orchestration:read'}:
        raise ValueError('Monitoring credential must have only read scopes')
    if not isinstance(credential['access_token'], str) or not credential['access_token']:
        raise ValueError('Missing monitoring credential')
    return {'Authorization': 'Bearer ' + credential['access_token'], 'x-t3-orchestration-protocol': '2'}


def status_blockers(kind, statuses):
    known = {
        'requests': {'pending', 'resolved', 'expired', 'cancelled'},
        'items': {'idle', 'pending', 'running', 'waiting', 'completed', 'failed', 'cancelled', 'interrupted'},
        'effects': {'pending', 'running', 'succeeded', 'failed', 'cancelled'},
    }
    if any(status not in known[kind] for status in statuses):
        raise ValueError('Unknown activity status')
    active = {'pending'} if kind == 'requests' else {'pending', 'running', 'waiting'}
    return [kind] if any(status in active for status in statuses) else []


def terminal_blockers(server_pid, proc_root=pathlib.Path('/proc')):
    # Only inspect the server's PTY children; inherited idle shells are allowed.
    processes = {}
    for directory in proc_root.iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            stat = (directory / 'stat').read_text()
            end = stat.rfind(')')
            fields = stat[end + 2:].split()
            processes[int(directory.name)] = (int(fields[1]), int(fields[4]), stat[stat.index('(') + 1:end])
        except FileNotFoundError:
            continue
    if server_pid not in processes:
        raise ValueError('Server process not observable')
    for pid, (parent, tty, command) in processes.items():
        if parent != server_pid or tty == 0:
            continue
        for child, (child_parent, _, child_command) in processes.items():
            if child_parent == pid and (child_command != command or any(p == child for p, _, _ in processes.values())):
                return ['terminal']
    return []


def observe(home, origin, credential):
    headers = monitor_headers(credential)
    request = urllib.request.Request(origin.rstrip('/') + '/api/auth/clients', headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        blockers = client_blockers(json.load(response))
    database = home / 'userdata/statev2.sqlite'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        blockers += run_blockers(db.execute('select status,payload_json from orchestration_v2_projection_runs'))
        blockers += status_blockers('requests', [row[0] for row in db.execute('select status from orchestration_v2_projection_runtime_requests')])
        blockers += status_blockers('items', [row[0] for row in db.execute("select status from orchestration_v2_projection_turn_items where type in ('command_execution','dynamic_tool','subagent')")])
        blockers += status_blockers('effects', [row[0] for row in db.execute('select status from orchestration_v2_effect_outbox')])
        checks = {
            'provider-background': "select count(*) from orchestration_v2_projection_provider_threads where json_array_length(payload_json,'$.pendingBackgroundTasks')>0",
            'effects': "select count(*) from orchestration_v2_effect_outbox where status in ('pending','running') and effect_type in ('provider-turn.start','provider-turn.interrupt','provider-turn.steer','provider-turn.restart','runtime-request.respond')",
            # 15 minute observation plus a conservative five-minute restart allowance.
            'schedule': "select count(*) from scheduled_tasks where last_run_status='running' or (enabled=1 and next_run_at <= strftime('%Y-%m-%dT%H:%M:%fZ','now','+20 minutes'))",
        }
        blockers += [name for name, sql in checks.items() if db.execute(sql).fetchone()[0]]
    runtime = json.loads((home / 'userdata/server-runtime.json').read_text())
    blockers += terminal_blockers(runtime['pid'])
    if os.statvfs(home).f_bavail * os.statvfs(home).f_frsize < 8 * 1024**3:
        blockers.append('disk')
    return sorted(set(blockers))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=pathlib.Path, required=True)
    parser.add_argument('--origin', default='http://127.0.0.1:3773')
    parser.add_argument('--credential', type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        blockers = observe(args.home.resolve(), args.origin, json.loads(args.credential.read_text()))
    except (OSError, ValueError, KeyError, sqlite3.Error):
        blockers = ['unknown']
    print(json.dumps({'eligible': not blockers, 'blockers': blockers}))
    return 1 if blockers else 0


if __name__ == '__main__':
    raise SystemExit(main())
