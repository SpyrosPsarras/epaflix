# Silo on TrueNAS

## Why Silo

MinIO upstream is archived. Silo is a maintained MinIO fork. Garage was rejected because of single-node data loss in [#1224](https://git.deuxfleurs.fr/Deuxfleurs/garage/issues/1224) and unreadable restic objects in [#1230](https://git.deuxfleurs.fr/Deuxfleurs/garage/issues/1230).
Silo keeps the S3 API, `.minio.sys` data format and `MINIO_*` settings. See [compatibility and migration](https://silo.pgsty.com/compatibility/migration/).

## What runs

TrueNAS custom app `silo` uses [docker-compose.yaml](docker-compose.yaml). The image is pinned by tag and digest there.
Dataset `pool1/minio`, at host path `/mnt/pool1/minio`, is bind-mounted into the container and uses on-disk format `xl-single`.
CNPG barman-cloud ObjectStore `postgres-minio-store`, Vaultwarden restic backups and Fytorium all use `minio.epaflix.com` through Traefik's internal entry point.

## Credentials

Silo reads `/mnt/apps/silo/silo.env` on TrueNAS, owned by `root:root`, mode `0600`, directory mode `0700`.
The stopped catalog app `minio` still holds the root credentials in its app config, which is the source they were copied from.

```sh
sudo install -d -m 0700 -o root -g root /mnt/apps/silo
sudo sh -c 'umask 077; midclt call app.config minio | jq -r ".minio.credentials | \"MINIO_ROOT_USER=\(.access_key)\nMINIO_ROOT_PASSWORD=\(.secret_key)\"" > /mnt/apps/silo/silo.env'
```

Add `MINIO_SERVER_URL` and `MINIO_BROWSER_REDIRECT_URL` lines with the values of SOPS keys `minio_api_endpoint` and `minio_console_endpoint`, respectively, using a root editor such as `sudo nano /mnt/apps/silo/silo.env`, not `echo` or shell tracing.
Never print `app.config minio` output or the file, use shell tracing or commit the file.

## Deploy or update

Renovate's docker-compose manager updates the image tag and digest in this compose. The compose is applied to TrueNAS by hand; a merged Renovate PR does not deploy it. Snapshot first, then `app.update`.
Run `docker`, `midclt` and `zfs` commands with `sudo` in a TrueNAS shell.
`pool1/minio@pre-silo-2026-10-25` is the snapshot taken before the MinIO to Silo cutover.
For later upgrades, run `sudo zfs snapshot pool1/minio@pre-<tag>`, replacing `<tag>` with the target tag.
Create uses `{"app_name": "silo", "custom_app": true, "custom_compose_config_string": "<compose>"}`. With this compose file in the shell's current directory:

```sh
sudo midclt call -j app.create "$(jq -n --rawfile compose docker-compose.yaml '{app_name: "silo", custom_app: true, custom_compose_config_string: $compose}')"
sudo midclt call -j app.update silo "$(jq -n --rawfile compose docker-compose.yaml '{custom_compose_config_string: $compose}')"
```
Choose create for a new app or update for the existing app. Do not run MinIO and Silo against the dataset at the same time.

## Checks after any change

Run these only with approval to test the live clients. Check health in the TrueNAS shell:

```sh
sudo docker exec ix-silo-silo-1 silo healthcheck ready
```

Run all `kubectl` commands on a cluster client. Set `PG_NAMESPACE`, `PG_PRIMARY`, `PG_CLUSTER`, `FYTORIUM_NAMESPACE` and `FYTORIUM_POD` to the current resources.
Record `failed_count`, switch WAL, then repeat the archiver query until the new WAL is archived without increasing `failed_count`:

```sh
kubectl -n "$PG_NAMESPACE" exec "$PG_PRIMARY" -c postgres -- psql -U postgres -c 'SELECT archived_count, last_archived_wal, failed_count FROM pg_stat_archiver;'
kubectl -n "$PG_NAMESPACE" exec "$PG_PRIMARY" -c postgres -- psql -U postgres -c 'SELECT pg_switch_wal();'
kubectl -n "$PG_NAMESPACE" exec "$PG_PRIMARY" -c postgres -- psql -U postgres -c 'SELECT archived_count, last_archived_wal, failed_count FROM pg_stat_archiver;'
kubectl -n "$PG_NAMESPACE" get cluster "$PG_CLUSTER" -o jsonpath='{range .status.conditions[*]}{.type}={.status}{"\n"}{end}'
BACKUP="silo-check-$(date -u +%Y%m%d%H%M%S)"
cat <<EOF | kubectl -n "$PG_NAMESPACE" create -f -
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata:
  name: $BACKUP
spec:
  cluster:
    name: $PG_CLUSTER
  method: plugin
  pluginConfiguration:
    name: barman-cloud.cloudnative-pg.io
EOF
kubectl -n "$PG_NAMESPACE" get backup "$BACKUP" -w
```

Require `ContinuousArchiving=True` and the Backup phase to reach `completed`. On the cluster client, create a one-off Pod in namespace `vaultwarden` using the `restic/restic` image pin from the `images:` entry in [kustomization.yaml](../../../2-k3s/26.vaultwarden/kustomization.yaml). Use [backup.yaml](../../../2-k3s/26.vaultwarden/backup.yaml) for the env and Secret layout.
Set `RESTIC_REPOSITORY=s3:https://minio.epaflix.com/vaultwarden-backups` and `RESTIC_PASSWORD_FILE=/run/restic/password`. Mount Secret `vaultwarden-backup` key `password` at that path read-only.
Set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` through `secretKeyRef` to that Secret's keys `access-key` and `secret-key`. Run `restic check --read-data` in the Pod without printing secret values.
Require `no errors were found`. Do not manually run the Vaultwarden CronJob on Sundays just to test: its Sunday `forget --prune` can delete snapshots.
Run the Fytorium check inside its pod using the app's installed `minio` client and credentials. It writes a unique 200 KB object, compares bytes, removes it and requires `NotFound`:

```sh
kubectl -n "$FYTORIUM_NAMESPACE" exec -i "$FYTORIUM_POD" -- node --input-type=module <<'JS'
import assert from 'node:assert/strict';
import { randomBytes, randomUUID } from 'node:crypto';
import { Client } from 'minio';
const url = new URL(process.env.MINIO_ENDPOINT);
const client = new Client({ endPoint: url.hostname, port: Number(url.port || (url.protocol === 'https:' ? 443 : 80)), useSSL: url.protocol === 'https:', accessKey: process.env.MINIO_ACCESS_KEY, secretKey: process.env.MINIO_SECRET_KEY });
const bucket = process.env.MINIO_BUCKET, key = `silo-check-${randomUUID()}`, data = randomBytes(200000);
await client.putObject(bucket, key, data);
const chunks = [];
for await (const chunk of await client.getObject(bucket, key)) chunks.push(chunk);
assert.deepEqual(Buffer.concat(chunks), data);
await client.removeObjects(bucket, [key]);
await assert.rejects(client.statObject(bucket, key), error => error.code === 'NotFound');
JS
```

## Rollback

In the TrueNAS shell, first list snapshots with `sudo zfs list -t snapshot -o name,creation -s creation pool1/minio`.
Stop `silo` with `sudo midclt call -j app.stop silo`.
If no snapshot is newer than `pool1/minio@pre-silo-2026-10-25`, run `sudo zfs rollback pool1/minio@pre-silo-2026-10-25`.
If newer snapshots exist, plain rollback refuses to run. Use `sudo zfs rollback -r pool1/minio@pre-silo-2026-10-25` only after owner approval to destroy all listed newer snapshots.
After rollback, start `minio` with `sudo midclt call -j app.start minio`.
Rollback drops all writes made after the snapshot. For a later upgrade, use its pre-upgrade snapshot and matching previous image/configuration.
While Silo runs, the catalog app `minio` stays installed and stopped for rollback until the owner removes it.
