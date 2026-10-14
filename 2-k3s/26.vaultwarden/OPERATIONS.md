# Backup and restore operations

## A stopped writer

The backup Job scales the writer to zero and sets
`vaultwarden.epaflix.com/backup-paused`. Its exit trap normally resumes the
writer. Node loss, SIGKILL and forced deletion cannot run that trap.

Inspect Jobs and pods before intervening:

```sh
kubectl -n vaultwarden get jobs,pods
kubectl -n vaultwarden get deployment vaultwarden
```

Confirm no backup is still capturing data. Only then resume and remove the
marker:

```sh
kubectl -n vaultwarden scale deployment/vaultwarden --replicas=1
kubectl -n vaultwarden annotate deployment/vaultwarden vaultwarden.epaflix.com/backup-paused-
```

Do not run another backup or sync a new pod template during capture.

## Isolated restore

1. Choose a full 64-character restic snapshot ID, not `latest`. Read the set
   metadata privately to obtain its UTC recovery point and exact server image.
   Obtain the restic password and MinIO credentials through the approved secret
   channel. Do not put them in command arguments, logs or plaintext files.
2. Provision a new CNPG role and database named
   `vaultwarden_restore_<rehearsal>`. The role must own that database, have no
   superuser, create-role, create-database or replication privileges, and have
   no membership in another role. Use a new empty PVC. Never mount the
   production PVC or production database Secret in a restore pod.
3. Prepare an isolated restore pod using the pinned PostgreSQL, restic and
   kubectl images and tool-copy init containers from `backup.yaml`. Mount the
   new PVC at `/isolated-data`, a memory-backed work volume at `/work`,
   `vaultwarden-recovery-scripts-57ddc75dbc` at `/scripts`, and the restic
   password at `/run/restic/password`. Run as UID/GID 10001 with fsGroup 10001.
   Copy the CA trust bundle and set `SSL_CERT_FILE=/tools/ca-certificates.crt`.
4. Set `PGHOST=postgres-cluster-rw.postgres-system.svc`, `PGPORT=5432`,
   `PGDATABASE` and `PGUSER` to the new matching isolated name,
   `PGSSLMODE=require`, and `PGPASSWORD` from the isolated role's Secret. Set
   `RESTIC_REPOSITORY=s3:https://minio.epaflix.com/vaultwarden-backups`,
   `RESTIC_PASSWORD_FILE=/run/restic/password`, the protected AWS credential
   environment and `SERVER_IMAGE` to the exact recorded image. Do not use the
   production database credential. Set `HOME=/work` and
   `RESTIC_CACHE_DIR=/work/cache` to writable paths because the root filesystem
   is read-only.
5. Run the script in that pod:

   ```sh
   bash /scripts/restore-check.sh <full-snapshot-id>
   ```

   It checks database identity, ownership, privileges and emptiness, verifies
   the restic restore, imports without source ownership, and compares restored
   files byte-for-byte. A failed attempt leaves an isolated partial target.
   Discard it and provision a fresh empty database and PVC before retrying.
6. Privately inspect the isolated copy of `/data/config.json` before starting
   a server. File settings override environment. Remove production SMTP, push,
   domain and database overrides from that copy. Use only the isolated
   database and recorded image; disable SMTP and push, restrict ingress,
   block outbound mail/push and never attach production routing.
7. Verify account unlock, entry counts, representative item fields and every
   attachment's bytes using native clients. Script success alone does not
   prove the application works. Record the snapshot ID, UTC recovery point,
   duration and comparison counts. Remove the isolated resources after
   acceptance. Rehearse monthly with a new target.

MinIO is off the Kubernetes storage node but still shares the TrueNAS host
and storage failure domain. Whole-host disaster recovery needs an independent
repository copy, an off-host password and a tested restore from that copy.
