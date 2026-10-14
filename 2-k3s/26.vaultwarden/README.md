# Vaultwarden

ArgoCD manages the server, Service, internal Traefik route, ingress
NetworkPolicy, 1Gi local-path PVC, recovery scripts and backup CronJob in
`vaultwarden`. The CNPG Database and DatabaseRole use `postgres-cluster` in
`postgres-system`. KSOPS decrypts the four SOPS-encrypted credentials.

The server runs one rootless writer with `Recreate` and pinned image digests.
`https://vaultwarden.epaflix.com` uses Traefik's internal entrypoint and wildcard
certificate. Pi-hole resolves it to `192.168.10.102`; Unbound blocks public AAAA
lookups. Registration and push are disabled. Invitations are enabled, and
`ORG_CREATION_USERS` restricts organization creation to the configured account.

ArgoCD self-heals with pruning disabled. It ignores the writer's replica count
and backup pause annotation so a sync cannot resume it during capture. The
namespace and PVC have prune and deletion protection. The four Secrets,
Database and DatabaseRole have deletion protection; CNPG also retains the
database and role.

## Backup

`vaultwarden-backup` runs daily at 02:30 UTC and is enabled. It pauses the writer,
waits for its pods to disappear, dumps PostgreSQL and copies all of `/data`. The
matched set records its UTC recovery point and exact server image. It resumes
the writer before uploading with restic to
`s3:https://minio.epaflix.com/vaultwarden-backups`. Sunday runs check repository
data before retaining 14 daily, 8 weekly and 12 monthly snapshots. Failed upload
or integrity checks skip retention.

Only one backup may run at a time. Do not start a manual Job while another is
active. A stale `vaultwarden.epaflix.com/backup-paused` annotation requires an
operator to check Jobs before resuming the writer. See [OPERATIONS.md](OPERATIONS.md)
for capture precautions.
The ConfigMap name is pinned to `vaultwarden-recovery-scripts-57ddc75dbc` because its suffix is not a content hash.

## Restore

Follow [OPERATIONS.md](OPERATIONS.md). Restore an explicit snapshot to a new
empty PVC and dedicated `vaultwarden_restore_*` database, never onto production.
Check account unlock and attachment bytes in an isolated server using the exact
recorded image. See [OPERATIONS.md](OPERATIONS.md) for storage failure risks and
whole-host recovery requirements.
