#!/usr/bin/env bash
# `ssh t3code` (t3code-ssh Service), in its own container so its liveness probe
# (statefulset.yaml) restarts only sshd, never T3.
set -euo pipefail

# The probe. A refused connection means sshd is waiting for T3 (below); an
# sshd that accepts must send its greeting, so a hung sshd fails too.
if [[ ${1:-} == check ]]; then
  { exec 3<>/dev/tcp/127.0.0.1/2222; } 2>/dev/null || exit 0
  read -r -t 5 greeting <&3
  [[ $greeting == SSH-* ]]
  exit
fi

bash /scripts/ssh-config.sh || echo "t3env: ssh client config not written" >&2

# authorized_keys goes to the passwd home: StrictModes rejects the PVC home
# (mode 2777). Sessions do not get T3's env tokens. A setup failure idles the
# container instead of crash-looping it: a not-ready container takes the whole
# pod, and T3 with it, out of its Services.
SSH_DIR=$(getent passwd "$(id -u)" | cut -d: -f6)/.ssh
SSHD_DIR=$HOME/.ssh/sshd
if ! { [[ -r /scripts/sshd-authorized-keys && -x /usr/sbin/sshd ]] &&
  mkdir -p -m 0700 "$SSHD_DIR" &&
  { [[ -s $SSHD_DIR/ssh_host_ed25519_key ]] ||
    ssh-keygen -q -t ed25519 -N '' -C t3code -f "$SSHD_DIR/ssh_host_ed25519_key"; } &&
  install -m 0600 /scripts/sshd-authorized-keys "$SSH_DIR/authorized_keys" &&
  cat >"$SSHD_DIR/sshd_config" <<EOF &&
Port 2222
HostKey $SSHD_DIR/ssh_host_ed25519_key
PidFile none
UsePAM no
PasswordAuthentication no
KbdInteractiveAuthentication no
AllowAgentForwarding no
AllowTcpForwarding local
PermitOpen 127.0.0.1:3773 localhost:3773
X11Forwarding no
PermitTunnel no
PrintMotd no
SetEnv HOME=$HOME SSH_AUTH_SOCK=/tmp/t3-ssh-agent/agent.sock PATH=/tools/node_modules/.bin:/usr/local/bin:/usr/bin:/bin
EOF
  # /etc/profile resets PATH in login shells (`ssh t3code`), dropping the image CLIs.
  printf '%s\n' 'PATH=/tools/node_modules/.bin:$PATH' '[ -r "$HOME/.profile" ] && . "$HOME/.profile"' \
    >"$HOME/.bash_profile" &&
  /usr/sbin/sshd -t -f "$SSHD_DIR/sshd_config"; }; then
  echo "t3env: sshd not set up; ssh t3code unavailable" >&2
  exec sleep infinity
fi

# sshd runs only while T3 does. Before T3 listens, or while it restarts, an SSH
# launch from the T3 app would start its own server and could take port 3773
# (files/entrypoint.sh, server-runtime.json). The wait asks for an HTTP answer
# and caps each try, since a starting server can accept a request and never
# answer it, then for the entrypoint to name the server in server-runtime.json;
# the watch only needs the port, so a slow T3 keeps sshd running.
while :; do
  until curl -fs --max-time 5 -o /dev/null http://127.0.0.1:3773/ &&
    [[ -s $HOME/.t3/userdata/server-runtime.json ]]; do sleep 1; done
  /usr/sbin/sshd -D -e -f "$SSHD_DIR/sshd_config" &
  sshd=$!
  while kill -0 "$sshd" 2>/dev/null && (exec 3<>/dev/tcp/127.0.0.1/3773) 2>/dev/null; do sleep 2; done
  # Open connections too, as when sshd ran in the T3 container: one could
  # otherwise launch a server while port 3773 is free. OpenSSH 9.8+ names them
  # sshd-session.
  pkill -x 'sshd|sshd-session' || :
  wait "$sshd" || :
  echo "t3env: T3 or sshd stopped; sshd waits for T3 again" >&2
  sleep 5
done
