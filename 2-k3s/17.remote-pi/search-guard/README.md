# Search guard (epaflix#1038)

A guard between the Pi harness web search (pi-web-access) and SearXNG that
refuses to hand over anything that is not JSON.

## Why

The harness reaches SearXNG unauthenticated only while Pi-hole resolves
`searxng.epaflix.com` to `192.168.10.102`, the traefik `internal` entrypoint.
That dnsmasq record is load-bearing. If resolution ever falls through to
public DNS, the same request lands on the Authentik-gated public route and
the harness receives an HTML 302 where it expects JSON. That failure used to
surface as a generic parse error or a quiet empty search, never as a broken
route.

Owner decision on the issue (2026-08-21): make the failure loud. A 3xx or a
non-JSON content type from the search endpoint becomes a hard configuration
error naming the issue, instead of an empty result set. This guard is that
check, consumer-side, because the pi-web-access provider code belongs to
upstream and the harness config lives outside this repo.

## Production path: in-cluster (deployed)

`deployment.yaml` + `service.yaml` + the `search-guard-scripts` ConfigMap run
the guard as `search-guard.remote-pi` behind a pinned LoadBalancer IP:

```
http://192.168.10.115:8893
```

The IP is pinned because `web-search.json` points the harness at it and the
SSRF `allowRanges` entry names it; change the service, the config and the
allowRange together. The pod fetches `https://searxng.epaflix.com` itself, so
the dnsmasq record stays load-bearing for the guard's own upstream fetch - the
guard makes its breakage loud, it does not prevent it.

ArgoCD (`app-remote-pi`) owns this deployment. `web-search.json` rides the
`.pi` Syncthing folder (`dygdz-zpjsp`) to the machines that run the harness;
it carries `192.168.10.115/32` in `ssrf.allowRanges` because pi-web-access's
SSRF guard blocks private endpoints unless explicitly allowed.

## Alternative: localhost install on the harness machine

`setup-search-guard.sh` (run as the pi user, re-runnable) installs the same
guard as a `systemd --user` unit on `127.0.0.1:8893`, repoints
`searxngBaseUrl` in every `web-search.json` it finds (`~/.pi/`, `~/.pi/agent/`,
`~/.pi/profiles/*/`) preserving other keys, adds `127.0.0.1/32` to
`ssrf.allowRanges`, and verifies with a live query. Use it where the harness
machine cannot reach the cluster IP.

## Failure mode

The harness gets results only when the upstream answers `200` with
`application/json` and a parseable body. Anything else (redirect, HTML,
upstream error status, unreachable upstream, captive-portal garbage) becomes
an HTTP 502 whose body starts with `epaflix#1038:` and names what was
observed and what to check. pi-web-access surfaces that text as the search
error.

## Verify

```bash
curl -s "http://192.168.10.115:8893/search?q=test&format=json" | jq -r '.results|length'
kubectl logs -n remote-pi deploy/search-guard --tail=20
```

Rollback: set `searxngBaseUrl` back to `https://searxng.epaflix.com` in
`web-search.json`; the guard can keep running harmlessly.

## Tests

`search-guard.test.py` runs offline (patched upstream, ephemeral port) and is
wired into CI:

```bash
python3 search-guard.test.py
```
