# Fytorium

Plant care for one household, served by a Node/SvelteKit app on port 3000.
Source: [SpyrosPsarras/epaflix-fytorium](https://github.com/SpyrosPsarras/epaflix-fytorium).
Image: `ghcr.io/spyrospsarras/epaflix-fytorium`, published by source CI on pushes to main.
Hostname: https://fytorium.epaflix.com, LAN only through Traefik's `internal` entry point.
`/metrics` is cluster-internal, excluded from the IngressRoute. Prometheus scrapes the Service.

## Secrets

`fytorium-secrets.enc.yaml` was built from Vaultwarden. It holds `fytorium-db-role`
in `postgres-system` and `fytorium-secrets` in `fytorium`, both at sync wave `-2`.
The role Secret uses `username: fytorium` and a `password`; `DATABASE_URL` must use
that same password and database `fytorium` on `postgres-cluster-rw.postgres-system.svc:5432`.
Keep credentials out of git, command arguments and logs.

| Vaultwarden path | Values and use |
| --- | --- |
| `/Fytorium/fytorium - postgres` | Username `fytorium`, password. Used for `fytorium-db-role` and `DATABASE_URL`: `postgresql://fytorium:<urlencoded pw>@postgres-cluster-rw.postgres-system.svc:5432/fytorium` |
| `/Fytorium/fytorium - authentik oidc` | Username = `OIDC_CLIENT_ID`, password = `OIDC_CLIENT_SECRET`. Also supplies the client id and secret in the Authentik blueprint. |
| `/Fytorium/fytorium - minio` | Username = access key `fytorium-app`, password = secret key. Used for `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY`. |
| `/Fytorium/fytorium - vapid` | Password = `VAPID_PRIVATE_KEY`, custom field `public key` = `VAPID_PUBLIC_KEY`, custom field `subject` = `VAPID_SUBJECT`, set to `https://fytorium.epaflix.com`. |
| `/Fytorium/fytorium - firebase` | Optional backup copy of the Firebase files. The live server key is only in `fytorium-fcm.enc.yaml` (see below). A lost key is replaced by generating a new one in Firebase: Project settings → Service accounts. |
| `/Personal/plantnet - api key` | Password = `PLANTNET_API_KEY` |
| `/perenual - api key` | Password = `PERENUAL_API_KEY` |
| `/Personal/MinIO Object Store` | MinIO admin username/password, setup only; never added to app Secrets |

To regenerate `fytorium-secrets.enc.yaml`, rebuild it from these items with `sops --encrypt --filename-override 2-k3s/25.fytorium/fytorium-secrets.enc.yaml`; never write plaintext to disk.

The encrypted Authentik blueprint contains the group `Fytorium users` with state
`created`, OAuth2 provider `fytorium`, application slug `fytorium`, and a policy
binding to that group.

Generate the VAPID pair once and keep it across deployments. Changing it means
phones must subscribe again. `OPEN_PLANTBOOK_API_KEY` is intentionally absent.
FCM is optional. Without it, the server skips app devices and logs this once.
`fytorium-fcm.enc.yaml` holds the Secret `fytorium-fcm` with `FCM_PROJECT_ID`
(`fytorium`) and `FCM_SERVICE_ACCOUNT_JSON` (the Firebase Admin SDK service
account JSON, compact). To rotate it, generate a new key in Firebase, build the
Secret in memory and pipe it into
`sops --encrypt --input-type yaml --output-type yaml --filename-override 2-k3s/25.fytorium/fytorium-fcm.enc.yaml /dev/stdin`,
then delete the downloaded key.

Do not enable the test clock, synthetic push transport, test image origin or
insecure OIDC HTTP in this deployment. `FCM_BASE_URL` and
`FCM_OAUTH_TOKEN_URL` must stay unset in production.

## App compatibility

`MIN_APP_VERSION` is `1.0.0`, matching the first release `android-v1.0.0`.
Older apps receive HTTP 426. Raise it only when an app release is required.
`SERVER_VERSION` is `7cc52b9`, the source commit shown in `/api/meta`.
Bump it whenever the image digest changes.

## First deploy

`kustomization.yaml` pins the image by the digest that source CI reports.
Repository CI blocks all-zero placeholder digests.

1. From a LAN PC, run `./2-k3s/25.fytorium/minio-setup.sh --dry-run`, then
   `./2-k3s/25.fytorium/minio-setup.sh`. Requires Docker, Python 3 and the existing
   vault-run hub access. On first use, the script builds and caches
   `fytorium-mc:RELEASE.2025-04-16T18-13-26Z` from `mc.Dockerfile`, using
   Go 1.24 and Alpine 3.21. The build needs internet access to fetch the base
   images, pinned mc source and Go modules, but no credentials. Later runs
   reuse the local image without rebuilding. It runs mc without a container
   shell. It creates the private `fytorium-photos`
   bucket, suspends versioning, replaces the scoped read/write/delete policy,
   creates or updates the app user with keys on stdin and attaches the policy
   only when absent. Re-runs update the same bucket, user and policy. Suspended
   versioning does not delete any old versions. The app cannot list the bucket.
   `FYTORIUM_MINIO_URL` overrides the endpoint for isolated local tests; fake
   `MINIO_ADMIN_USERNAME`, `MINIO_ADMIN_PASSWORD`, `VAULT_USERNAME` and
   `VAULT_PASSWORD` can exercise `--apply` without vault access. The normal
   invocation always obtains both credential pairs through vault-run.
2. Apply both tracked Pi-hole DNS changes using `1-proxmox/pihole/README.md`.
   The dnsmasq A record points at `192.168.10.102`; the matching Unbound static
   zone stops AAAA queries leaking upstream. A static zone without the A record
   also returns NXDOMAIN for A. Restart Pi-hole FTL and reload Unbound as that
   runbook describes, not just a DNS cache flush.
3. Merge the homelab changes. ArgoCD discovers `app-fytorium` and syncs the
   database and app. Keep one replica and `Recreate`: workers assume one instance.
4. The Authentik worker applies the blueprint after the merge. Check that the
   application `fytorium` exists and that its issuer
   `https://auth.epaflix.com/application/o/fytorium/` answers
   `/.well-known/openid-configuration`.
5. The owner must add themselves to `Fytorium users` before the first login,
   because the group starts empty. The owner logs in first. The first login
   creates the household and makes that user owner, so do not let a family
   member or sitter claim it first.
6. Add family or sitters to the Authentik group `Fytorium users` before inviting
   them through Fytorium's Members page.

## Local setup regression

Build the app repo's `tests/stack/Minio.Dockerfile` server stage first:

```sh
docker build -q --target server -t fytorium-test-minio:local \
  -f /path/to/epaflix-fytorium/tests/stack/Minio.Dockerfile \
  /path/to/epaflix-fytorium/tests/stack
python3 2-k3s/25.fytorium/minio-setup.test.py
docker image rm fytorium-test-minio:local
```

Run from the homelab repo root. The test uses fake keys, a throwaway MinIO
container and an isolated Docker network. It runs setup twice with the actual
cached or source-built deployment client, checks live `ps` and `docker top`
for secret leakage, verifies the policy attaches only once, checks app
put/get/delete and denied listing/other-bucket access, and removes its containers
and network afterward. Keep the `fytorium-mc` image for later setup runs.
`FYTORIUM_TEST_MINIO_IMAGE` overrides only the test server image.

## Updates and smoke check

After source CI publishes a new image, bump its immutable digest in
`kustomization.yaml`, bump `SERVER_VERSION` in `deployment.yaml` to the source
commit, and merge the homelab change. ArgoCD recreates the pod;
migrations run again before readiness. No mutable tag rollout or manual restart
is needed.

On a phone on the LAN, log in, add a site and plant, and complete a task. Leave
another task due, enable notifications on that phone and set the daily reminder
time in Household settings. Receive the push reminder and tap it to open Today.
Check app readiness and Prometheus scraping inside the cluster; `/metrics` must
not be reachable through the hostname, even on the LAN.
