#!/usr/bin/env python3
"""Install the owner's Forgejo AGENTS.md as Pi's instructions; keep the current file on any failure."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

GUIDANCE = Path('/opt/t3/isolated-guidance.md')
SSH_GUIDES = {'homelab': Path('/scripts/homelab-ssh.md'), 'work': Path('/scripts/work-ssh.md')}
REPOS = {'homelab': 'https://git.epaflix.com/spypsarras/skills.git',
         'work': 'https://git.epaflix.com/spypsarras/skills-work.git'}
LIMIT = 65536
MESSAGES = {'unprepared': 'instructions: home not prepared yet',
            'unchanged': 'instructions: unchanged',
            'kept': 'instructions: fetch failed or file invalid; current file kept'}


def fetch(home, repo):
    cache = home / '.local/share/t3-instructions'
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}

    def git(*args):
        return subprocess.run(['git', *args], env=env, capture_output=True, timeout=30, check=True).stdout

    try:
        if not (cache / 'HEAD').exists():
            git('init', '-q', '--bare', str(cache))
        git('-C', str(cache), 'fetch', '-q', '--depth', '1', repo, 'main')
        return git('-C', str(cache), 'show', 'FETCH_HEAD:AGENTS.md'), \
            git('-C', str(cache), 'rev-parse', '--short', 'FETCH_HEAD').decode().strip()
    except (OSError, subprocess.SubprocessError):
        return None, None


def sync(home, repo, ssh_path, guidance=GUIDANCE):
    data, revision = fetch(home, repo)
    try:
        text = data.decode() if data is not None and len(data) <= LIMIT else ''
    except UnicodeDecodeError:
        text = ''
    if not text.strip():
        return 'kept'
    content = (text + '\n' + guidance.read_text() + '\n' + ssh_path.read_text()).encode()
    target = home / '.pi/agent/AGENTS.md'
    if target.exists() and target.read_bytes() == content:
        return 'unchanged'
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        handle.write(content)
    os.chmod(handle.name, 0o600)
    os.replace(handle.name, target)
    return f'updated {revision}'


def main():
    if sys.argv[1:] not in (['once'], ['loop']):
        sys.exit('usage: instructions-sync.py once|loop')
    instance = os.environ['MCP_HUB_INSTANCE']
    home = Path(os.environ['HOME'])
    last = None
    while True:
        if (home / '.config/t3-runtime/environment').exists():
            status = sync(home, REPOS[instance], SSH_GUIDES[instance])
        else:
            status = 'unprepared'
        if status != last:
            print(MESSAGES.get(status, f'instructions: {status}'), flush=True)
            last = status
        if sys.argv[1] == 'once':
            return
        time.sleep(60)


if __name__ == '__main__':
    main()
