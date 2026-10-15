#!/usr/bin/env bash
set -euo pipefail
connect_timeout_seconds=10
login=$(getent passwd "$(id -u)" | cut -d: -f6)
for home in "$login" "$HOME"; do
  mkdir -p "$home/.ssh"
  chmod 0700 "$home/.ssh"
  cat >"$home/.ssh/config" <<EOF
Include /scripts/homelab-ssh.conf

Host *
  IdentityFile none
  PreferredAuthentications publickey
  PasswordAuthentication no
  KbdInteractiveAuthentication no
  BatchMode yes
  ForwardAgent no
  ConnectTimeout $connect_timeout_seconds

Match final all
  IdentityAgent none
  StrictHostKeyChecking yes
EOF
  printf 'IdentityAgent /run/t3-vault/ssh-agent.sock\nUserKnownHostsFile %s/.ssh/known_hosts\nStrictHostKeyChecking accept-new\n' "$HOME" >"$home/.ssh/homelab-identity.conf"
  chmod 0600 "$home/.ssh/config" "$home/.ssh/homelab-identity.conf"
done
