#!/usr/bin/env bash
set -euo pipefail
connect_timeout_seconds=10
# Pinned from https://api.github.com/meta ssh_keys (fingerprints:
# https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints).
github_known_hosts='github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=
'
login=$(getent passwd "$(id -u)" | cut -d: -f6)
for home in "$login" "$HOME"; do
  mkdir -p "$home/.ssh"
  chmod 0700 "$home/.ssh"
  cat >"$home/.ssh/config" <<EOF
Include /scripts/homelab-ssh.conf

Host github.com
  HostName github.com
  User git
  Port 22
Match originalhost github.com host github.com
  IdentityAgent /run/t3-vault/ssh-agent.sock
  IdentitiesOnly no
  StrictHostKeyChecking yes
  UserKnownHostsFile $home/.ssh/github_known_hosts

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
  printf '%s' "$github_known_hosts" >"$home/.ssh/github_known_hosts"
  printf 'IdentityAgent /run/t3-vault/ssh-agent.sock\nUserKnownHostsFile %s/.ssh/known_hosts\nStrictHostKeyChecking accept-new\n' "$HOME" >"$home/.ssh/homelab-identity.conf"
  chmod 0600 "$home/.ssh/config" "$home/.ssh/github_known_hosts" "$home/.ssh/homelab-identity.conf"
done
