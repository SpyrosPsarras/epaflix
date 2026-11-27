#!/usr/bin/env python3
"""Install the owner's Forgejo AGENTS.md as Pi's instructions; keep the current file on any failure."""
import fcntl
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
CACHE = '.local/share/t3-instructions'
MESSAGES = {'unprepared': 'instructions: home not prepared yet',
            'nocache': 'instructions: cache unavailable; current file kept',
            'unchanged': 'instructions: unchanged',
            'kept': 'instructions: fetch failed or file invalid; last good copy in use',
            'fallback': 'instructions: fetch failed or file invalid; bundle instructions in use'}


def git(cache, *args):
    return subprocess.run(['git', '-C', str(cache), *args], env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
                          capture_output=True, timeout=30, check=True).stdout


def read(cache, ref):
    try:
        data = git(cache, 'cat-file', 'blob', f'{ref}:AGENTS.md')
        text = data.decode() if len(data) <= LIMIT else ''
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
    return text if text.strip() else None


def sync(home, repo, ssh_path, guidance=GUIDANCE):
    cache = home / CACHE
    revision = None
    try:
        cache.mkdir(parents=True, exist_ok=True)
        if not (cache / 'HEAD').exists():
            git(cache, 'init', '-q', '--bare')
        git(cache, 'fetch', '-q', '--depth', '1', repo, '+refs/heads/main:refs/remotes/origin/main')
        candidate = git(cache, 'rev-parse', 'refs/remotes/origin/main').decode().strip()
        if read(cache, candidate) is not None:
            git(cache, 'update-ref', 'refs/heads/good', candidate)
            revision = candidate[:12]
    except (OSError, subprocess.SubprocessError):
        revision = None
    text = read(cache, 'refs/heads/good')
    if text is None:
        return 'fallback'
    content = (text + '\n' + guidance.read_text() + '\n' + ssh_path.read_text()).encode()
    target = home / '.pi/agent/AGENTS.md'
    if not (target.exists() and target.read_bytes() == content):
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
            handle.write(content)
        os.replace(handle.name, target)
        if revision:
            return f'updated {revision}'
    return 'unchanged' if revision else 'kept'


def main():
    if sys.argv[1:] not in (['once'], ['loop']):
        sys.exit('usage: instructions-sync.py once|loop')
    instance = os.environ['MCP_HUB_INSTANCE']
    home = Path(os.environ['HOME'])
    last = None
    while True:
        if (home / '.config/t3-runtime/environment').exists():
            try:
                (home / CACHE).mkdir(parents=True, exist_ok=True)
                lock = open(home / CACHE / 'lock', 'w')
            except OSError:
                status = 'nocache'
            else:
                with lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
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
