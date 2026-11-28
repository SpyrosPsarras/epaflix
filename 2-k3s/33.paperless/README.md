# Paperless-ngx

Receipts archive with OCR. Design: `docs/superpowers/specs/2026-11-08-receipts-design.md`.

Hostname https://paperless.epaflix.com on Traefik `websecure` at `192.168.10.101`, so phones can upload from the internet. The pod runs Paperless on port 8000 and Valkey on `127.0.0.1:6379` with `emptyDir` storage. Valkey is a native sidecar, so it answers before Paperless starts. Without that, Paperless gave up after a few Redis pings and the Paperless container restarted once on every pod start. The database is `paperless` in `postgres-cluster`.

## Access

- Login goes through the Authentik OAuth2 provider `paperless` with django-allauth `openid_connect`, provider id `authentik`. Authentik forward auth does not sit in front of Paperless.
- The Authentik application `paperless` binds `authentik Admins` and `Servarr users`. Authentik denies everyone else, so Paperless never creates an account for them.
- Paperless creates the account on the first SSO login and adds it to group `receipts` through `PAPERLESS_SOCIAL_ACCOUNT_DEFAULT_GROUPS`. Password login in the web UI is off and the login page redirects to Authentik.
- The REST API still accepts basic auth. No user has a usable password, so do not set one with `manage.py`.
- Traefik answers 403 to every request for `/admin/`. `PAPERLESS_DISABLE_REGULAR_LOGIN` leaves the Django admin login open, so the route stays blocked.
- `PAPERLESS_COOKIE_PREFIX=paperless_` renames the CSRF, session and language cookies. It was added for a suspected clash with another `*.epaflix.com` cookie. The real cause of the first-login 403 was a repeated login POST after a successful login. The prefix stays, because renaming the cookies logs every browser out.
- Phones use an API token, as described in Phone setup.
- After the owner's first login, an owner-confirmed `manage.py shell` sets `is_superuser` and `is_staff` on his user.

### In-app objects

With the owner's approval, the agent ran a `manage.py shell` script with `get_or_create` once. None of these objects has an owner, so every user sees them.

| Object | Name | Id | Details |
|---|---|---|---|
| Group | `receipts` | 1 | view, add, change and delete on Document, Note, SavedView and UISettings; view, add and change on Tag, Correspondent, DocumentType and CustomField, 28 permissions in total |
| Custom field | `Total` | 1 | type monetary, `extra_data` empty, so no default currency |
| Custom field | `Gmail thread` | | type string, created by `ingest/configure.py`; thread-sync stores the Gmail thread ID in it |
| Tag | `paperless-gpt-auto` | 1 | matching algorithm None, so Paperless never assigns it on its own |
| Workflow | `Tag new receipts for AI` | 1 | trigger 1 Document Added excluding `source:gmail` and `source:drive`, action 1 assigns tag `paperless-gpt-auto` |
| User | `paperless-gpt` | 3 | superuser, unusable password, API token in `paperless-gpt-secrets` |

The owner is superuser and does not need the group.

### Removing a user

Removing someone from `Servarr users` stops new web logins only. Their API token keeps working. In Paperless, open Users & Groups, edit the user and clear Active. That blocks the web session and the API token.

## Phone setup

1. Install Paperless Go from F-Droid, package `com.ventouxlabs.paperlessgo.nogoogle`, or from its GitHub Releases. The Play Store app with the same name has a different package id, `com.github.iweinzierl.paperlessgo`.
2. Open https://paperless.epaflix.com in a browser and log in through Authentik.
3. Open My Profile, create an API token and copy it.
4. In Paperless Go, choose "Login with API token", enter server `https://paperless.epaflix.com` and paste the token.
5. Scan receipts with the camera's Receipt preset, or share a photo or PDF to Paperless Go. paperless-gpt fills in the title, shop and `Total` within a few minutes.

