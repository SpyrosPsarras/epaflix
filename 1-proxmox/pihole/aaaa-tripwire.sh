#!/usr/bin/env bash
#
# IPv6 / AAAA tripwire for the *.epaflix.com zone.  Issue #882, follows #868.
#
# Why this exists
# ---------------
# `address=/name/<IPv4>` in /etc/dnsmasq.d/ makes Pi-hole authoritative for the
# A record only.  An AAAA query for the same name goes to Unbound, out to public
# DNS, and the Cloudflare-proxied `*.epaflix.com` wildcard synthesizes an AAAA.
# An IPv6 client on the LAN would then be sent to Cloudflare instead of the LAN
# box.  That is harmless today only because nothing on 192.168.10.0/24 has a
# global IPv6 address.  The day one does, Happy Eyeballs prefers v6, so the leak
# becomes the *preferred* path, not a fallback.
#
# This script is the mechanical version of that sentence.  It FAILS when:
#   1. any checked host grows a global IPv6 address, an IPv6 default route, or a
#      router-advertised on-link IPv6 prefix, or Unbound flips to `do-ip6: yes`;
#   2. any guarded name stops answering an EMPTY AAAA;
#   3. a non-192.168.10.101 `*.epaflix.com` record exists with no guard at all,
#      which is how a new record silently joins the leak.
#
# Check 3 is the important one.  The guarded list is derived from the live
# `address=` lines every run, so it cannot go stale behind a new record.
#
# Usage
# -----
#   ./aaaa-tripwire.sh            # human-readable, exit 0 pass / 1 fail
#   ./aaaa-tripwire.sh -q         # summary line only
#   PIHOLE=192.168.10.30 ./aaaa-tripwire.sh
#
# Run it before enabling IPv6 anywhere, after adding or moving any
# *.epaflix.com record, and after editing no-aaaa-leak.conf.  It is read-only
# except for one `unbound-control flush_zone epaflix.com`, which is required:
# Unbound runs cache-max-ttl 14400 with serve-expired, so without the flush a
# name that leaked before a fix keeps answering from cache and the check goes
# green on stale data.
#
# Requires: ssh root@$PIHOLE (passwordless), dig on this machine.

set -uo pipefail

PIHOLE="${PIHOLE:-192.168.10.30}"
# Names on this address have a public Traefik route by design, so a Cloudflare
# AAAA for them is a hairpin rather than a boundary break.  They are excluded
# from the AAAA checks and reported as a count.
PUBLIC_LB="${PUBLIC_LB:-192.168.10.101}"
# -n matters: without it ssh reads the `while read` loop's stdin and the record
# loop silently stops after one iteration.
SSH=(ssh -n -o ConnectTimeout=8 -o BatchMode=yes)
QUIET=0
[[ "${1:-}" == "-q" ]] && QUIET=1

pass=0
fail=0
skip=0
declare -a failures=()

# report <PASS|FAIL|SKIP> <check-id> <detail>
report() {
    local verdict=$1 id=$2 detail=$3
    case $verdict in
        PASS) pass=$((pass + 1)) ;;
        FAIL) fail=$((fail + 1)); failures+=("${id}: ${detail}") ;;
        SKIP) skip=$((skip + 1)) ;;
    esac
    (( QUIET )) || printf '%-4s  %-42s  %s\n' "$verdict" "$id" "$detail"
}

# ---------------------------------------------------------------------------
# Check 1 - is there IPv6 on the LAN yet?
# ---------------------------------------------------------------------------
# Global addresses and default routes are the two things that make a client
# choose IPv6 for an off-LAN destination.  The on-link prefix route catches a
# router that has started advertising IPv6 before any host has taken an
# address, which is the earliest visible moment.
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

# Unbound's own switch.  `do-ip6: yes` alone does not create a leak, but it is
# the config change that always accompanies turning IPv6 on, so it belongs here.
do_ip6=$("${SSH[@]}" "root@${PIHOLE}" \
    "awk '/^[[:space:]]*do-ip6:/ {print \$2; exit}' /etc/unbound/unbound.conf.d/pi-hole.conf" 2>/dev/null)
case "$do_ip6" in
    no)  report PASS "ipv6/unbound-do-ip6" "do-ip6: no" ;;
    "")  report FAIL "ipv6/unbound-do-ip6" "could not read do-ip6 from pi-hole.conf" ;;
    *)   report FAIL "ipv6/unbound-do-ip6" "do-ip6: ${do_ip6} - Unbound will resolve over IPv6" ;;
esac

# ---------------------------------------------------------------------------
# Check 2 and 3 - derive the guarded list from the live box, then test it
# ---------------------------------------------------------------------------
records=$("${SSH[@]}" "root@${PIHOLE}" \
    "grep -h '^address=/' /etc/dnsmasq.d/*.conf | sort -u" 2>/dev/null)
if [[ -z $records ]]; then
    report FAIL "records/read" "could not read address= lines from ${PIHOLE}"
    printf 'TRIPWIRE FAIL - cannot reach the Pi-hole, nothing was verified\n'
    exit 1
fi

# Every local-zone on the box, not just no-aaaa-leak.conf.  vm-epaflix.conf holds
# the zone-wide `local-zone: "vm.epaflix.com." static`, which guards every name
# under it, so a per-file read would report the two user VMs as unguarded.
guards=$("${SSH[@]}" "root@${PIHOLE}" \
    "grep -hoP '(?<=local-zone: \")[^\"]+' /etc/unbound/unbound.conf.d/*.conf 2>/dev/null | sed 's/\\.$//'" 2>/dev/null)

# A local-zone covers the name itself and everything below it.
is_local_zoned() {
    local n=$1 z
    while IFS= read -r z; do
        [[ -n $z ]] || continue
        [[ $n == "$z" || $n == *".${z}" ]] && return 0
    done <<<"$guards"
    return 1
}

# Flush so a pre-fix cached AAAA cannot produce a false green.
"${SSH[@]}" "root@${PIHOLE}" "unbound-control flush_zone epaflix.com" >/dev/null 2>&1 \
    || report SKIP "cache/flush" "unbound-control flush_zone failed; AAAA results may be cached"

hairpin=0
while IFS= read -r line; do
    name=${line#address=/}
    ip=${name##*/}
    name=${name%%/*}

    # Only *.epaflix.com can hit the Cloudflare wildcard.  Bare hostnames have
    # no TLD and *.epaflix.lan is not a real public TLD, so neither can leak.
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

    # Two valid guards, one each side of the boundary:
    #
    #  * an Unbound `local-zone`, which makes the LAN answer empty; or
    #  * an exact DNS-only Cloudflare record, which takes the name out of the
    #    `*.epaflix.com` wildcard so the wildcard stops synthesizing an AAAA.
    #
    # The Cloudflare side is only observable from a public resolver, and the test
    # is the PUBLIC AAAA, not the public A.  Comparing the public A to the local
    # IP does not work: wg-hop's exact record points at the router public address
    # 81.167.233.67 while its `address=` line points at 192.168.10.45.  An empty
    # public AAAA means the wildcard no longer covers the name, which is the whole
    # mechanism.
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

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
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
