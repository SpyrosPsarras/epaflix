#!/usr/bin/env bash
# Workaround for pingdotgg/t3code #5749 and #14115.
set -euo pipefail

stop_launch_servers_sshd_cannot_forward_to() {
  pkill -f -- "/t3 serve .* --base-dir ${launch_home//./\\.}/\\.t3\$" || :
}

point_launch_state_at_running_t3_server() {
  launch_runtime=$launch_home/.t3/userdata/server-runtime.json
  if [[ -f $launch_runtime && $(<"$launch_runtime") =~ \"port\":[[:space:]]*([0-9]+) ]]; then
    state_dir=$launch_home/.t3/ssh-launch/${command##* }
    mkdir -p "$state_dir"
    rm -f "$state_dir/pid"
    state_copy=$(mktemp "$state_dir/port.XXXXXX")
    printf '%s\n' "${BASH_REMATCH[1]}" >"$state_copy"
    mv -f "$state_copy" "$state_dir/port"
    state_copy=$(mktemp "$state_dir/managed.XXXXXX")
    printf 'external\n' >"$state_copy"
    mv -f "$state_copy" "$state_dir/managed"
  fi
}

shell=${SHELL:-/bin/sh}
command=${SSH_ORIGINAL_COMMAND:-}
if [[ $command =~ ^sh\ -l\ -s\ --\ [0-9a-f]{16}$ ]]; then
  launch_home=$HOME/.t3-ssh-launch
  (umask 0077; mkdir -p "$launch_home")
  if [[ -L $launch_home ]]; then
    printf 'ssh-command: refusing %s: symlink\n' "$launch_home" >&2
    exit 1
  fi
  if [[ ! -O $launch_home ]]; then
    printf 'ssh-command: refusing %s: not owned by %s\n' "$launch_home" "$(id -un)" >&2
    exit 1
  fi
  chmod 00700 "$launch_home"
  mkdir -p "$launch_home/.t3/userdata" "$HOME/.t3/runtime"
  runtime_file=$HOME/.t3/userdata/server-runtime.json
  if [[ -f $runtime_file ]]; then
    runtime_copy=$(mktemp "$launch_home/.t3/userdata/server-runtime.json.XXXXXX")
    cp "$runtime_file" "$runtime_copy"
    mv -f "$runtime_copy" "$launch_home/.t3/userdata/server-runtime.json"
  else
    rm -f "$launch_home/.t3/userdata/server-runtime.json"
  fi
  if [[ ! -L $launch_home/.t3/runtime || $(readlink "$launch_home/.t3/runtime") != "$HOME/.t3/runtime" ]]; then
    ln -sfnT "$HOME/.t3/runtime" "$launch_home/.t3/runtime"
  fi
  stop_launch_servers_sshd_cannot_forward_to
  point_launch_state_at_running_t3_server
  export HOME=$launch_home
fi
if [[ -z $command ]]; then
  exec -a "-${shell##*/}" "$shell"
fi
exec "$shell" -c "$command"