OCR runs Tesseract with `nor+eng+deu+ell` and `PAPERLESS_OCR_USER_ARGS={"oversample": 300}`. Paperless Go saves receipt scans as JPEG pages at about 137 ppi. On 2026-11-09 OCRmyPDF found 5 of 7 key strings on a real receipt at that resolution, 6 of 7 at 300 and 4 of 7 at 400. Made-up German and Greek receipts scored the same at every setting. Without `ell` the Greek receipt dropped to 1 of 6, so Greek stays in the default languages.

Owner check on 2026-11-09: one real receipt scanned in Paperless Go reached Paperless as a PDF with OCR text. paperless-gpt set the title, shop, date and `Total` in 2 minutes 12 seconds, and the next share-lists run made the shop visible to everyone. Its date was wrong before the prompt was revised. paperless-gpt no longer sets dates. `Total` got no currency code although the receipt prints one; the owner chose to leave the currency prompt unchanged.

Browser login inside the app is requested upstream in Paperless Go issue #51. For it, Authentik provider `paperless` is a public client and allows the strict redirect URI `com.ventouxlabs.paperlessgo:/oidc-callback`. Authentik skips the secret check for public clients, so the web login, which still sends the secret, keeps working. Authentik does not force PKCE on public clients; the strict redirect URIs limit where Authentik sends login codes. Until a Paperless Go release has "Sign in with Authentik", use the API token steps above.

## paperless-gpt

Deployment `paperless-gpt`, image `icereed/paperless-gpt`, reads Paperless OCR text and asks `qwen3.8-bonsai:27b` on llama-server at `http://llama-server.ai.svc.cluster.local:8080/v1` through its OpenAI provider. The model thinks before each answer. LLM OCR stays off. It calls `http://paperless:8000` with the API token of user `paperless-gpt`.

Workflow `Tag new receipts for AI` tags new documents `paperless-gpt-auto`, except those tagged `source:gmail` or `source:drive`. thread-sync assigns it to new Gmail documents, as Gmail threads describes. paperless-gpt polls that tag and sets the title, the shop as correspondent, tags, document type and `Total`. Then it removes `paperless-gpt-auto` and adds `paperless-gpt-auto-complete`, or `paperless-gpt-failed` when Paperless rejects a value. It creates those two tags at startup. It picks only existing tags and document types and may create new shops.

With `AUTO_GENERATE_CREATED_DATE=false`, paperless-gpt leaves the created date alone. Paperless uses the mail's Date header for email bodies. For attachments and phone scans, it uses the first date it finds in the document text, or the import day.

It runs as 1000:1000. The entrypoint skips its user setup when not root. Its files live on `paperless-data`: `gpt/config` with `settings.json`, `gpt/prompts`, and `gpt/db` with the SQLite undo history. On first start it copies the image's `default_prompts` into the empty `gpt/prompts`, and on later starts it updates every prompt nobody edited.

### Access

There is no ingress and the UI has no login. NetworkPolicy `paperless-gpt-ingress` denies all ingress. `kubectl port-forward` goes through the API server, so it still works:

```sh
kubectl -n paperless port-forward svc/paperless-gpt 8080:8080
```

Then open http://localhost:8080. Anyone who can port-forward in `paperless` can change every document.

### Custom field `Total`

The setting and the two prompt edits below were applied on 2026-11-08 after the PR 3 sync. They live on the NFS volume, so a new pod keeps them. Prompt files can also be edited directly in `/mnt/pithari/paperless/data/gpt/prompts` on TrueNAS, followed by a restart of `paperless-gpt`.

v0.31.0 has no environment variable for custom fields. The setting lives in `gpt/config/settings.json`. With the port-forward running, turn it on for field id 1 in append mode, which never overwrites a value already on the document:

```sh
curl -s localhost:8080/api/settings \
  | jq '.settings | .custom_fields_enable = true | .custom_fields_selected_ids = [1] | .custom_fields_write_mode = "append"' \
  | curl -s -X POST -H 'Content-Type: application/json' -d @- localhost:8080/api/settings
```

