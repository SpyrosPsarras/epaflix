# Receipts archive with OCR and automatic fields

## Intent

The owner and his friends keep their receipts in one place on the
homelab. They upload from an Android phone or a PC. The server reads each
receipt and fills in the shop, the date and the total, so nobody types
them. Use existing open-source apps and write no new app.

## Goals

- G1. A receipt photographed on Android or uploaded from a PC reaches the
  archive in one step.
- G2. Every receipt gets OCR text, a shop, a purchase date and a total
  without manual typing. People correct mistakes by hand.
- G3. Each person finds a receipt again by shop, date, amount or text, as
  proof for a return or a warranty claim.
- G4. Each person can filter their receipts by month and shop and see the
  totals.
- G5. Only members of the Authentik group `Servarr users`, plus
  `authentik Admins`, can log in.
- G6. Receipt content stays on the homelab. OCR and the language model
  run locally.
- G7. Each new app has a health alert, logs in Loki, and the storage
  disk has a health alert.

## Non-goals

- Splitting costs between people, "who owes whom". The owner picked O1,
  which leaves this out.
- An off-site copy. Storage is temporary, as the Storage section says.
- Email ingestion, a watched folder on a PC, and document chat (RAG).
- An iPhone app. The owner chose an Android app only. iPhone users
  can use the web UI.
- Paperless-ngx's built-in AI. Its "Apply AI Suggestions" workflow
  fills title, tags, correspondent, document type, storage path and
  created date, but not custom fields, so it cannot fill `Total`.
  paperless-gpt does all of them, so only one AI component runs.

## Anti-goals

- Receipt text or images sent to a cloud AI or OCR service.
- One user seeing another user's receipts without an explicit share.
- A login path where the client names its own identity in a header.
- Credentials in Git, chat or logs.

## Constraints

- Apps run in k3s. Argo CD owns them. Changes reach the cluster only
  through a merged PR.
- Images use a version tag plus digest so Renovate can update them.
- The shared CNPG cluster `postgres-cluster` in `postgres-system` runs
  Postgres 16. New apps add a `DatabaseRole` and `Database` in Git, like
  `2-k3s/25.fytorium/database.yaml`.
- Ollama runs at `http://ollama.ai.svc.cluster.local:30068` on the GPU
  node k3s-worker-64. It already has `qwen3.5:9b`, `gemma4:e4b`,
  `lfm2.5:8b` and `aya-expanse:8b`. Lingarr and Open WebUI share it.
- Storage lives on shypowder for now. shypowder has 4 CPU
  cores and about 3 GB free RAM, so it takes no new VM.
- The `truenas-iscsi` storage class sits on the TrueNAS `apps` pool, a
  100 GB virtual disk. It is not for receipt files.

## Approaches

### Apps

The owner picked O1.

- O1, chosen. Paperless-ngx, paperless-gpt and the Paperless Go Android
  app. Paperless-ngx has 47,863 GitHub stars, 643 commits and 14
  releases in the 90 days to 2026-11-08, and is GPL-3.0. paperless-gpt
  (MIT, 2,824 stars, 138 commits, 4 releases) fills in fields with
  Ollama. Covers G1 to G4. Has no cost splitting.
- O2. Receipt Wrangler. The only app that also splits costs. Rejected:
  297 stars, 2 releases in 90 days, no Authentik login, and its Android
  app has no license file.
- O3. O1 plus Spliit for cost splitting. Rejected for now: Spliit reads
  receipts only through OpenAI, and splits stay separate from the
  receipts.
- Dropped as inactive or incomplete: paperless-ai (no commits in 90
  days), Paperless Mobile (no commits in 90 days), Papra (no AI, no
  Android app), Docspell (slow).

### Android app

The owner picked Paperless Go (AGPL-3.0, 117 commits and 10 releases in
90 days). It has a camera scanner with a receipt preset, a share target,
offline upload queue and custom field editing. It logs in with username
and password or a pasted API token. Browser SSO login is requested in
issue #51 at `ventouxlabs/paperless-go`, open with no work started.
PaperNext was the alternative: free, on Google Play, more installs, but
closed source and labelled beta.

