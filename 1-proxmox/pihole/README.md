`dnsmasq.d/` tracks the live `/etc/dnsmasq.d/` on CT 1030 (pihole.epaflix.com): copy the files there and run `systemctl restart pihole-FTL` to apply (about two seconds without LAN DNS).

`pihole reloaddns` only flushes the cache: dnsmasq reads `address=` lines at startup, not on reload. `pihole restartdns` on v6.4.2 fails silently (`utils.sh: FTL_PID_FILE: readonly variable`). Check that it took with `ps -o lstart= -C pihole-FTL` and `dig +short @127.0.0.1 <name>`.

`unbound-no-aaaa-leak.conf` lists the `local-zone` lines of the live `/etc/unbound/unbound.conf.d/no-aaaa-leak.conf`. The live file also carries the rationale comments; add lines there rather than copying this file over it. Apply with `unbound-checkconf /etc/unbound/unbound.conf && unbound-control reload`. Add the local-zone together with the dnsmasq line: a static zone without the dnsmasq `address=` answers NXDOMAIN for A as well.