The POST replaces all settings, so the command sends back the current ones with only these three keys changed. Check with `curl -s localhost:8080/api/settings | jq .settings`.

### Prompts

`LLM_LANGUAGE=Norwegian, English, German or Greek` fills `{{.Language}}` in the title, tag, shop, document type, date and custom field prompts, rendered as "Norwegian, English, German Or Greek". Two prompts were edited in the UI under Settings and saved to `gpt/prompts`.

`created_date_prompt.tmpl` is unused while `AUTO_GENERATE_CREATED_DATE=false`. Keep this line in case date generation is turned back on:

```text
On a receipt this is the purchase date as printed on the receipt. The receipts write the day first, as DD.MM.YYYY, DD.MM.YY, DD/MM/YYYY or DD-MM-YYYY: the first number is the day and the second is the month. Take the date only from the content, never from these instructions.
```

`custom_field_prompt.tmpl`, instruction 3 replaced, so a receipt without a currency gets an empty currency instead of a guess:

```text
3.  For fields of type `monetary`, the value must be a number with two decimal places and a period as the decimal separator. Remove thousands separators and convert the decimal comma to a period. Put a three-letter currency code such as NOK, EUR or USD at the beginning of the value only when the document prints a currency code or symbol, such as `NOK`, `€` or `EUR`, or `kr` on a Norwegian receipt. If it prints none, return the number alone and never guess a currency from the shop, country or language. Format: an amount printed as `X.XXX,XX €` becomes `EURXXXX.XX`, an amount printed as `kr 1.234,50` on a Norwegian receipt becomes `NOK1234.50`, and an amount printed as `X.XXX,XX` with no currency becomes `XXXX.XX`.
```

The date line has no example date. An earlier version had one, and on a real receipt with noisy OCR the AI returned that example date. On 2026-11-09 the current wording gave the right date on that receipt twice and on six made-up receipts in Norwegian, German, English and Greek with formats DD.MM.YYYY, DD.MM.YY, DD/MM/YYYY and DD-MM-YYYY.

If AI date generation is turned back on, the date prompt can misread US receipts that print the month first. The custom field prompt may turn `kr` on a Swedish or Danish receipt into NOK. Fix those by hand. An edited prompt no longer follows upstream defaults. After an image update, compare it with the new `default_prompts`.

## Email and Drive import

The [import design](../../docs/superpowers/specs/2026-11-10-paperless-ingest-design.md) covers scope and limits. Gmail imports run every 10 minutes in container `thread-sync`. Drive import is a one-off copy, not a sync. Both keep the originals and assign imported documents to the owner, not other Paperless users.

Deployment and Service `paperless-converters` run Tika on port 9998 and Gotenberg on port 3000 for email PDFs and Office files. NetworkPolicy `paperless-converters-ingress` admits only Paperless pods. Gotenberg disables JavaScript and allows only local files under `/tmp`, so email scripts and tracking pixels cannot load. Converter failures do not take the Paperless pod offline. `PaperlessConverterRestarting` warns when either converter restarts at least three times in 15 minutes.

### Owner setup

The owner enables Gmail 2-Step Verification and IMAP, creates an app password and stores it in a vault item holding the Gmail app password, `<gmail-imap-item>`. He creates his own Paperless API token under My Profile and stores it in a vault item holding the owner's Paperless API token, `<paperless-token-item>`. Never put either value in Git, shell arguments or logs.

Both Gmail filters exclude Promotions, Social and Forums. They match mail that meets any of these criteria:

- The subject contains one of these words or phrases: receipt, invoice, bill, kvittering, faktura, regning, ordrebekreftelse, order confirmation, Rechnung, Quittung, Bestellbestätigung, απόδειξη, τιμολόγιο, λογαριασμός.
- The sender is on a short list of known billing senders.
- The sender is on a longer list of billing senders and a receipt word appears anywhere in the mail.

