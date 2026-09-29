# deltasync

Self-hosted [DeltaSync](https://gitlab.com/Star95/keepass-deltasync) server.
It syncs the KeePass vault per entry between devices. It stores only
client-encrypted blobs; state lives in the shared CNPG cluster.

- Devices: `https://deltasync.epaflix.com`, Traefik `internal` entry point
  only (LAN and Tailscale). The Pi-hole record is in
  `1-proxmox/pihole/dnsmasq.d/10-epaflix.conf` plus the matching
  `local-zone` in `1-proxmox/pihole/unbound-no-aaaa-leak.conf`. Argo CD does
  not deploy these: copy them to the Pi-hole by hand, as
  `1-proxmox/pihole/README.md` describes. Until then the name does not
  resolve on the LAN.
- In-cluster (keepass MCP): `http://deltasync.deltasync.svc` (port 80).
- Health: `GET /api/v1/health` (200 = app and DB up). The kubelet probes and
  the blackbox target `deltasync` use it.

## Files

| File | What |
|---|---|
| `database.yaml` | CNPG `DatabaseRole` + `Database` `deltasync` on `postgres-cluster` (ns `postgres-system`) |
| `server.yaml` | Deployment (1 replica, `Recreate`) and Service |
| `ingress.yaml` | IngressRoute on `internal` |
| `deltasync-secrets.enc.yaml` | ns `deltasync`: `db-password`, `admin-password`, `admin-token` |
| `deltasync-db-role.enc.yaml` | ns `postgres-system`: `username`/`password` for the `DatabaseRole`, label `cnpg.io/reload` |

The image tag sits in `kustomization.yaml` `images:`. Renovate opens a PR for
new tags; minor and major bumps need review (`.github/renovate.json`).

## Admin login

The admin login exists only in sops, in `deltasync-secrets.enc.yaml`:
username `admin`, password `admin-password`, API token `admin-token`.
Read it with:

```bash
sops --disable-version-check -d 2-k3s/25.deltasync/deltasync-secrets.enc.yaml
```

The entrypoint registers both at every start and logs only their lengths
(`kubectl -n deltasync logs deploy/deltasync | grep 'admin account'`).

## Users and devices

```bash
kubectl -n deltasync exec deploy/deltasync -- php bin/admin user:create spyros
```

Enrollment tokens for new devices come from the admin panel,
`https://deltasync.epaflix.com/admin.html`.

## Security context

Facts from the upstream `server/Dockerfile` and `server/docker-entrypoint.sh`
(read at upstream commit a994d06 on main, not at the `0.5.0` tag):

- The image is `php:8.2-apache` with no `USER` line. The entrypoint runs as
  root, runs the migrations, then execs `apache2-foreground`, which binds :80
  as root and serves requests from `www-data` workers.
- So the pod cannot use `runAsNonRoot`, and dropping all capabilities would
  stop Apache from binding :80 and switching user. What it does set:
  `allowPrivilegeEscalation: false` and `seccompProfile: RuntimeDefault`.
- The admin secret files are mounted with mode `0400`. Only the root
  entrypoint reads `ADMIN_PASSWORD_FILE` and `ADMIN_TOKEN_FILE`; it passes the
  password to `php bin/admin admin:ensure` through the environment. The PHP
  code never opens those files, so `www-data` does not need them.

`TRUSTED_PROXIES` is the k3s pod CIDR `10.42.0.0/16`, where Traefik runs, so
the session cookie gets `Secure` and the audit log records real client IPs.

## Render locally

`ksops` is needed for the full render:

```bash
kustomize build --enable-alpha-plugins --enable-exec 2-k3s/25.deltasync
```
