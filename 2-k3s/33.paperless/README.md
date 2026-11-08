# Paperless-ngx

Receipts archive with OCR. Design: `docs/superpowers/specs/2026-11-08-receipts-design.md`.

Hostname https://paperless.epaflix.com on Traefik `websecure` at `192.168.10.101`, so phones can upload from the internet. The pod runs Paperless on port 8000 and Valkey on `127.0.0.1:6379` with `emptyDir` storage. The database is `paperless` in `postgres-cluster`.

## Access

- Login goes through the Authentik OAuth2 provider `paperless` with django-allauth `openid_connect`, provider id `authentik`. Authentik forward auth does not sit in front of Paperless.
- The Authentik application `paperless` binds `authentik Admins` and `Servarr users`. Authentik denies everyone else, so Paperless never creates an account for them.
- Paperless creates the account on the first SSO login and adds it to group `receipts` through `PAPERLESS_SOCIAL_ACCOUNT_DEFAULT_GROUPS`. Password login in the web UI is off and the login page redirects to Authentik.
- The REST API still accepts basic auth. No user has a usable password, so do not set one with `manage.py`.
- Traefik answers 403 to every request for `/admin/`. `PAPERLESS_DISABLE_REGULAR_LOGIN` leaves the Django admin login open, so the route stays blocked.
- `PAPERLESS_COOKIE_PREFIX=paperless_` renames the CSRF, session and language cookies. The prefix keeps Paperless cookies apart from other `*.epaflix.com` apps.
- Phones use an API token. The user logs in to the web UI once, creates the token under My Profile and pastes it into Paperless Go.
- After the owner's first login, an owner-confirmed `manage.py shell` sets `is_superuser` and `is_staff` on his user.

### In-app objects

Created once with an owner-confirmed `manage.py shell` script that uses `get_or_create`. None of them has an owner, so every user sees them.

| Object | Name | Id | Details |
|---|---|---|---|
| Group | `receipts` | 1 | view, add, change and delete on Document, Note, SavedView and UISettings; view, add and change on Tag, Correspondent, DocumentType and CustomField (28 permissions) |
| Custom field | `Total` | 1 | type monetary, `extra_data` empty, so no default currency |
| Tag | `paperless-gpt-auto` | 1 | matching algorithm None, so Paperless never assigns it on its own |
| Workflow | `Tag new receipts for AI` | 1 | trigger 1 Document Added from every source, action 1 assigns tag `paperless-gpt-auto` |
| User | `paperless-gpt` | 3 | superuser, unusable password, API token in `paperless-gpt-secrets` |

The owner is superuser and does not need the group.

### Removing a user

Removing someone from `Servarr users` stops new web logins only. Their API token keeps working. In Paperless, open Users & Groups, edit the user and clear Active. That blocks the web session and the API token.

## paperless-gpt

Deployment `paperless-gpt`, image `icereed/paperless-gpt`, reads Paperless OCR text and asks Ollama `qwen3.5:9b` at `http://ollama.ai.svc.cluster.local:30068` with thinking off. LLM OCR stays off. It calls `http://paperless:8000` with the API token of user `paperless-gpt`.

Workflow `Tag new receipts for AI` tags every new document `paperless-gpt-auto`. paperless-gpt polls that tag and sets the title, shop (correspondent), tags, document type, created date and `Total`. Then it removes `paperless-gpt-auto` and adds `paperless-gpt-auto-complete`, or `paperless-gpt-failed` when Paperless rejects a value. It creates those two tags at startup. It picks only existing tags and document types and may create new shops.

It runs as 1000:1000. The entrypoint skips its user setup when not root. Its files live on `paperless-data`: `gpt/config` (`settings.json`), `gpt/prompts` and `gpt/db` (SQLite undo history). On first start it copies the image's `default_prompts` into the empty `gpt/prompts`, and on later starts it updates every prompt nobody edited.

### Access

There is no ingress and the UI has no login. NetworkPolicy `paperless-gpt-ingress` denies all ingress. `kubectl port-forward` goes through the API server, so it still works:

```sh
kubectl -n paperless port-forward svc/paperless-gpt 8080:8080
```

Then open http://localhost:8080. Anyone who can port-forward in `paperless` can change every document.

### Custom field `Total`

v0.31.0 has no environment variable for custom fields. The setting lives in `gpt/config/settings.json`. With the port-forward running, turn it on for field id 1 in append mode, which never overwrites a value already on the document:

```sh
curl -s localhost:8080/api/settings \
  | jq '.settings | .custom_fields_enable = true | .custom_fields_selected_ids = [1] | .custom_fields_write_mode = "append"' \
  | curl -s -X POST -H 'Content-Type: application/json' -d @- localhost:8080/api/settings
```

The POST replaces all settings, so the command sends back the current ones with only these three keys changed. Check with `curl -s localhost:8080/api/settings | jq .settings`.

### Prompts