The attachment filter adds `paperless-receipt` using `filename:(pdf OR doc OR docx OR xls OR xlsx OR odt OR ods)`. The body filter adds `paperless-receipt-body` using `-filename:(pdf OR doc OR docx OR xls OR xlsx OR odt OR ods)`.

The old body-wide keyword filter was deleted and its label removed from all mail. The two current filters were applied to existing mail on 2026-11-18. If a filter misses a receipt, add `paperless-receipt` by hand to import eligible attachments or `paperless-receipt-body` to import the email body. Replies need no label; thread-sync finds them by thread.

### Gmail threads

Container `thread-sync` in the Paperless pod imports Gmail receipts every 10 minutes with `ingest/thread_sync.py`; design: [Gmail threads](../../docs/superpowers/specs/2026-11-28-paperless-gmail-threads-design.md). One receipt conversation becomes one document:

- The first mail labelled `paperless-receipt` creates the document from its first PDF or Office file, attached or inline. Its other files become versions on the next run. A mail labelled `paperless-receipt-body`, or `paperless-receipt` without an eligible file, creates the document from the email page.
- Later replies in the same Gmail thread, including the owner's, add their text as a note and their files as versions. A mail counts as a reply only with an `In-Reply-To` or `References` header, so monthly bills that Gmail groups by subject stay separate documents. Replies go to the newest document of the thread.
- When a document has two or more files, the newest version is one PDF joining all of them, labelled `All files: <ids>`. Preview, phone app and search show that file. The originals stay as older versions.
- New documents get title from subject, the owner, tags `source:gmail` and `paperless-gpt-auto`, and custom field `Gmail thread`. Paperless skips workflows for versions, so paperless-gpt only reads the first file.

Gmail labels show the state of each message:

| Label | Meaning |
|---|---|
| `paperless-done` | Every file is in Paperless and the note exists |
| `paperless-failed` | Paperless failed to import a file; the Paperless task list has the error |
| `paperless-done-att`, `paperless-done-eml` | Imported by the old mail rules before 2026-11-28; thread-sync skips them |

Each run logs one line, for example `thread-sync: read=2 created=1 versions=0 joined=0 notes=1 done=1 waiting=1 import_failed=0 failed=0`. `failed` counts messages with an error, logged with the Gmail message ID; they are retried next run. `PaperlessThreadSyncRestarting` warns when the container restarts at least three times in 15 minutes.

```sh
kubectl -n paperless logs deploy/paperless -c thread-sync --tail 20
```

To retry a failed message, fix the cause and remove `paperless-failed`. To import a message again, permanently delete its document or version, then remove `paperless-done`. A file whose checksum exists in Paperless, trash included, is never imported twice.

The mail rules `gmail-receipt-bodies` and `gmail-attachments` stay in Paperless, disabled, so Mail > Processed Mails keeps their history.

### Migrate existing Gmail documents

`ingest/thread_migrate.py` applies the same rules to documents the old mail rules imported. It matches each message labelled `paperless-done-att` or `paperless-done-eml` to its documents by SHA-256, sets `Gmail thread`, turns replies' email pages into notes and moves those pages to the trash, and makes replies' files versions. Without `THREAD_MIGRATE_APPLY=1` it only prints the plan as document IDs. Run the dry run, have the owner approve the printed `version` and `delete` lines, then apply:

```sh
kubectl -n paperless exec -i deploy/paperless -c thread-sync -- python3 manage.py shell -v 0 < 2-k3s/33.paperless/ingest/thread_migrate.py
kubectl -n paperless exec -i deploy/paperless -c thread-sync -- env THREAD_MIGRATE_APPLY=1 python3 manage.py shell -v 0 < 2-k3s/33.paperless/ingest/thread_migrate.py
```

Trashed documents can be restored from Paperless's trash for 30 days. The next thread-sync run builds the joined files.

### Configure Gmail

