# Immich

Photos and videos for the owner and friends at https://immich.epaflix.com. Design: [docs/superpowers/specs/2026-11-27-immich-design.md](../../docs/superpowers/specs/2026-11-27-immich-design.md).

The official chart `oci://ghcr.io/immich-app/immich-charts/immich` runs the server, machine learning and Valkey. Each person has a private library. `server.publicUsers` is off, so users cannot list each other. Share links and shared albums still work when someone creates one.

## Login

Login goes only through Authentik OpenID, provider and application `immich` in the encrypted blueprint. Members of "Servarr users" and "authentik Admins" can log in, and Immich creates the account on first login. Scope `immich` sends `immich_role`: `admin` for "authentik Admins", `user` for everyone else. Immich applies it on every login. Password login and passkeys are off, so no one can log in around Authentik. There is no forward auth in front of Immich: the phone app and share links could not pass it.

Phone app: server URL `https://immich.epaflix.com`, then "Login with Authentik".

To add a friend, add them to "Servarr users" in Authentik. To remove one, take them out of the group, then delete their user in Immich Administration. Immich keeps their photos until the user is deleted.

## Configuration

Immich reads its settings from Secret `immich-config` (key `immich-config.yaml`, in `secrets.enc.yaml`). With a config file, the web settings page is read-only: change settings there, not in the UI. Notable values:

- `oauth.defaultStorageQuota: null` is no quota. `0` would mean 0 GiB.
- `storageTemplate.enabled: false`: originals stay in `upload/<user id>/` and never move. Moves would leave extra copies in the Drive sync.
- `backup.database.enabled: false`: Immich's own dumps need superuser. `postgres-cluster` backups cover the database.

The OpenID client id and secret exist only in that Secret and in the Authentik blueprint. Change them in both in the same commit.

## Uploads over 100 MB

Public `*.epaflix.com` goes through Cloudflare, which refuses request bodies over 100 MB. Immich uploads a file in one request, so longer videos only finish on home Wi-Fi, where Pi-hole sends `immich.epaflix.com` to Traefik on `192.168.10.101`. The phone app keeps them queued until then. Keep "upload videos over cellular" off in the app. Resumable upload is Immich PR #22385; drop this section once it and app support ship.

## Storage

- Library: NFS `192.168.10.200:/mnt/pithari/immich`, PV `immich-library-pithari`, mounted at `/data`. The share maps every client to TrueNAS user `immich` 1001:1001, and the pods run as 1001.
- Dataset `pithari/immich` is encrypted with a key stored on TrueNAS. A copy of the key belongs in the vault (owner step). It is in the daily recursive `pithari` snapshots (30 days).
- Machine learning model cache: PVC `immich-ml-cache` on `truenas-iscsi`.

## Database

`immich` in `postgres-cluster`, role `immich` without superuser. The `Database` object creates `vector`, `vchord`, `cube` and `earthdistance`. After a VectorChord update (Renovate never auto-merges it), run as `postgres` in database `immich`:

```sql
ALTER EXTENSION vchord UPDATE;
REINDEX INDEX face_index;
REINDEX INDEX clip_index;
```

## Machine learning

Runs on `k3s-worker-64` with one time-slice of the RTX 2070 SUPER, shared with Ollama and Jellyfin. GPU memory is not split. If `ImmichMachineLearningDown` or out-of-memory errors repeat, shorten Ollama's keep-alive or move Immich machine learning to CPU (remove the GPU settings and use the image tag without `-cuda`).

## Offsite copy

TrueNAS cloud sync task `immich photos to Google Drive (encrypted)` copies `upload/` and `profile/` from `/mnt/pithari/immich` to Google Drive `/epaflix-offsite/immich` every day at 03:00. It uses COPY mode, so deletes in Immich never reach Drive, and Drive grows with everything ever uploaded. Encryption uses the same password and salt as task 1, so the existing offsite encryption credentials in the vault read both. TrueNAS's own alerts report a failed run.

Restore one user's originals: create a TrueNAS cloud sync PULL task from the same folder with the same encryption settings into an empty dataset, then upload the files to Immich again.

## Upgrades

Renovate groups the server, machine learning and chart and never auto-merges them. Read the release notes, then merge. Phone apps must run the same major and minor as the server.