`LLM_LANGUAGE=Norwegian, English, German or Greek` fills `{{.Language}}` in the title, tag, shop, document type, date and custom field prompts, rendered as "Norwegian, English, German Or Greek". Two prompts change, in the UI under Settings, saved to `gpt/prompts`:

`created_date_prompt.tmpl`, a new second line:

```text
On a receipt this is the purchase date as printed. Receipts here print the day first, so read 03.11.26 or 03/11/2026 as 3 November 2026.
```

`custom_field_prompt.tmpl`, instruction 3 replaced, so a receipt without a currency gets an empty currency instead of a guess:

```text
3.  For fields of type `monetary`, the value must be a number with two decimal places and a period as the decimal separator. Remove thousands separators and convert the decimal comma to a period. Put a three-letter currency code (e.g., NOK, EUR, USD) at the beginning of the value only when the document prints a currency code or symbol, such as `kr`, `NOK`, `€` or `EUR`. If it prints none, return the number alone and never guess a currency from the shop, country or language. Format: an amount printed as `X.XXX,XX €` becomes `EURXXXX.XX`, and an amount printed as `X.XXX,XX` becomes `XXXX.XX`.
```

An edited prompt no longer follows upstream defaults. After an image update, compare it with the new `default_prompts`.

## Shared lists

Paperless gives every shop, tag and document type created through the API or web UI an owner, so other users would not see it. CronJob `paperless-share-lists` runs every 10 minutes with the Paperless image and sets the owner of every correspondent, tag and document type to none. Its `command` runs `manage.py shell -c` directly, so the image entrypoint, with its migrations and package installs, does not run. Each run logs how many rows it changed per model.

Run it once by hand:

```sh
kubectl -n paperless create job --from=cronjob/paperless-share-lists share-lists-manual
```

## Storage

PV `paperless-data-shypowder` mounts NFS `192.168.10.13:/hdd/paperless`, the ZFS dataset `hdd/paperless` on shypowder described in `1-proxmox/shypowder/README.md`. The export squashes every client to 1000:1000, which matches `USERMAP_UID`/`USERMAP_GID`. `PAPERLESS_DATA_DIR` is `data/`, `PAPERLESS_MEDIA_ROOT` is `media/` and paperless-gpt uses `gpt/` on that volume. The consume folder is `emptyDir`, because uploads through the API and web UI skip it.

Only sanoid snapshots on shypowder, one disk, back up the files. The database uses the CNPG backups to MinIO.

### Move to TrueNAS

When the 6×14 TB pool exists on TrueNAS:

1. Scale Paperless and paperless-gpt to zero.
2. `syncoid hdd/paperless` to a dataset on the new pool, snapshots included.
3. Export that dataset over NFS from TrueNAS to the k3s workers, squashed to 1000:1000.
4. In a PR, replace the PV with one pointing at TrueNAS, then scale up.
5. After a check, stop the shypowder export. Deleting `hdd/paperless` needs the owner's approval.

A PV's NFS source cannot change in place. Argo must delete and recreate the PV and PVC, with Paperless at zero.

## Secrets

`paperless-secrets.enc.yaml` holds Secret `paperless-db-role` in `postgres-system` and Secret `paperless-secrets` in `paperless`, both at sync wave `-2`. `paperless-secrets` has `PAPERLESS_SECRET_KEY`, `PAPERLESS_DBPASS` and `PAPERLESS_SOCIALACCOUNT_PROVIDERS`.

The values were generated at random when the files were encrypted and exist only in the SOPS files. No vault item holds them. The OIDC client id and secret appear twice: in `PAPERLESS_SOCIALACCOUNT_PROVIDERS` and in provider `paperless` of `2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml`. Rotate them in both files in the same PR. The database password appears in both Secrets of `paperless-secrets.enc.yaml`.

`paperless-gpt-secrets.enc.yaml` holds Secret `paperless-gpt-secrets` with `PAPERLESS_API_TOKEN`, the DRF token of user `paperless-gpt`. `manage.py drf_create_token paperless-gpt` ran inside the `vault run` command that encrypted the file, so the token was never printed. To rotate it, run the same command with `-r` the same way, then restart `paperless-gpt`.

## DNS

Public DNS resolves through the Cloudflare wildcard and tunnel. The LAN needs `paperless.epaflix.com` in `1-proxmox/pihole/dnsmasq.d/10-epaflix.conf` as A `192.168.10.101` and in `1-proxmox/pihole/unbound-no-aaaa-leak.conf`. Apply them on the Pi-hole the way `2-k3s/08.servarr/navidrome/README.md` records.

## Observability

- `PaperlessDown` fires when the Deployment has no available replica for 15 minutes or is absent.
- `PaperlessGptDown` does the same for Deployment `paperless-gpt`. It reads kube-state-metrics, so Prometheus needs no access to the pod.
- Blackbox target `paperless` probes `https://192.168.10.101` with host `paperless.epaflix.com`.
- Logs reach Loki through Alloy.
