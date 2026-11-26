#!/usr/bin/env bash
# Office VPNs for the work pod. `vpn serve` runs in the root vpn sidecar, the only container with the configs and
# NET_ADMIN. up/down/status run in t3 and sshd: they drop request files and read the status the sidecar publishes.
set -euo pipefail
root=${VPN_ROOT:-}
config=$root/run/t3-vpn-config
control=$root/run/t3-vpn
private=$root/tmp
net=$root/sys/class/net
logs='kubectl --context epaflix -n t3code-work logs work-0 -c vpn'
site_re='^[a-z0-9-]{1,11}$'

need_status() {
  [[ -s $control/status ]] || { echo 'vpn: no sites; the owner has not created the t3code-vpn Secret' >&2; exit 1; }
}

state() {
  sed -n "s/^$1 //p" "$control/status"
}

known() {
  need_status
  [[ $1 =~ $site_re && -n $(state "$1") ]] && return
  echo "vpn: unknown site '$1'; sites: $(cut -d' ' -f1 "$control/status" | paste -sd' ')" >&2
  exit 2
}

up() {
  known "$1"
  touch "$control/$1.on"
  for _ in {1..60}; do
    [[ $(state "$1") == up ]] && { echo "$1: up"; return; }
    [[ -e $control/$1.on ]] || { echo "$1: failed; owner: $logs" >&2; exit 1; }
    sleep 1
  done
  echo "$1: still connecting after 60s; the laptop may hold the same profile. 'vpn down $1' cancels." >&2
  exit 1
}

down() {
  known "$1"
  rm -f "$control/$1.on"
  for _ in {1..15}; do
    [[ $(state "$1") =~ ^(down)?$ ]] && { echo "$1: down"; return; }
    sleep 1
  done
  echo "$1: still up after 15s; owner: $logs" >&2
  exit 1
}

status() {
  need_status
  sed 's/ /: /' "$control/status"
}

sites() {
  local file
  for file in "$config"/*.ovpn "$config"/*.conf; do
    if [[ -f $file ]]; then file=${file##*/}; printf '%s\n' "${file%.*}"; fi
  done
  for file in "$private"/vpn-*.conf; do
    if [[ -f $file ]]; then file=${file##*/vpn-}; printf '%s\n' "${file%.conf}"; fi
  done
  if [[ ${#pid[@]} -gt 0 ]]; then printf '%s\n' "${!pid[@]}"; fi
}

profile() {
  local file
  for file in "$config/$1.ovpn" "$config/$1.conf"; do
    if [[ -f $file ]]; then echo "$file"; return; fi
  done
}

connect() {
  local site=$1 file=$2 dev=vpn-$1
  if [[ $file == *.ovpn ]]; then
    if [[ -z ${pid[$site]:-} ]]; then
      echo "vpn: $site: connecting"
      # Like NetworkManager's never-default: only the office's own routes, never the pod's default route.
      openvpn --config "$file" --dev "$dev" --dev-type tun --pull-filter ignore redirect-gateway &
      pid[$site]=$!
    elif ! kill -0 "${pid[$site]}" 2>/dev/null; then
      echo "vpn: $site: openvpn exited"
      unset "pid[$site]"
      rm -f "$control/$site.on"
    fi
  elif [[ ! -e $private/$dev.conf ]]; then
    echo "vpn: $site: connecting"
    # Office DNS would replace the pod's resolver, and wg-quick needs resolvconf for it. Kept until `wg-quick down`.
    sed '/^[[:space:]]*DNS[[:space:]]*=/d' "$file" >"$private/$dev.conf"
    wg-quick up "$private/$dev.conf" || { echo "vpn: $site: wg-quick failed"; rm -f "$private/$dev.conf" "$control/$site.on"; }
  fi
}

disconnect() {
  local site=$1 dev=vpn-$1
  if [[ -n ${pid[$site]:-} ]]; then
    kill "${pid[$site]}" 2>/dev/null || true
    wait "${pid[$site]}" 2>/dev/null || true
    unset "pid[$site]"
  fi
  if [[ -e $private/$dev.conf ]]; then
    wg-quick down "$private/$dev.conf" || true
    rm -f "$private/$dev.conf"
  fi
  if [[ -e $net/$dev ]]; then ip link delete dev "$dev" || true; fi
  echo "vpn: $site: down"
}

publish() {
  local file
  file=$(mktemp -p "$control" .status.XXXXXX)
  printf '%s' "$1" >"$file"
  chmod 0444 "$file"
  mv -f "$file" "$control/status"
}

active() {
  [[ -n ${pid[$1]:-} || -e $private/vpn-$1.conf || -e $net/vpn-$1 ]]
}

snapshot() {
  local site file on status=''
  requested=()
  for site in "$@"; do
    file=$(profile "$site")
    on=''
    if [[ -n $file && -e $control/$site.on ]]; then on=1; requested[$site]=1; fi
    if [[ -e $net/vpn-$site ]]; then status+="$site up"$'\n'
    elif [[ -n $on ]]; then status+="$site connecting"$'\n'
    elif active "$site"; then status+="$site stopping"$'\n'
    elif [[ -n $file ]]; then status+="$site down"$'\n'
    fi
  done
  if [[ $status != "$published" ]]; then
    publish "$status"
    published=$status
    echo "vpn: status: $(tr '\n' ' ' <<<"$status")"
  fi
}

serve() {
  umask 077
  declare -gA pid=() requested=()
  local published=- site file
  local -a list
  trap 'kill "${pid[@]}" 2>/dev/null || true; exit 0' TERM INT
  while :; do
    mapfile -t list < <(sites | sort -u | grep -E "$site_re" || true)
    snapshot "${list[@]}"
    for site in "${list[@]}"; do
      file=$(profile "$site")
      [[ -n $file ]] || rm -f "$control/$site.on"
      if [[ -n $file && -e $control/$site.on ]]; then
        # `vpn down` trusts a published `down`, so start only requests already published as `connecting`.
        if [[ -n ${requested[$site]:-} ]]; then connect "$site" "$file"; fi
      elif active "$site"; then
        disconnect "$site"
      fi
    done
    snapshot "${list[@]}"
    sleep 2
  done
}

case ${1:-}:${2:-} in
  serve: | status:) "$1" ;;
  up:?* | down:?*) "$1" "$2" ;;
  *) echo 'usage: vpn up <site> | vpn down <site> | vpn status' >&2; exit 2 ;;
esac
