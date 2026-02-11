# Traefik v3.6.11 - Docker Swarm Stack

Traefik reverse proxy deployed as a Docker Swarm service on `ds-master`, providing:

- **Wildcard TLS** — Let's Encrypt `*.epaflix.com` via Cloudflare DNS-01 challenge
- **HTTP → HTTPS redirect** — at the entrypoint level (no per-app config needed)
- **Traefik dashboard** — at `traefik.epaflix.com`
- **Shared middlewares** — `redirect-https`, `security-headers`, `compress` available to all stacks
- **Pinned to `ds-master`** — `acme.json` is a bind mount on `192.168.10.71`

## Version note

Traefik v3 had a confirmed P0 bug ([#12253](https://github.com/traefik/traefik/issues/12253)) where its
internal Go Docker client hardcoded the API negotiation version to `1.24`. Docker 29+ dropped support
for API versions older than `1.40`, causing the Swarm provider to fail entirely.

This was **fixed in v3.6.6** and we are running **v3.6.11** (latest stable as of 2026-03-22).
The Swarm provider uses the dedicated `--providers.swarm` flags introduced in v3 (replacing the
old `--providers.docker.swarmMode=true` from v2).

---

## Prerequisites (run once)

### 1. Create the overlay network

All stacks that need Traefik to route to them must share this network.

```bash
ssh ubuntu@192.168.10.71 "docker network create --driver overlay --attachable traefik-public"
```

### 2. Create the Cloudflare API token secret

```bash
# Token value is in .github/instructions/secrets.yml
ssh ubuntu@192.168.10.71 "echo '<CF_API_TOKEN>' | docker secret create cloudflare_api_token -"
```

> The token needs DNS edit permissions for `epaflix.com` on Cloudflare.

### 3. Prepare the acme.json file on ds-master

```bash
ssh ubuntu@192.168.10.71 "
  sudo mkdir -p /opt/traefik &&
  sudo touch /opt/traefik/acme.json &&
  sudo chmod 600 /opt/traefik/acme.json &&
  sudo chown root:root /opt/traefik/acme.json &&
  ls -la /opt/traefik/acme.json
"
```

### 4. (Recommended) Seed acme.json from the existing K3s Traefik

The wildcard cert `*.epaflix.com` is already issued in K3s. Seeding it here avoids hitting
Let's Encrypt rate limits (max 5 duplicate certs per week) and means HTTPS works immediately
after deploy without waiting for a new DNS-01 challenge (~2 minutes).

```bash
# Extract acme.json from the running K3s Traefik pod
kubectl exec -n traefik-system deploy/traefik -- cat /data/acme.json > /tmp/acme.json

# Verify it contains the wildcard cert and key material
python3 -c "
import json
with open('/tmp/acme.json') as f:
    d = json.load(f)
for resolver, v in d.items():
    for c in (v.get('Certificates') or []):
        main = c['domain']['main']
        has_cert = bool(c.get('certificate'))
        has_key = bool(c.get('key'))
        print(f'{main}: cert={has_cert}, key={has_key}')
"

# Copy to ds-master and place with correct permissions
scp /tmp/acme.json ubuntu@192.168.10.71:/tmp/acme.json
ssh ubuntu@192.168.10.71 "
  sudo cp /tmp/acme.json /opt/traefik/acme.json &&
  sudo chmod 600 /opt/traefik/acme.json &&
  sudo chown root:root /opt/traefik/acme.json &&
  rm /tmp/acme.json &&
  ls -la /opt/traefik/acme.json
"

# Clean up local copy
rm /tmp/acme.json
```

---

## Deploy

```bash
# Copy compose file to ds-master and deploy
scp 3-docker-swarm/stacks/traefik/docker-compose.yml ubuntu@192.168.10.71:/tmp/traefik-compose.yml
ssh ubuntu@192.168.10.71 "docker stack deploy -c /tmp/traefik-compose.yml traefik && rm /tmp/traefik-compose.yml"
```

### Verify

```bash
# Check service is placed and running on ds-master
ssh ubuntu@192.168.10.71 "docker service ps traefik_traefik"

# Check logs — look for certificate loading and provider startup
ssh ubuntu@192.168.10.71 "docker service logs traefik_traefik --tail 50 2>&1"

# Confirm ports 80 and 443 are bound on ds-master
ssh ubuntu@192.168.10.71 "ss -tlnp | grep -E ':80|:443'"

# Test HTTP → HTTPS redirect (should return 301)
curl -sI http://traefik.epaflix.com | grep -E 'HTTP|Location'

# Test HTTPS dashboard is reachable
curl -sk https://traefik.epaflix.com/dashboard/ | head -5

# Check the loaded TLS certificates via Traefik API
curl -sk https://traefik.epaflix.com/api/overview | python3 -m json.tool
```

---

## Update / Redeploy

```bash
# Re-deploy with latest config (or after image tag change)
scp docker-compose.yml ubuntu@192.168.10.71:/tmp/traefik-compose.yml
ssh ubuntu@192.168.10.71 "docker stack deploy -c /tmp/traefik-compose.yml traefik && rm /tmp/traefik-compose.yml"
```

> Because ports are in `host` mode with `order: stop-first`, Traefik will stop before the new
> container starts. There will be a brief period (~5s) of downtime during updates. This is
> acceptable for a single entry-point — the alternative would be to use an NFS-mounted
> `acme.json` and allow Traefik to float across nodes.

## Remove

```bash
ssh ubuntu@192.168.10.71 "docker stack rm traefik"
```

> Note: The `traefik-public` network, the `cloudflare_api_token` secret, and `/opt/traefik/acme.json`
> are **not** removed automatically. Re-deploying will reuse them.

---

## How Other Stacks Use Traefik

Any service that needs a domain just needs to:

1. Join the `traefik-public` network
2. Add `traefik.enable=true` and routing labels under `deploy.labels`

### Minimal example (HTTP only — Traefik auto-upgrades to HTTPS via entrypoint redirect)

```yaml
networks:
  traefik-public:
    external: true

services:
  myapp:
    image: myapp:latest
    networks:
      - traefik-public
    deploy:
      labels:
        - "traefik.enable=true"
        - "traefik.http.routers.myapp.rule=Host(`myapp.epaflix.com`)"
        - "traefik.http.routers.myapp.entrypoints=websecure"
        - "traefik.http.routers.myapp.tls=true"
        - "traefik.http.services.myapp.loadbalancer.server.port=8080"
```

### Full example with security headers and compression

```yaml
deploy:
  labels:
    - "traefik.enable=true"

    # HTTPS router
    - "traefik.http.routers.myapp.rule=Host(`myapp.epaflix.com`)"
    - "traefik.http.routers.myapp.entrypoints=websecure"
    - "traefik.http.routers.myapp.tls=true"
    - "traefik.http.routers.myapp.middlewares=security-headers@swarm,compress@swarm"

    # Target port inside the container
    - "traefik.http.services.myapp.loadbalancer.server.port=8080"
```

### Key rules for Swarm labels

| Rule | Reason |
|------|--------|
| Labels go under `deploy.labels`, **not** top-level `labels` | Swarm mode only reads `deploy.labels` for routing |
| Always set `traefik.http.services.<name>.loadbalancer.server.port` | Swarm doesn't auto-detect ports the way standalone Docker does |
| Always attach the service to `traefik-public` network | Traefik can only reach containers on a shared network |
| Reference shared middlewares as `<name>@swarm` | `@swarm` tells Traefik the middleware is defined via Swarm service labels |

---

## Available Shared Middlewares

These are defined on the Traefik service itself and available to all stacks:

| Middleware | Reference | Effect |
|---|---|---|
| `redirect-https` | `redirect-https@swarm` | Redirects HTTP → HTTPS (301) |
| `security-headers` | `security-headers@swarm` | HSTS, XSS filter, content-type sniff, noindex robots |
| `compress` | `compress@swarm` | Gzip/brotli response compression |

---

## Architecture Notes

### Why `host` port mode instead of `ingress` (routing mesh)?

Traefik is pinned to `ds-master` with `replicas: 1`. Using `mode: host` on ports 80/443 means:

- The real client IP is preserved in `X-Forwarded-For` headers (important for Traefik access logs and rate limiting)
- No IPVS/iptables routing mesh overhead
- Direct bind — if Traefik is down, the port is simply not listening (cleaner failure mode)

The tradeoff is that traffic **must** enter via `192.168.10.71`. Your router port forward and
Pi-hole DNS both point there, so this is fine.

### Why pinned to ds-master?

`acme.json` is stored at `/opt/traefik/acme.json` on `ds-master`. Let's Encrypt certificates
and private keys live in this file. It must be on a fixed node. If you want Traefik to float
across nodes in future, the solution is to use an NFS bind mount for `/opt/traefik/` instead.

### Certificate renewal

Traefik handles renewal automatically — it checks cert expiry every 24 hours and renews when
less than 30 days remain. The Cloudflare DNS-01 challenge creates a `_acme-challenge` TXT record,
waits 30 seconds for propagation, then validates with Let's Encrypt.

---

## Troubleshooting

### Port 80/443 not binding on ds-master

```bash
# Check the service is actually placed on ds-master
docker service ps traefik_traefik

# Check logs for bind errors
docker service logs traefik_traefik 2>&1 | grep -i "error\|bind\|listen"

# Manually check ports
ssh ubuntu@192.168.10.71 "ss -tlnp | grep -E ':80|:443'"
```

### Certificate not issuing / renewing

```bash
# Watch Traefik logs for ACME activity
docker service logs traefik_traefik -f 2>&1 | grep -i "acme\|cloudflare\|certificate\|error"

# Check acme.json is not empty and has correct permissions
ssh ubuntu@192.168.10.71 "ls -la /opt/traefik/acme.json && wc -c /opt/traefik/acme.json"
```

### 404 on traefik.epaflix.com

```bash
# Confirm dashboard router is active
curl -sk https://192.168.10.71/dashboard/ -H "Host: traefik.epaflix.com" | head -5

# Inspect Traefik's known routers via API
curl -sk https://traefik.epaflix.com/api/http/routers | python3 -m json.tool | grep '"name"'
```

### App not routing (502 / no rule matched)

```bash
# Check Traefik can see the service
curl -sk https://traefik.epaflix.com/api/http/services | python3 -m json.tool | grep '"name"'

# Confirm the app container is on the traefik-public network
docker service inspect <stack>_<service> | grep -A5 Networks
```

---



## References

- [Traefik Docker Swarm Provider (v3)](https://doc.traefik.io/traefik/reference/install-configuration/providers/swarm/)
- [Traefik ACME / Let's Encrypt](https://doc.traefik.io/traefik/https/acme/)
- [Traefik Cloudflare DNS Challenge](https://doc.traefik.io/traefik/https/acme/#providers)
- [Traefik Middlewares](https://doc.traefik.io/traefik/middlewares/overview/)
- [Bug: client version 1.24 too old (v3 + Docker 29)](https://github.com/traefik/traefik/issues/12253)