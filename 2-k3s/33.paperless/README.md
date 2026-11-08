# Paperless-ngx

Receipts archive with OCR. Design: `docs/superpowers/specs/2026-11-08-receipts-design.md`.

Hostname https://paperless.epaflix.com on Traefik `websecure` (192.168.10.101), so phones can upload from the internet. The pod runs Paperless on port 8000 and Valkey on `127.0.0.1:6379` with `emptyDir` storage. The database is `paperless` in `postgres-cluster`.

## Access

- Login goes through the Authentik OAuth2 provider `paperless` with django-allauth `openid_connect`, provider id `authentik`. Authentik forward auth does not sit in front of Paperless.
- The Authentik application `paperless` binds `authentik Admins` and `Servarr users`. Authentik denies everyone else, so Paperless never creates an account for them.
- Paperless creates the account on the first SSO login. Password login in the web UI is off and the login page redirects to Authentik.
- Traefik answers 403 to every request for `/admin/`. `PAPERLESS_DISABLE_REGULAR_LOGIN` leaves the Django admin login open, so the route stays blocked.
- Phones use an API token. The user logs in to the web UI once, creates the token under My Profile and pastes it into Paperless Go.
- After the owner's first login, an owner-confirmed `manage.py shell` sets `is_superuser` and `is_staff` on his user.

### Removing a user

Removing someone from `Servarr users` stops new web logins only. Their API token keeps working. In Paperless, open Users & Groups, edit the user and clear Active. That blocks the web session and the API token.

## Storage

PV `paperless-data-shypowder` mounts NFS `192.168.10.13:/hdd/paperless` (ZFS dataset `hdd/paperless` on shypowder, see `1-proxmox/shypowder/README.md`). The export squashes every client to 1000:1000, which matches `USERMAP_UID`/`USERMAP_GID`. `PAPERLESS_DATA_DIR` is `data/` and `PAPERLESS_MEDIA_ROOT` is `media/` on that volume. The consume folder is `emptyDir`, because uploads through the API and web UI skip it.

Files are backed up only by sanoid snapshots on shypowder, one disk. The database uses the CNPG backups to MinIO.

### Move to TrueNAS

When the 6×14 TB pool exists on TrueNAS:

1. Scale Paperless (and paperless-gpt) to zero.
2. `syncoid hdd/paperless` to a dataset on the new pool, snapshots included.
3. Export that dataset over NFS from TrueNAS to the k3s workers, squashed to 1000:1000.
4. In a PR, replace the PV with one pointing at TrueNAS, then scale up.
5. After a check, stop the shypowder export. Deleting `hdd/paperless` needs the owner's approval.

A PV's NFS source cannot change in place. Argo must delete and recreate the PV and PVC, with Paperless at zero.

## Secrets

`paperless-secrets.enc.yaml` holds Secret `paperless-db-role` in `postgres-system` and Secret `paperless-secrets` in `paperless`, both at sync wave `-2`. `paperless-secrets` has `PAPERLESS_SECRET_KEY`, `PAPERLESS_DBPASS` and `PAPERLESS_SOCIALACCOUNT_PROVIDERS`.

The values were generated at random when the files were encrypted and exist only in the SOPS files. No vault item holds them. The OIDC client id and secret appear twice: in `PAPERLESS_SOCIALACCOUNT_PROVIDERS` and in provider `paperless` of `2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml`. Rotate them in both files in the same PR. The database password appears in both Secrets of `paperless-secrets.enc.yaml`.

## DNS

Public DNS resolves through the Cloudflare wildcard and tunnel. The LAN needs `paperless.epaflix.com` in `1-proxmox/pihole/dnsmasq.d/10-epaflix.conf` (A `192.168.10.101`) and in `1-proxmox/pihole/unbound-no-aaaa-leak.conf`. Apply them on the Pi-hole the way `2-k3s/08.servarr/navidrome/README.md` records.

## Observability

- `PaperlessDown` fires when the Deployment has no available replica for 15 minutes or is absent.
- Blackbox target `paperless` probes `https://192.168.10.101` with host `paperless.epaflix.com`.
- Logs reach Loki through Alloy.
