#!/usr/bin/env bash
# Workaround for pingdotgg/t3code #5749 and #14115.
set -euo pipefail

shell=${SHELL:-/bin/sh}
command=${SSH_ORIGINAL_COMMAND:-}
if [[ $command =~ ^sh\ -l\ -s\ --\ [0-9a-f]{16}$ ]]; then
  launch_home=$HOME/.t3-ssh-launch
  [[ ! -L $launch_home ]] || exit 1
  if [[ ! -d $launch_home ]]; then
    mkdir -m 0700 "$launch_home"
  fi
  [[ -O $launch_home ]] || exit 1
  chmod 00700 "$launch_home"
  mkdir -p "$launch_home/.t3/userdata"
  rm -f "$launch_home/.t3/userdata/server-runtime.json"
  if [[ -f $HOME/.t3/userdata/server-runtime.json ]]; then
    cp "$HOME/.t3/userdata/server-runtime.json" "$launch_home/.t3/userdata/server-runtime.json"
  fi
  if [[ ! -L $launch_home/.t3/runtime || $(readlink "$launch_home/.t3/runtime") != "$HOME/.t3/runtime" ]]; then
    ln -sfnT "$HOME/.t3/runtime" "$launch_home/.t3/runtime"
  fi
  export HOME=$launch_home
fi
if [[ -z $command ]]; then
  exec -a "-${shell##*/}" "$shell"
fi
exec "$shell" -c "$command"
