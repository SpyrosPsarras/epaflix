#!/usr/bin/env bash
set -euo pipefail
config=${1:?usage: connect-laptop.sh /absolute/path/ssh_config [local-port] [homelab|work]}
environment=${3:-homelab}
case "$environment" in homelab) default_port=13773 ;; work) default_port=23773 ;; *) exit 1 ;; esac
port=${2:-$default_port}
[[ $config == /* && -f $config && $port =~ ^[0-9]+$ ]]
((port > 1024 && port <= 65535))
exec ssh -F "$config" -o IdentityAgent=none -o ForwardAgent=no -o IdentitiesOnly=yes \
  -o ExitOnForwardFailure=yes -N -L "127.0.0.1:$port:127.0.0.1:3773" "t3-$environment"