### Storage

The owner picked O4.

- O4, chosen. ZFS dataset `hdd/paperless` on shypowder, shared over NFS
  from the shypowder host. Paperless runs on any worker. The later move
  is a `zfs send` with snapshots.
- O5. A virtual disk from Proxmox storage `local-hdd` on VM 1064
  (k3s-worker-64) with a local PV. Rejected: it pins Paperless to the
  busy GPU node and the move is a file copy.

## Design

### Data flow

1. A user scans a receipt in Paperless Go, or uploads it in the web UI.
2. Paperless-ngx stores the original, runs Tesseract OCR and writes a
   searchable PDF and a thumbnail.
3. A Paperless workflow adds the tag `paperless-gpt-auto` to every new
   document.
4. paperless-gpt picks up tagged documents, sends the OCR text to
   Ollama, and writes back title, shop (correspondent), purchase date
   (created date), tags and the custom field `Total`. It then removes the
   tag.
5. The user sees the filled-in receipt in the app or web UI and fixes
   any mistake.

### Paperless-ngx

- New Argo application `paperless` in `2-k3s/33.paperless/`, namespace
  `paperless`.
- Image `ghcr.io/paperless-ngx/paperless-ngx`, current 3.x release,
  pinned.
- Database `paperless` and role `paperless` in `postgres-cluster`.
- Redis broker as a Valkey container in the same pod with `emptyDir`
  storage, like Open WebUI.
- `PAPERLESS_DATA_DIR` and `PAPERLESS_MEDIA_ROOT` on the NFS volume, as
  subdirectories `data/` and `media/`. Uploads through the API and web UI
  do not use the consume folder, so it stays on `emptyDir`.
- Custom field `Total`, type monetary, created once after first boot.
  Each value carries its own currency code. There is no default
  currency: when the receipt shows none, the currency stays empty and
  the user fills it in.
- OCR languages Norwegian, English, German and Greek:
  `PAPERLESS_OCR_LANGUAGE=nor+eng+deu+ell`, with the matching Tesseract
  packs in `PAPERLESS_OCR_LANGUAGES` if the image lacks them.
- Shops (correspondents), tags and document types are shared: they have
  no owner, so every user sees and uses the same list. Receipts stay
  private to their owner. Paperless gives every object created through
  the API or web UI an owner, and paperless-gpt does not override that.
  A CronJob `paperless-share-lists` runs every 10 minutes with the
  Paperless image and clears the owner of every correspondent, tag and
  document type with one `manage.py shell` command.
- Hostname `paperless.epaflix.com`, reachable from the internet so
  phones can upload away from home. Exposed the same way as Navidrome.
  Traefik blocks `/admin/` from outside the cluster, because
  `PAPERLESS_DISABLE_REGULAR_LOGIN` does not disable the Django admin
  login.
- Each user owns the receipts they upload. Others see them only when the
  owner shares them in Paperless.

### Login

- Paperless uses its own OIDC login against an Authentik OAuth2 provider
  `paperless`, through `PAPERLESS_SOCIALACCOUNT_PROVIDERS` with
  `openid_connect`. No Authentik forward auth in front of Paperless.
- The Authentik application binds `Servarr users` and `authentik
  Admins`, so nobody else completes the login.
- `PAPERLESS_DISABLE_REGULAR_LOGIN=true` and
  `PAPERLESS_REDIRECT_LOGIN_TO_SSO=true`.
- New SSO users join a Paperless group `receipts` through
  `PAPERLESS_SOCIAL_ACCOUNT_DEFAULT_GROUPS`. That group grants add, view
  and change on documents, tags, correspondents and custom fields.
- Phone setup: the user logs in to the web UI once, creates an API token
  under My Profile and pastes it into Paperless Go.
- The owner's Paperless account becomes superuser after his first SSO
  login.
- Removing someone from `Servarr users` stops new web logins only. Their
  API token keeps working. The README gets an owner step: deactivate the
  Paperless user, which also stops the token. A manual step with no
  sync job is the agent's call.

### paperless-gpt

- Image `icereed/paperless-gpt`, current release, pinned. Deployment in
  namespace `paperless`.
