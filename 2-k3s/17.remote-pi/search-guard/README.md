# Search guard (epaflix#1038)

A tiny local proxy between the Pi harness web search and SearXNG that refuses
to hand over anything that is not JSON.

## Why

Pi's web search (pi-web-access, `searxngBaseUrl` in `web-search.json`) reaches
SearXNG unauthenticated only because Pi-hole resolves `searxng.epaflix.com`
to `192.168.10.102`, the traefik `internal` entrypoint. That dnsmasq record
is load-bearing. If resolution ever falls through to public DNS, the same
request lands on the Authentik-gated public route and the harness receives an
HTML 302 where it expects JSON. That failure used to surface as a generic
parse error or a quiet empty search, never as a broken route.

Owner decision on the issue (2026-08-21): make the failure loud. A 3xx or a
non-JSON content type from the search endpoint becomes a hard configuration
error naming the issue, instead of an empty result set. This guard is that
check, implemented consumer-side because the pi-web-access provider code
belongs to upstream and the harness config lives outside this repo.

## What it does

`search-guard.py` listens on `127.0.0.1:8893` and forwards `/search?...` to
`https://searxng.epaflix.com`. It does not follow redirects. The harness gets
results only when the upstream answers `200` with `application/json` and a
parseable body. Anything else (redirect, HTML, upstream error status,
unreachable upstream, captive-portal garbage) becomes an HTTP 502 whose body
starts with `epaflix#1038:` and names what was observed and what to check.
pi-web-access surfaces that text as the search error, so the break is loud
and points at the dnsmasq record.

The guard does not prevent the break; it names it. The dnsmasq record stays
load-bearing for the guard's own upstream fetch.

## Install

On the Pi, as the user that runs pi:

```bash
bash setup-search-guard.sh
```

The script is re-runnable and:

1. installs `search-guard.py` to `~/.local/bin` and a `systemd --user` unit
   (`search-guard.service`), enables it and turns on lingering so the guard
   survives logout;
2. repoints `searxngBaseUrl` to `http://127.0.0.1:8893` in every
   `web-search.json` it finds (`~/.pi/`, `~/.pi/agent/`, `~/.pi/profiles/*/`)
   and adds `127.0.0.1/32` to `ssrf.allowRanges`. The allowRange is required:
   pi-web-access's SSRF guard blocks loopback endpoints unless explicitly
   allowed, and it blocks the LAN address the old URL resolved to the same
   way, so the existing range entry stays;
3. verifies the guard with a live query.

Other keys in `web-search.json` are preserved.

## Verify

```bash
systemctl --user status search-guard
journalctl --user -u search-guard -n 20
curl -s "http://127.0.0.1:8893/search?q=test&format=json" | jq -r '.results|length'
```

## Rollback

Set `searxngBaseUrl` back to `https://searxng.epaflix.com` in the
`web-search.json` files, then `systemctl --user disable --now search-guard`.
Keep the `127.0.0.1/32` allowRange or drop it as preferred; nothing else
depends on it.

## Tests

`search-guard.test.py` runs offline (patched upstream, ephemeral port) and is
wired into CI:

```bash
python3 search-guard.test.py
```
