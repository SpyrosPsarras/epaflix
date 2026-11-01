#!/usr/bin/env bash
set -euo pipefail

json_version() {
  python3 -c '
import json
import re
import sys

try:
    value = json.load(sys.stdin)[sys.argv[1]]
    if not isinstance(value, str):
        raise ValueError
    value = value.removeprefix(sys.argv[2])
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*(?:[-+][0-9A-Za-z.-]+)*", value):
        raise ValueError
except (ValueError, KeyError, TypeError):
    sys.exit("pegaprox-auto-update: invalid JSON or version field")
print(value)
' "$1" "${2-}"
}

fetch() {
  curl --fail --silent --show-error --location --connect-timeout 10 --max-time 60 "$1"
}

update_pegaprox() {
  local app_dir=$1 installed latest main_version main_pid
  installed=$(json_version version <"$app_dir/version.json")
  latest=$(fetch https://api.github.com/repos/PegaProx/project-pegaprox/releases/latest | json_version tag_name v)

  if [[ $installed == "$latest" ]]; then
    echo "pegaprox-auto-update: already at release $latest; skipping"
    return 0
  fi

  main_version=$(fetch https://raw.githubusercontent.com/PegaProx/project-pegaprox/main/version.json | json_version version)
  if [[ $main_version != "$latest" ]]; then
    echo "pegaprox-auto-update: main is not at release $latest (main: $main_version); skipping"
    return 0
  fi

  echo "pegaprox-auto-update: updating $installed to release $latest"
  cd "$app_dir"
  PEGAPROX_BRANCH=main bash ./update.sh <<< y
  main_pid=$(systemctl show -p MainPID --value pegaprox.service)
  sleep 30

  installed=$(json_version version <"$app_dir/version.json")
  if [[ $installed != "$latest" ]]; then
    echo "pegaprox-auto-update: expected release $latest after update, found $installed" >&2
    return 1
  fi
  if ! systemctl is-active --quiet pegaprox.service; then
    echo 'pegaprox-auto-update: pegaprox.service is not active after update' >&2
    return 1
  fi
  if [[ ! $main_pid =~ ^[1-9][0-9]*$ ]]; then
    echo 'pegaprox-auto-update: pegaprox.service has no main process after update' >&2
    return 1
  fi
  if [[ $main_pid != "$(systemctl show -p MainPID --value pegaprox.service)" ]]; then
    echo 'pegaprox-auto-update: pegaprox.service restarted after update' >&2
    return 1
  fi
  echo "pegaprox-auto-update: release $latest installed and pegaprox.service is stable"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  if (( EUID != 0 )); then
    echo 'pegaprox-auto-update: run as root' >&2
    exit 1
  fi
  update_pegaprox /opt/PegaProx
fi
