#!/usr/bin/env bash
# SSH client config, run at start by both containers (entrypoint.sh, sshd.sh).
set -euo pipefail

# ssh expands ~ from /etc/passwd (/home/node), not $HOME, so its config goes
# there. It is rewritten on every start.
SSH_DIR=$(getent passwd "$(id -u)" | cut -d: -f6)/.ssh
mkdir -m 0700 "$SSH_DIR" 2>/dev/null || chmod 0700 "$SSH_DIR"
{
  # Homelab hosts (generated from the .lan DNS records, 1-proxmox/ssh). First,
  # so no Host block scopes the Include. They include
  # ~/.ssh/homelab-identity.conf: the key comes from SSH_AUTH_SOCK (the vault
  # agent in entrypoint.sh); host keys persist on the PVC and a new host's key
  # is saved on first connect, then must never change.
  printf 'Include /scripts/homelab-ssh.conf\n\n'
  # git@github.com: remotes (the work checkouts).
  if [[ -r /run/t3-github-ssh/identity ]]; then
    cat <<'EOF'
Host github.com
  User git
  IdentityFile /run/t3-github-ssh/identity
  IdentitiesOnly yes
  UserKnownHostsFile /run/t3-github-ssh/known_hosts
  StrictHostKeyChecking yes
EOF
  fi
} >"$SSH_DIR/config"
# ssh does not create the directory of a UserKnownHostsFile; a fresh PVC has none.
mkdir -p -m 0700 "$HOME/.ssh"
# OpenSSH 9.2 expands ~ in Include from $HOME (the PVC), not the passwd home,
# so write the file to both.
for d in "$SSH_DIR" "$HOME/.ssh"; do
  printf 'UserKnownHostsFile %s/.ssh/known_hosts\nStrictHostKeyChecking accept-new\n' "$HOME" >"$d/homelab-identity.conf"
  chmod 0600 "$d/homelab-identity.conf"
done
chmod 0600 "$SSH_DIR/config"