Run from the repository root after the reviewed manifests sync and the owner approves. Replace the placeholders with the Paperless pod name, the owner's Paperless username from `/api/profile/` and his Gmail address. Keep profile output private.

```sh
umask 077
export D="$(mktemp -d)"
export POD='<paperless-pod>' OWNER='<paperless-username>' EMAIL='<gmail-address>'
kubectl -n paperless cp 2-k3s/33.paperless/ingest/configure.py "$POD":/tmp/configure.py -c paperless
vault run --name '<gmail-imap-item>' --password-env VAULT_IMAP_PW -- sh -c '
  python3 -c "import json, os; print(json.dumps(dict(owner=os.environ[\"OWNER\"], email=os.environ[\"EMAIL\"], password=os.environ[\"VAULT_IMAP_PW\"])))" |
    kubectl -n paperless exec -i "$POD" -c paperless -- sh -c "cd /usr/src/paperless/src && python3 manage.py shell -c \"exec(open(\\\"/tmp/configure.py\\\").read())\"" > "$D/configure.out" 2>&1
'
```

Read the private `$D/configure.out`. Run the vault command again to check that setup remains idempotent. The script reports created or updated object names, not separate creation counts. Then remove the copied script:

```sh
kubectl -n paperless exec "$POD" -c paperless -- rm /tmp/configure.py
```

`ingest/configure.py` keeps both mail rules disabled and creates custom field `Gmail thread`. Check the `thread-sync` log and the Paperless task list after the next run. To rotate the app password, the owner updates the vault item, then rerun this setup with the same owner and email. Mail account credentials live in the Paperless database, not a Kubernetes Secret.

### Import Drive

Use the existing homelab client credential in `MCP_HUB_TOKEN`. Do not print it or substitute a Google upstream bearer. Downloads use the authenticated [hub attachment path](../23.mcp-hub/instances/README.md), `/drive/attachments/<uuid>`.

From the repository root, create a private output directory and a venv with the same dependencies as CI:

```sh
umask 077
export D="$(mktemp -d)"
python3 -m venv "$D/venv"
"$D/venv/bin/pip" install mcp==2.2.0 -r .github/hooks/requirements.txt
kubectl -n paperless port-forward svc/paperless 18000:8000
```

Leave the port-forward running. In another shell, set `D` to that same private directory and run the plan:

```sh
export D='<private-output-directory>'
vault run --name '<paperless-token-item>' --password-env VAULT_PL_TOKEN -- \
  "$D/venv/bin/python" 2-k3s/33.paperless/ingest/drive_import.py --out "$D" --plan-only
```

`--plan-only` lists owned files and writes `report.json` without downloading or uploading documents. Review that private report with the owner. After approval, run:

```sh
vault run --name '<paperless-token-item>' --password-env VAULT_PL_TOKEN -- \
  "$D/venv/bin/python" 2-k3s/33.paperless/ingest/drive_import.py --out "$D"
```

The script defaults to Paperless at `http://127.0.0.1:18000` and the homelab hub's `/drive` endpoint. It tags uploads `source:drive`, uses the Drive name as title and exports Google Docs, Sheets and Slides as PDFs. Unsupported types appear as skipped in the manifest. It downloads one file at a time, removes the local copy after each attempt and limits downloads to 1 GiB per rolling hour.

Rerun with the same `--out` directory to resume from `manifest.jsonl` and skip already uploaded file IDs. If an upload response was lost, a rerun may retry the same file. Paperless rejects it if it is identical to a document already imported. Keep that directory private and retain it between runs. `report.json` contains cumulative counts and `failed_ids`, not a per-run upload count. Compare runs until no new uploads occur, check `stopped` and the failed list, and verify upload tasks in Paperless. An accepted upload is not proof that Paperless finished processing it.

## Shared lists

Paperless gives every shop, tag and document type created through the API or web UI an owner, so other users would not see it. CronJob `paperless-share-lists` runs every 10 minutes with the Paperless image and sets the owner of every correspondent, tag and document type to none. Its `command` runs `manage.py shell -c` directly, so the image entrypoint, with its migrations and package installs, does not run. Each run logs how many rows it changed per model.