- `LLM_PROVIDER=ollama`, `LLM_MODEL=qwen3.5:9b`, thinking off.
- Uses Paperless OCR text. LLM OCR with a vision model stays off.
- Custom field generation on, target field `Total`, including the
  receipt's currency. If the receipt prints no currency, it leaves the
  currency empty instead of guessing.
- Shops and tags it creates get no owner, so all users see them.
- Receipts can be in Norwegian, English, German or Greek. The prompts
  say so and ask for the date as printed on the receipt.
- It uses an API token of a Paperless service user that can edit all
  documents.
- No ingress. paperless-gpt has no login. A NetworkPolicy allows ingress
  only from Prometheus, and the owner reaches the UI with
  `kubectl port-forward`.

### Storage

- shypowder: ZFS dataset `hdd/paperless` on the existing single-disk
  pool `hdd` (Seagate ST10000NE0008). Checked live on 2026-11-08: the
  pool exists and is online. Proxmox storage `local-hdd` points at
  `hdd/data`, which holds the disks of VMs 1041 and 1042. They stay
  untouched, and `hdd/paperless` sits beside them. No disk wipe is
  needed.
- Install `nfs-kernel-server` on shypowder. Export only `hdd/paperless`,
  only to the k3s worker IPs.
- Install `sanoid` on shypowder. Keep 24 hourly, 30 daily and 6 monthly
  snapshots of `hdd/paperless`.
- k3s: a static NFS PersistentVolume and PVC `paperless-data` in
  namespace `paperless`.
- Record the host changes in `1-proxmox/shypowder/README.md`.

### Later move to the 6×14 TB pool

The owner builds a 6×14 TB pool on TrueNAS, hosted on patroklos, later
this week. Then:

1. Scale Paperless and paperless-gpt to zero.
2. `syncoid hdd/paperless` to a dataset on the new pool, snapshots
   included.
3. Share that dataset over NFS from TrueNAS to the k3s workers.
4. A PR replaces the PV with one pointing at TrueNAS, then scales up.
5. After a check, stop the shypowder NFS export. Deleting
   `hdd/paperless` needs the owner's approval.

The move gets its own short plan when the pool exists.

### Backups

- Files: sanoid snapshots on shypowder. These undo deletions. They do not
  survive a dead disk.
- Database: the existing CNPG backups to MinIO on TrueNAS.

### Observability

- Alerts in `2-k3s/10.observability/alertmanager-config/custom-alerts.yaml`
  when `paperless` or `paperless-gpt` has no available replica, following
  the `digarr` rule and its test.
- A blackbox HTTPS probe of `paperless.epaflix.com`.
- Logs reach Loki through the existing Alloy collection.
- shypowder runs `prometheus-node-exporter` with the ZFS collector.
  Prometheus scrapes it, and an alert fires when pool `hdd` is not
  online or no longer reported, like the TrueNAS zpool alert. Clients
  mount NFS from `192.168.10.13`.

### Secrets

Paperless secret key, database password and OIDC client secret live in
SOPS-encrypted Secrets built from vault items, like Fytorium. The
paperless-gpt API token exists only after first boot. The agent creates
it with `manage.py` inside the command that writes the SOPS Secret, so
the value is never printed. No secret appears in Git
or chat.

## Risks

- R1. Receipts live on one disk with no second copy until the move. The
  owner accepted this as temporary.
- R2. Both future copies are in the same house.
- R3. Paperless Go has 6 stars and is young. If it stalls, PaperNext
  talks to the same server.
- R4. Ollama is shared. A busy GPU delays field extraction, not uploads.
- R5. paperless-gpt can edit every document. Only Prometheus and a
  port-forward reach it.
- R6. Paperless is reachable from the internet. Login is only through
  Authentik, and `/admin/` is blocked from outside.

## Left to the builder

- Exact image versions and digests.
- Valkey and Paperless resource requests and limits.
- The paperless-gpt prompts for receipts.
- Which Traefik middlewares and DNS records copy Navidrome's public
  setup.
- Checking that monthly totals per shop work in Paperless (G4). If saved
  views cannot sum `Total`, report it rather than add another app.
