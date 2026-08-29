#!/usr/bin/env bash

set -uo pipefail

PIHOLE="${PIHOLE:-192.168.10.30}"
PUBLIC_LB="${PUBLIC_LB:-192.168.10.101}"
SSH=(ssh -n -o ConnectTimeout=8 -o BatchMode=yes)
QUIET=0
[[ "${1:-}" == "-q" ]] && QUIET=1

pass=0
fail=0
skip=0
declare -a failures=()

report() {
    local verdict=$1 id=$2 detail=$3
    case $verdict in
        PASS) pass=$((pass + 1)) ;;
        FAIL) fail=$((fail + 1)); failures+=("${id}: ${detail}") ;;
        SKIP) skip=$((skip + 1)) ;;
    esac
    (( QUIET )) || printf '%-4s  %-42s  %s\n' "$verdict" "$id" "$detail"
}

ipv6_probe='
  printf "%s %s %s\n" \
    "$(ip -6 addr show scope global 2>/dev/null | grep -c "inet6")" \
    "$(ip -6 route show default 2>/dev/null | grep -c .)" \
    "$(ip -6 route show 2>/dev/null | grep -cE "^(2|3)[0-9a-f]*:")"
'

check_ipv6_host() {
    local label=$1 out
    if [[ $label == local ]]; then
        out=$(bash -c "$ipv6_probe" 2>/dev/null)
    else
        out=$("${SSH[@]}" "root@${label}" "$ipv6_probe" 2>/dev/null)
    fi
    if [[ -z $out ]]; then
        report SKIP "ipv6/${label}" "unreachable over ssh, not checked"
        return
    fi
    read -r addrs defroutes prefixes <<<"$out"
    if (( addrs == 0 && defroutes == 0 && prefixes == 0 )); then
        report PASS "ipv6/${label}" "no global address, no default route, no on-link v6 prefix"
    else
        report FAIL "ipv6/${label}" \
            "IPv6 is live: ${addrs} global address(es), ${defroutes} default route(s), ${prefixes} on-link prefix(es)"
    fi
}

check_ipv6_host local
check_ipv6_host "$PIHOLE"
check_ipv6_host 192.168.10.10
check_ipv6_host 192.168.10.11

do_ip6=$("${SSH[@]}" "root@${PIHOLE}" \
    "awk '/^[[:space:]]*do-ip6:/ {print \$2; exit}' /etc/unbound/unbound.conf.d/pi-hole.conf" 2>/dev/null)
case "$do_ip6" in
    no)  report PASS "ipv6/unbound-do-ip6" "do-ip6: no" ;;
    "")  report FAIL "ipv6/unbound-do-ip6" "could not read do-ip6 from pi-hole.conf" ;;
    *)   report FAIL "ipv6/unbound-do-ip6" "do-ip6: ${do_ip6} - Unbound will resolve over IPv6" ;;
esac

records=$("${SSH[@]}" "root@${PIHOLE}" \
    "grep -h '^address=/' /etc/dnsmasq.d/*.conf | sort -u" 2>/dev/null)
if [[ -z $records ]]; then
    report FAIL "records/read" "could not read address= lines from ${PIHOLE}"
    printf 'TRIPWIRE FAIL - cannot reach the Pi-hole, nothing was verified\n'
    exit 1
fi

guards=$("${SSH[@]}" "root@${PIHOLE}" \
    "grep -hoP '(?<=local-zone: \")[^\"]+' /etc/unbound/unbound.conf.d/*.conf 2>/dev/null | sed 's/\\.$//'" 2>/dev/null)

is_local_zoned() {
    local n=$1 z
    while IFS= read -r z; do
        [[ -n $z ]] || continue
        [[ $n == "$z" || $n == *".${z}" ]] && return 0
    done <<<"$guards"
    return 1
}

"${SSH[@]}" "root@${PIHOLE}" "unbound-control flush_zone epaflix.com" >/dev/null 2>&1 \
    || report SKIP "cache/flush" "unbound-control flush_zone failed; AAAA results may be cached"

hairpin=0
while IFS= read -r line; do
    name=${line#address=/}
    ip=${name##*/}
    name=${name%%/*}

    [[ $name == *.epaflix.com ]] || continue
    if [[ $ip == "$PUBLIC_LB" ]]; then
        hairpin=$((hairpin + 1))
        continue
    fi

    aaaa_pihole=$(dig +short +time=3 +tries=1 AAAA "$name" "@${PIHOLE}" 2>/dev/null | tr '\n' ' ')
    aaaa_unbound=$("${SSH[@]}" "root@${PIHOLE}" \
        "dig +short +time=3 +tries=1 AAAA ${name} @127.0.0.1 -p 5335" 2>/dev/null | tr '\n' ' ')
    aaaa_pihole=${aaaa_pihole% }
    aaaa_unbound=${aaaa_unbound% }

    has_local_zone=0
    is_local_zoned "$name" && has_local_zone=1
    public_aaaa=$(dig +short +time=3 +tries=1 AAAA "$name" @1.1.1.1 2>/dev/null | tr '\n' ' ')
    has_exact_cf=0
    [[ -z ${public_aaaa// /} ]] && has_exact_cf=1

    if [[ -n $aaaa_pihole || -n $aaaa_unbound ]]; then
        report FAIL "aaaa/${name}" \
            "expected empty AAAA, got pihole=[${aaaa_pihole}] unbound=[${aaaa_unbound}] (A=${ip})"
    elif (( has_local_zone )); then
        report PASS "aaaa/${name}" "AAAA empty, guarded by Unbound local-zone (A=${ip})"
    elif (( has_exact_cf )); then
        report PASS "aaaa/${name}" "AAAA empty, guarded by exact DNS-only Cloudflare record (A=${ip})"
    else
        report FAIL "guard/${name}" \
            "A=${ip} is not ${PUBLIC_LB} and has NO guard - no covering local-zone in /etc/unbound/unbound.conf.d/ and the Cloudflare wildcard still answers AAAA [${public_aaaa}]. It answers empty AAAA on the LAN today by luck, not by design"
    fi
done <<<"$records"

report PASS "records/hairpin-count" \
    "${hairpin} name(s) on ${PUBLIC_LB} left unguarded on purpose - public Traefik route by design"

if (( fail == 0 )); then
    printf 'TRIPWIRE PASS - %d checks passed, %d skipped, 0 failed\n' "$pass" "$skip"
    exit 0
fi

printf 'TRIPWIRE FAIL - %d check(s) fired (%d passed, %d skipped)\n' "$fail" "$pass" "$skip"
for f in "${failures[@]}"; do
    printf '  - %s\n' "$f"
done
printf 'Next step: see "The non-192.168.10.101 names" in .github/instructions/pihole.instructions.md.\n'
printf 'Give each failing name an Unbound local-zone or an exact DNS-only Cloudflare record before enabling IPv6 (#882).\n'
exit 1