Run it once by hand:

```sh
kubectl -n paperless create job --from=cronjob/paperless-share-lists share-lists-manual
```

## Storage

PV `paperless-data-pithari` mounts NFS `192.168.10.200:/mnt/pithari/paperless/data`, the dataset `pithari/paperless/data` on the TrueNAS raidz2 pool `pithari`. It inherits the encryption of `pithari/paperless`, whose key TrueNAS stores and is set to load at boot, with a copy in the vault. Unlock after a TrueNAS reboot has not been tested yet. The TrueNAS share allows the five k3s workers and maps every client to user and group `paperless` (1000:1000), which matches `USERMAP_UID`/`USERMAP_GID`. `PAPERLESS_DATA_DIR` is `data/`, `PAPERLESS_MEDIA_ROOT` is `media/` and paperless-gpt uses `gpt/` on that volume. The consume folder is `emptyDir`, because uploads through the API and web UI skip it.

TrueNAS snapshot task `pithari` (recursive, daily 00:45, kept 30 days) protects the files against deletion. CronJob `paperless-backup` copies them to MinIO, as described in Backup. The database uses the CNPG backups to MinIO.

## Backup

CronJob `paperless-backup` runs daily at 03:30 UTC. It mounts the volume read-only and uploads it with restic to `s3:https://minio.epaflix.com/paperless-backups`, host `paperless`, tag `files`. It skips `data/index`, which `manage.py document_index reindex` rebuilds, and `data/log`, which Loki already holds. The first run creates the repository. Sunday runs read back all repository data with `restic check --read-data`, then keep 14 daily, 8 weekly and 12 monthly snapshots. A failed upload or check skips retention.

Paperless keeps running during the backup, so a snapshot can hold a document that the database backup of the same night does not, or the reverse. `gpt/db` is copied while paperless-gpt may write to it.

NetworkPolicy `paperless-backup` lets the pod reach DNS and Traefik's internal listener on TCP 8443, which serves `minio.epaflix.com`. A NetworkPolicy cannot filter by hostname, so the pod can also reach the other apps on that listener.

MinIO runs on the same TrueNAS host, on pool `mpara-tank-lan`. The encrypted TrueNAS cloud sync of `mpara-tank-lan/minio` copies the bucket to Google Drive every night, so the files survive the loss of TrueNAS.

### Recreate the bucket and keys

Bucket `paperless-backups`, user `paperless-backup` and policy `paperless-backups-rw` exist on the S3 store, and Secret `paperless-backup` holds the keys. After a loss of the S3 store, recreate them on a LAN machine with `mc`, `sops`, `openssl` and the cluster age key, from the repository root:

```sh
mc alias set homelab https://minio.epaflix.com
```

It asks for the MinIO admin keys. Run it alone, then the rest:

```sh
mc mb --ignore-existing homelab/paperless-backups
mc anonymous set none homelab/paperless-backups
mc admin policy create homelab paperless-backups-rw /dev/stdin <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": ["arn:aws:s3:::paperless-backups/*"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": ["arn:aws:s3:::paperless-backups"]
    }
  ]
}
JSON
S3_SECRET=$(openssl rand -hex 32)
printf '%s\n%s\n' paperless-backup "$S3_SECRET" | mc admin user add homelab
mc admin policy attach homelab paperless-backups-rw --user paperless-backup
f=2-k3s/33.paperless/paperless-backup.enc.yaml
printf '"%s"' paperless-backup | sops set --value-stdin "$f" '["stringData"]["access-key"]'
printf '"%s"' "$S3_SECRET" | sops set --value-stdin "$f" '["stringData"]["secret-key"]'
unset S3_SECRET
```

The S3 secret key stays in a shell variable and reaches `mc` and `sops` on stdin. Merge the changed file. After the sync, start a run and read its log:

