#!/usr/bin/env bash
set -euo pipefail
config=${1:?usage: connect-laptop.sh /absolute/path/ssh_config [local-port]}
port=${2:-13773}
[[ $config == /* && -f $config && $port =~ ^[0-9]+$ ]]
((port > 1024 && port <= 65535))
exec ssh -F "$config" -o IdentityAgent=none -o ForwardAgent=no -o IdentitiesOnly=yes \
  -o ExitOnForwardFailure=yes -N -L "127.0.0.1:$port:127.0.0.1:3773" t3-homelab
