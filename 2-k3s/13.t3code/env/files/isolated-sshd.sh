#!/usr/bin/env bash
set -euo pipefail
if [[ ${1:-} == check ]]; then
  { exec 3<>/dev/tcp/127.0.0.1/2222; } 2>/dev/null || exit 0
  read -r -t 5 greeting <&3
  [[ $greeting == SSH-* ]]
  exit
fi
while ! curl -fs --max-time 5 -o /dev/null http://127.0.0.1:3773/health ||
      [[ ! -s $HOME/.t3/userdata/server-runtime.json ]]; do sleep 1; done
login=$(getent passwd "$(id -u)" | cut -d: -f6)/.ssh
state=$HOME/.config/t3-sshd
mkdir -p "$login" "$state"
chmod 0700 "$login" "$state"
install -m 0600 /run/t3-login/authorized_keys "$login/authorized_keys"
ssh-keygen -l -f "$login/authorized_keys" >/dev/null
if [[ ! -s $state/ssh_host_ed25519_key ]]; then
  ssh-keygen -q -t ed25519 -N '' -C homelab -f "$state/ssh_host_ed25519_key"
fi
cat >"$state/sshd_config" <<EOF
Port 2222
HostKey $state/ssh_host_ed25519_key
AuthorizedKeysFile $login/authorized_keys
PidFile none
UsePAM no
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
AllowAgentForwarding no
AllowTcpForwarding local
PermitOpen 127.0.0.1:3773 localhost:3773
X11Forwarding no
PermitTunnel no
PrintMotd no
ForceCommand /scripts/ssh-command.sh
SetEnv HOME=$HOME PATH=/tools/node_modules/.bin:/usr/local/bin:/usr/bin:/bin
EOF
/usr/sbin/sshd -t -f "$state/sshd_config"
trap 'pkill -x "sshd|sshd-session" || :' EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
while :; do
  until curl -fs --max-time 5 -o /dev/null http://127.0.0.1:3773/health &&
    [[ -s $HOME/.t3/userdata/server-runtime.json ]]; do sleep 1; done
  /usr/sbin/sshd -D -e -f "$state/sshd_config" &
  pid=$!
  while kill -0 "$pid" 2>/dev/null && (exec 3<>/dev/tcp/127.0.0.1/3773) 2>/dev/null; do sleep 2; done
  pkill -x 'sshd|sshd-session' || :
  wait "$pid" || :
  sleep 2
done