```sh
kubectl -n paperless create job --from=cronjob/paperless-backup paperless-backup-manual
kubectl -n paperless logs -f job/paperless-backup-manual
```

Do not start a manual Job while another backup runs. A Sunday run also checks and prunes the repository, so it takes longer.

### Restore

No restore script exists. Restore a chosen snapshot into an empty directory, never onto the live volume, from a pod with the same image, environment and Secret as the CronJob and a writable target:

```sh
restic snapshots --host paperless --tag files
restic restore <snapshot-id> --target /restore --verify
```

The files land under `/restore/srv/paperless`. Restore the database to the same point in time with CNPG, copy `media` and `gpt` back with Paperless and paperless-gpt at zero replicas, then run `manage.py document_index reindex` and `manage.py document_sanity_checker`, which lists files and database rows that do not match.

A killed Job can leave a repository lock that fails the next Sunday run. Check that no backup Job is running, then run `restic unlock` from such a pod.

### Move to TrueNAS

Done on 2026-11-23: one `zfs send` of snapshot `hdd/paperless@pithari-move` from shypowder into `pithari/paperless/data`, with Paperless at zero. A PV's NFS source cannot change in place, so Argo deleted and recreated the PV and PVC.

## Secrets

`paperless-secrets.enc.yaml` holds Secret `paperless-db-role` in `postgres-system` and Secret `paperless-secrets` in `paperless`, both at sync wave `-2`. `paperless-secrets` has `PAPERLESS_SECRET_KEY`, `PAPERLESS_DBPASS` and `PAPERLESS_SOCIALACCOUNT_PROVIDERS`.

The values were generated at random when the files were encrypted and exist only in the SOPS files. No vault item holds them. The OIDC client id and secret appear twice: in `PAPERLESS_SOCIALACCOUNT_PROVIDERS` and in provider `paperless` of `2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml`. Rotate them in both files in the same PR. The database password appears in both Secrets of `paperless-secrets.enc.yaml`.

`paperless-gpt-secrets.enc.yaml` holds Secret `paperless-gpt-secrets` with `PAPERLESS_API_TOKEN`, the DRF token of user `paperless-gpt`. `manage.py drf_create_token paperless-gpt` ran inside the `vault run` command that encrypted the file, so the token was never printed. To rotate it, run the same command with `-r` the same way, then restart `paperless-gpt`.

`paperless-backup.enc.yaml` holds Secret `paperless-backup` with the restic repository `password`, generated at random inside the command that encrypted the file. It exists only in that SOPS file, and without it the repository cannot be read. `access-key` and `secret-key` are the S3 keys of user `paperless-backup`.

## DNS

Public DNS resolves through the Cloudflare wildcard and tunnel. The LAN needs `paperless.epaflix.com` in `1-proxmox/pihole/dnsmasq.d/10-epaflix.conf` as A `192.168.10.101` and in `1-proxmox/pihole/unbound-no-aaaa-leak.conf`. Apply them on the Pi-hole the way `2-k3s/08.servarr/navidrome/README.md` records.

## Observability

- `PaperlessDown` fires when the Deployment has no available replica for 15 minutes or is absent.
- `PaperlessGptDown` does the same for Deployment `paperless-gpt`. It reads kube-state-metrics, so Prometheus needs no access to the pod.
- `PaperlessShareListsFailed` fires 5 minutes after the newest `paperless-share-lists` Job fails and clears when the next run succeeds.
- `PaperlessBackupOverdue` fires when `paperless-backup` has had no successful run for 30 hours or has never succeeded, which includes a suspended CronJob.
- `PaperlessBackupFailed` fires 5 minutes after the newest `paperless-backup` Job fails. `KubeJobFailedLastRun` skips this CronJob.
- Blackbox target `paperless` probes `https://192.168.10.101` with host `paperless.epaflix.com`.
- Logs reach Loki through Alloy.
