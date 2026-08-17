<!-- Owned by ArgoCD Application "remote-pi". Rationale: document the relay's security, storage, backup, and rollback contract. -->
# Remote Pi relay

A self-hosted relay connects the Remote Pi mobile app to Pi processes. The
relay carries prompts, responses, tool events, and protocol metadata; it does
not synchronize repositories or other files. This stage supports an
interactive Pi process on the current workstation only. It does not provide an
always-on coding agent or cross-PC mesh.

## Internal-only network boundary

The only route is `https://remote-pi.epaflix.com` on Traefik's `internal`
entry point. The chart creates a dedicated `traefik-internal` LoadBalancer
Service at `192.168.10.102`; it exposes only port 443 and targets port 8443 on
the shared Traefik pod. It sets `allocateLoadBalancerNodePorts: false`, and its
port must have no `nodePort`, so no alternate endpoint is allocated on every
K3s node. The existing public Traefik Service at `192.168.10.101` keeps only
ports 22000, 80, and 443, and does not expose the `internal` entry point. The
relay's own Service remains `ClusterIP` on port 3000, with no NodePort,
LoadBalancer, or HTTP route.

The router forwards nothing to `192.168.10.102`. That unforwarded dedicated
address, together with explicit internal DNS, is the network boundary. A
source-address allow-list is deliberately absent: the shared LoadBalancer
path obscures client addresses, so such a rule would give false confidence.
The native client also has no reviewed browser/OIDC flow, so this route has no
Authentik middleware.

## Deployment behavior and Traefik rollback

The existing Traefik ArgoCD Application has automated sync, self-heal, and
prune enabled. Merging the Traefik values change to `main` therefore deploys
the `internal` entry point and `traefik-internal` Service at once; there is no
separate manual Traefik sync. The reviewed merge is the human deployment gate.
The new `remote-pi` child Application is different: the app-of-apps creates it
on merge, but its `syncPolicy: {}` keeps the relay resources unsynced until a
human explicitly approves their first sync.

Before merge, locally render the same pinned Traefik chart used by ArgoCD:

```bash
helm template traefik traefik \
  --repo https://traefik.github.io/charts \
  --version 41.1.1 \
  --namespace traefik-system \
  --values 2-k3s/05.traefik-deployment/values/traefik-values.yaml \
  >/tmp/remote-pi-traefik-rendered.yaml
```

Do not merge unless the render has `traefik-internal` on
`192.168.10.102:443`, `allocateLoadBalancerNodePorts: false`, no internal
`nodePort`, and the public `192.168.10.101` Service still has exactly ports
22000, 80, and 443.

**One-line Traefik rollback:** Revert the values commit and let automated ArgoCD sync restore the previous Traefik state.

Before the first manual Remote Pi ArgoCD sync, a human must:

1. Map `remote-pi.epaflix.com` to `192.168.10.102` in Pi-hole's managed
   dnsmasq file.
2. Exclude this hostname from public Cloudflare wildcard behavior with an
   exact DNS-only shadow record pointing to `192.168.10.102`.
3. Confirm the router has no port forward to `192.168.10.102`.

> **Step 2 was not applied when the relay landed.** Step 1 was done, step 2 was
> skipped. Without it the Cloudflare-proxied `*.epaflix.com` wildcard still
> covers this name, so `dig AAAA remote-pi.epaflix.com @192.168.10.30` answers
> with Cloudflare IPv6 addresses instead of nothing - the A record is
> overridden locally, the AAAA is not. It is being applied now under #868.
> The LAN is IPv4-only today, so no traffic was actually sent to Cloudflare;
> enabling IPv6 anywhere would have made it live. Publishing `192.168.10.102`
> adds no disclosure - `cliproxy.epaflix.com` already publishes it. Mechanism
> and verification: `.github/instructions/pihole.instructions.md` → "AAAA and
> the Cloudflare wildcard".

After sync, verify `/health` from LAN and WireGuard. From a genuinely external
network, test both normal DNS and a forced SNI/Host request to the router's
public origin. The normal path must be unreachable; the forced-origin path may
only fail to connect or return Traefik 404, never another route or relay
health. Internal or public DNS shadowing by itself is not a security boundary.

## Plaintext-at-relay trust model

Current protocol messages are not end-to-end encrypted. TLS protects traffic
on the network but terminates at Traefik. Traefik, the relay process, the relay
host administrator, or a compromised relay executable can inspect content
while it is routed. Self-hosting reduces who must be trusted; it does not make
prompts, responses, images, or tool events opaque to the relay. Do not put
secrets in prompts.

The relay's normal database stores signed membership metadata only, not message
or tool-event history. That limited persistence does not change the live
plaintext-at-relay trust boundary.

## Workload and image

The Deployment has exactly one replica and uses `Recreate` so two relay writers
never access SQLite together. It runs as a numeric non-root user with a
read-only root filesystem and writes only to `/data`.

The relay image is pinned to an immutable digest. Registry observations tie
that digest to the `v0.3.1` and `latest` tags, pushed 2026-08-12. `v1.0.0` and
`v1.0.1` remain older stale pushes from 2026-05-22, not a newer release. The
deployment does not use a mutable tag.

Upstream versions the relay and the pi extension as a matched pair and states
the upgrade order explicitly: **relay first, extension second**. An old
extension can consume the new relay's UUID error shape, so relay-first is the
safe direction. This digest is the relay half; the other half is npm
`remote-pi@0.7.0`, published 52 seconds after the image. There is no `0.6.x` on
npm at all - the extension stream jumps `0.5.5` -> `0.7.0`, so 0.7.0 is the
"extension 0.6" the upstream upgrade note refers to. Extension 0.7.0 carries a
one-release legacy wire-label shim, so a mixed fleet interoperates only while
every participant is upgraded in one window; do not leave it mixed.

This image is deliberately absent from the `images:` block in
`kustomization.yaml`, so Renovate does not track it. Upstream's tag stream has
shipped stale `v1.0.x` pushes that sort above the real releases, which makes an
unattended tag-driven bump unsafe. Bumps here are manual: resolve the new tag
to a digest, confirm the extension half is published, then pin the digest.

## Storage and backup

`remote-pi-data` is a 1 GiB `ReadWriteOnce` claim using the cluster's default
`local-path` StorageClass. It is mounted at `/data`, and the database path is
`/data/mesh.db`.

`local-path` is node-local and has a `Delete` reclaim policy. It is not a
replicated or retained backup: loss of the selected worker, the backing VM
disk, or the claim can lose the database.

### What `mesh.db` actually holds

One table, `mesh_versions`: per Owner, the latest **Owner-signed, monotonically
versioned** membership document (canonical JSON `blob` + Ed25519 `sig`). It is
~16 KiB. It is a relay-side **cache of a document the relay does not author**:

- The pi extension only ever issues `GET /mesh/<owner_hash>`. There is no PUT or
  POST anywhere in the extension - the document is published by the **Owner**
  (the paired mobile app), which holds the signing key.
- Phone-to-workstation pairing does **not** live here. Paired devices are in
  `~/.pi/remote/peers.json` on each machine, plus the device's own record.
  Wiping `mesh.db` does not unpair anything.
- The only consumer is cross-PC sibling discovery. On a missing record the read
  path is `if (!envelope) continue;` in `mesh/siblings.js` - a no-op yielding an
  empty topology, not an error.

### Policy: accept the loss (#831)

No automated backup. The volume is deliberately **not** added to a backup path,
because there is no PVC backup automation in this cluster to add it to - the
only backup automation that exists is `2-k3s/maintenance/backup-all-databases.sh`,
which is manual and PostgreSQL-only. The TrueNAS container-data snapshot in
`RECOVERY.md` was a one-off, not a running job. Building bespoke PVC backup
automation for the least critical stateful component in the cluster is not
proportionate.

This is a decision about a re-derivable cache, not about accepting data loss.
Recovery, in order:

1. **Do nothing.** With one PC in the mesh (`homePC`), an empty `mesh_versions`
   costs nothing: cross-PC sibling discovery is the only consumer, and this
   deployment is single-workstation by design (see the top of this README).
2. **Let the Owner republish.** The mobile app re-publishes its signed
   membership document on the next membership change (pair, revoke, rename).
   The version counter is the Owner's, so it resumes above the lost value.
3. **Re-pair** only if the Owner's own state is also gone.

The impact recorded when #831 was raised - "every paired device relationship it
knows disappears, so each phone and each machine has to pair again" - is
overstated: pairing state is not in this database.

### Residual risk accepted

The relay enforces the monotonic version floor from this table. Wiping it resets
that floor. A freshly started extension process sends no `since`, so until the
Owner publishes a higher version, a wiped relay would serve whatever it is given
- which means a captured older signed document could re-admit a revoked PC. The
attacker needs both a captured blob and write access to the relay, and the relay
is only reachable on the internal-only Traefik entry point. Accepted at this
scale. Revoke from the app after any relay data loss to force a version bump.

### Still required: cold copy before a change

"Accept the loss" covers *unplanned* loss. A *planned* image or schema change
still takes a cold copy first, because that is when a bad migration is most
likely and a 16 KiB file is free to keep. Stop the single relay and copy
`mesh.db`, or use SQLite backup semantics from a controlled one-shot utility
that mounts the claim. Do not make an arbitrary live file copy because SQLite
may have a transient rollback journal.

Cold-copy procedure, as used before the v0.2.3 -> v0.3.1 bump:

```bash
kubectl -n remote-pi scale deploy/remote-pi-relay --replicas=0
kubectl -n remote-pi wait --for=delete pod \
  -l app.kubernetes.io/name=remote-pi-relay --timeout=110s
# one-shot pod mounting the same claim read-only, then:
kubectl -n remote-pi cp remote-pi-backup:/data/mesh.db ./mesh.db
sha256sum ./mesh.db && sqlite3 ./mesh.db 'PRAGMA integrity_check;'
kubectl -n remote-pi delete pod remote-pi-backup
kubectl -n remote-pi scale deploy/remote-pi-relay --replicas=1
```

With the relay stopped, `/data` holds `mesh.db` alone - no `-wal`, `-shm`, or
rollback journal - so the copy is the whole database.

Keep the copy under the git-ignored `backups/remote-pi-relay/<timestamp>-cold/`
with its `sha256`. Restore is a straight file put-back into `/data` with the
relay scaled to `0`; verify with `PRAGMA integrity_check` before scaling up.
The v0.3.1 bump is the worked example: the post-upgrade file was byte-identical
to the pre-upgrade copy, confirming v0.3.1 runs no migration on this schema.

## Verification

After the approved first sync:

```bash
kubectl -n remote-pi get deploy,pod,service,pvc
kubectl -n remote-pi port-forward service/remote-pi-relay 3000:3000
curl --fail http://127.0.0.1:3000/health
kubectl -n traefik-system get service traefik-internal \
  -o jsonpath='{.spec.allocateLoadBalancerNodePorts}{"\n"}'
kubectl -n traefik-system get service traefik-internal \
  -o go-template='{{range .spec.ports}}{{if .nodePort}}{{.nodePort}}{{"\n"}}{{end}}{{end}}'
```

The first Traefik command must print `false`; the second must print nothing.
Also verify that the public Traefik Service still exposes only 22000, 80, and
443, the HTTPS endpoint works from LAN and WireGuard, the external-network
denial holds, one relay pod runs, the claim is bound, and data persists across
a deliberate pod replacement. Pair only after those checks pass.

## Rollback

For an image-only rollback, revert to the previously approved immutable digest.
`Recreate` stops the current pod before the previous image starts. Never roll
back by moving a mutable tag.

For a database or schema rollback, stop the Deployment, restore the matching
application-consistent `mesh.db` backup to the existing claim, then start the
previous image and verify health, membership behavior, and external denial.
If no usable backup exists, pair clients again.

For full removal, use this explicit sequence:

1. Stop clients and gate PVC retention before any prune or deletion. A human
   must decide whether to retain the live claim or delete it, and preserve a
   final application-consistent encrypted database backup first. If retaining
   the claim, exclude both the PVC and its Namespace from every prune or delete.
2. Land reviewed Git changes for the relay-resource removal while keeping the
   child Application available long enough to perform the removal. Because the
   child uses manual sync, Git changes alone do not remove live relay resources:
   manually sync it with prune, or explicitly delete the relay resources. Apply
   the approved PVC decision; delete the claim only after explicit human
   approval because its `Delete` reclaim policy removes the provisioned volume.
3. After confirming the intended relay resources are gone, remove the child
   declaration through reviewed Git. The app-of-apps has prune disabled, so
   explicitly delete the orphaned `remote-pi` Application; Git removal alone
   leaves that Application live.
4. Separately revert the `internal` entry point and `traefik-internal` Service
   from the Traefik values. Automated ArgoCD sync performs this Traefik rollback;
   verify the internal Service and entry point are gone without treating it as
   removal of the manually managed relay child.

---

# CLIProxyAPI

A second, unrelated workload in this same namespace and this same ArgoCD
Application. It is not part of the relay and shares no data with it.

CLIProxyAPI (`https://help.router-for.me`) signs in to Gemini, Codex and Claude
accounts and re-exposes them behind OpenAI-, Anthropic- and Gemini-compatible
HTTP APIs, so a local client such as `omp` can point at one endpoint instead of
juggling provider SDKs. Tracking issue #858.

## Why it shares the namespace and the Application

Two reasons, both about the gate rather than about the software:

- The `remote-pi` child Application has `syncPolicy: {}`. Every change here
  waits for a human to press sync. That is the property we want for a proxy
  holding provider credentials, and it already exists - a new Application would
  mean a new app-of-apps entry and a second thing to keep on manual sync.
- Both workloads are internal-only and reachable through the same
  `traefik-internal` entry point on `192.168.10.102`. The network boundary
  documented above for the relay applies unchanged.

The cost is that a sync touches both workloads. With one replica each, `Recreate`
on both, and no shared storage, that is acceptable.

Layout is `2-k3s/17.remote-pi/cliproxy/`. The parent kustomization deliberately
has NO top-level `namespace:` transformer: two of the objects under `cliproxy/`
belong to `postgres-system`, not `remote-pi`.

## Postgres dependency

Durable state lives in the existing CNPG cluster, not on a PVC. The proxy picks
its storage backend from which env vars are present at startup, in this
precedence order:

`PGSTORE_DSN` > `OBJECTSTORE_ENDPOINT` > `GITSTORE_GIT_URL` > local files.

Only `PGSTORE_DSN` is set, so Postgres wins.

- Database `cliproxy`, role `cliproxy`, both declared as CNPG `Database` and
  `DatabaseRole` CRs in `cliproxy/database.yaml`, namespace `postgres-system`.
- The connection target is the service name
  `postgres-rw.postgres-system.svc.cluster.local:5432` with `sslmode=require`.
  Never a pod name: CNPG renumbers instances on failover and the primary is
  currently `postgres-cluster-10`, not `postgres-cluster-1`.
- The proxy creates its own tables on first start - `config_store` and
  `auth_store` - and seeds the config row from its bundled
  `config.example.yaml`. There is no migration step to run.
- After that it syncs both ways between Postgres and its local copy on disk, so
  an edit made through the management UI lands in Postgres and survives the pod.
- The password is one value in two places: the `kubernetes.io/basic-auth` Secret
  `cliproxy-db-role` that CNPG applies to the role, and `pg-password` in
  `cliproxy-secrets` that the pod interpolates into the DSN. Both are documents
  in `cliproxy/cliproxy-secrets.enc.yaml`, so a rotation is one edit. Let them
  drift and the pod cannot log in.
- `PGSTORE_SCHEMA` is left at its default `public`.

## Logs - two surfaces, and why only one of them is a file (#1007)

There are two entirely separate things called "logs" here, and they are answered
in two different places. Getting this wrong wastes an afternoon, so:

| Question | Where the answer is | Config |
|---|---|---|
| Which calls happened, when, status, duration | Loki / Grafana, 31-day retention | none - stdout, shipped by promtail |
| What was actually *sent and returned* | one file per request on the pod | `request-log: true` |

### Access lines: Grafana, not files

The proxy writes a gin access line per call to stdout and promtail already ships
it. Query it:

```
{namespace="remote-pi", container="cliproxy"}
```

Grafana Explore, Loki datasource (uid `P8E80F9AEF21F6940`, tenant
`X-Scope-OrgID: 1` - a manual `curl` without that header gets `no org id`):
<https://grafana.epaflix.com/goto/afvfszfwash6oc?orgId=default>

Each line carries the request ID in brackets, which is the join key to the next
section:

```
[2026-08-17 17:33:00] [2ea6fa4f] [info ] [gin_logger.go:97] 200 | 5.575s | 10.42.0.0 | POST "/v1/messages"
```

### Payloads: `request-log`, fetched by request ID

The prompt and the completion are **never** on stdout, at any log level, so no
promtail or Loki change can ever surface them. `request-log: true` writes one
file per call under `WRITABLE_PATH/logs` = `/var/lib/cliproxy/logs`, named
`<path>-<timestamp>-<requestID>.log`, containing the request body, the response
body, both header sets, and the upstream API request/response.

**Read them with `cliproxy/tools/cliproxy-payload.sh`** - a raw payload file is
1-4 MB of JSON and SSE frames, so reading one by eye is not a workflow:

```bash
cd 2-k3s/17.remote-pi/cliproxy/tools
./cliproxy-payload.sh list              # newest 10: request id, time, size
./cliproxy-payload.sh show 2ea6fa4f     # readable transcript
./cliproxy-payload.sh raw  2ea6fa4f /tmp/full.log
```

`show` prints the model, the upstream URL and which provider account served it,
the system prompt's first 300 chars, every message turn with tool calls and tool
results labelled, and the assistant's reply reassembled from the SSE deltas.
That last part is why the renderer walks `content_block_start` / `_delta` events
instead of regexing for `text_delta`: an agent turn is often only `thinking` plus
`tool_use`, which a text-only match reports as "no response body", i.e. exactly
wrong on the most common case.

The script reads the files off the pod with `kubectl exec cat` (no `kubectl cp` -
the image has no `tar`), so it needs no management key and no port-forward.
Override the cluster with `CLIPROXY_CONTEXT` / `CLIPROXY_NAMESPACE`.

The management API serves the same file over HTTP if you would rather not exec:

```bash
kubectl --context epaflix -n remote-pi port-forward deploy/cliproxy 8317:8317
curl -H "Authorization: Bearer <management key>" \
  http://127.0.0.1:8317/v0/management/request-log-by-id/2ea6fa4f
```

`GetRequestLogByID` checks only the log directory, **not** `logging-to-file`,
which is what makes the split above possible. There is no *list* endpoint - the
API only fetches by ID - which is the other reason the script exists.

Bearer tokens in the captured headers are already truncated by the logger
(`Authorization: Bearer omp-...db27`, `sk-a...dgAA`), so a payload file does not
leak a usable key. The prompt text is still the whole prompt.

### `logging-to-file` stays `false`, deliberately

It does not add a sink, it *moves* one: `ConfigureLogOutput` calls
`log.SetOutput(logWriter)` and gin's writers are the same logrus instance, so
turning it on takes the access lines off stdout. `kubectl logs` and Loki both go
dark, and 31 days of searchable history is traded for a 10Mi rotating file on an
emptyDir that dies with the pod. Its only benefit is the management UI's own
"Logs" tab, which returns `400 logging to file disabled` while this is `false`.
That is an acceptable loss. The verify block in `reconcile-config.psql` raises if
anything sets it to `true`.

### Bounds and caveats

- `logs-max-total-size-mb: 256` is **mandatory**, not cosmetic. Files are
  per-request, the `work` emptyDir is `sizeLimit: 512Mi` shared with the pgstore
  spool and the management SPA, and the container has
  `ephemeral-storage: 1Gi`. The cleaner
  (`internal/logging/log_dir_cleaner.go`) ticks every 60s and deletes the oldest
  `*.log` until the directory is back under the cap; it runs even with
  `logging-to-file` false. The shipped default is `0`, meaning unlimited.
- **How much history 256MB actually buys, measured (#1011):** one
  `/v1/messages` file is ~1.1MB, largest seen 2.9MB, because each carries the
  whole system prompt plus the conversation history. So the cap is ~230 requests
  - a few hours of active use, not days. The first attempt at `64` held ~55
  requests, roughly 20-30 minutes, which is short enough to have already deleted
  the evidence by the time you go looking. Re-measure with
  `du -sm /var/lib/cliproxy/logs` before assuming the current number is right,
  and if you raise it, raise the `work` emptyDir `sizeLimit` and the container's
  `ephemeral-storage` limit with it.
- Payload files are **ephemeral**. `strategy: Recreate` on an emptyDir means a
  pod replacement loses them. That is intentional - see the next point.
- Payload files contain **full prompt text**. Bearer values are truncated by the
  logger, so they are not a credential leak, but they stay on the pod on purpose
  and are not shipped to Loki - this repo has already had to purge
  credential-shaped lines out of a 31-day retention window (#634, #702, #824,
  #911), and a whole-prompt corpus is worse to have sitting in a searchable index
  than a single line. Do not "improve" this by tailing them into promtail.
- Neither `request-log` nor `logs-max-total-size-mb` has an environment variable
  (there is no `*LOG*` env in the binary at all), so both are reconciled into the
  Postgres config row by `reconcile-config.psql` - the #861 pattern. Toggling
  them in the management UI instead is a live-only change the next rebuild eats.

## Security posture - how this differs from the relay, and why

The relay pod next door runs as UID 1000 with `readOnlyRootFilesystem: true`.
The proxy pod does neither. This is deliberate and recorded, not an oversight:

The proxy is hardened to match the relay pod: `runAsNonRoot: true` as uid 1000, `readOnlyRootFilesystem: true`, every capability dropped, no privilege escalation, `seccompProfile: RuntimeDefault`, and no service account token.

It was not always. It first shipped as root with a writable root filesystem, on the assumption that the process rewrites `/CLIProxyAPI/config.yaml` in place and needs `/root/.cli-proxy-api`. That assumption was wrong for this deployment. With the Postgres backend it writes neither - both paths were absent on the running pod. Config and auth live in Postgres and are mirrored to `PGSTORE_LOCAL_PATH`, which is the mounted emptyDir.

Two things the hardening needs, and why:

- `HOME` is redirected to `/var/lib/cliproxy/home`. The image default is `/root`, mode 700 and owned by root, which uid 1000 cannot write.
- `/tmp` is its own emptyDir. With a read-only root filesystem anything writing a temp file fails without it.

This was proven before it was changed, not after. A throwaway probe Deployment ran the same image under the hardened settings with an isolated `PGSTORE_SCHEMA`, so it could not touch the live tables. It reached 1/1, logged `postgres-backed token store enabled`, downloaded the management SPA, and served `/management.html` with HTTP 200 as uid 1000 with a read-only rootfs. The probe and its schema were then deleted.

If a future image bump reintroduces a write outside the emptyDirs, the pod will CrashLoop rather than fail quietly. Check the logs for a permission error before assuming the image is broken.

What still holds the blast radius down: no service account token
(`automountServiceAccountToken: false`), no host mounts, all capabilities
dropped, `allowPrivilegeEscalation: false`, `seccompProfile: RuntimeDefault`,
and an `ephemeral-storage` limit of 2Gi that caps the writable container layer.
The `/var/lib/cliproxy` emptyDir (512Mi) holds the Postgres spool and the
downloaded management SPA so neither grows that layer.

The spool path is set with `PGSTORE_LOCAL_PATH`. Some upstream docs call it
`PGSTORE_SPOOL_DIR`, which this build ignores - that was #867, and it meant the
spool sat in the image WORKDIR for the first day. The name was checked against
the binary rather than the docs:

```bash
POD=$(kubectl --context epaflix -n remote-pi get po \
  -l app.kubernetes.io/name=cliproxy -o jsonpath='{.items[0].metadata.name}')
kubectl --context epaflix -n remote-pi exec "$POD" -- \
  sh -c "grep -oa 'PGSTORE_[A-Z_]*' /CLIProxyAPI/CLIProxyAPI | sort -u"
```

On `v7.2.123` that returns exactly `PGSTORE_DSN`, `PGSTORE_LOCAL_PATH` and
`PGSTORE_SCHEMA`. A wrong name fails silently - there is no error, the spool just
lands in the WORKDIR. After any image bump, check the startup log line:

```bash
kubectl --context epaflix -n remote-pi logs deploy/cliproxy | grep 'workspace path'
```

It must say `/var/lib/cliproxy/pgstore`.

`PGSTORE_LOCAL_PATH` is a base directory - the binary appends `pgstore/` to
whatever it is given. So the value is `/var/lib/cliproxy` and the spool lands at
`/var/lib/cliproxy/pgstore`. Setting the full path gave
`/var/lib/cliproxy/pgstore/pgstore`, which worked but read wrong.

Probes are `tcpSocket` on 8317 on purpose. `/v1/*` needs a client API key, and
`/management.html` can legitimately 404 before the SPA download finishes, so
neither is an honest health signal.

## First boot - bootstrap over port-forward

Out of the box `remote-management.allow-remote` is `false`, which gates every
non-localhost caller. So the first configuration pass has to happen through a
port-forward, not through `cliproxy.epaflix.com`:

```bash
kubectl -n remote-pi port-forward deploy/cliproxy 8317:8317
# then open http://127.0.0.1:8317/management.html
```

In that session, do three things:

1. Set `remote-management.allow-remote` to `true`, otherwise the internal
   hostname stays useless.
2. Confirm the management key. `MANAGEMENT_PASSWORD` comes from the
   `management-password` key of `cliproxy-secrets`. If a plaintext
   `remote-management.secret-key` is present in `config.yaml`, the process
   rewrites it in place as a bcrypt hash on first start - so a later `grep` of
   that file showing a hash is normal, not corruption.
3. Add the client-facing `api-keys:` list. That list is the only thing standing
   between a caller on the LAN and your provider accounts. There is no env var
   for it - it lives in `config.yaml`, which means in Postgres after first sync.

The seeded config ships three example keys. Until you replace them the proxy
logs `unsafe example API key configured; proxy API endpoints disabled until
api-keys is updated` and every `/v1/*` call returns `403`, even with a correct
key. Replacing them:

```bash
kubectl --context epaflix -n remote-pi port-forward deploy/cliproxy 8317:8317
# the endpoint takes a BARE JSON ARRAY - this is easy to get wrong
curl -X PUT http://127.0.0.1:8317/v0/management/api-keys \
  -H "Authorization: Bearer <management key>" \
  -H 'Content-Type: application/json' \
  --data '["<client key>"]'
```

`{"api-keys":[...]}` and `{"keys":[...]}` both return `400 invalid body`, and
`PATCH` returns `400 missing fields`. Only a bare array works on `v7.2.123`.

The current key is kept in `cliproxy-secrets.enc.yaml` under `omp-api-key` so a
rebuild does not lose it. Nothing consumes that value automatically yet - it is
still typed in through the management API by hand. See #861 for seeding it
properly.

### Known limitation - provider OAuth needs a port-forward every time

The web UI's provider login flow only completes against `localhost` /
`127.0.0.1`. The redirect it hands the provider is a loopback URL, and the
provider will not accept `cliproxy.epaflix.com` in its place. So this is not a
one-off bootstrap quirk:

**Every time an account is added or re-authorised, you have to run the
port-forward above and drive the flow from `127.0.0.1`.** Normal day-to-day use
over the internal hostname is unaffected.

### `management.html` 404 is not automatically a broken deployment

The management SPA is not baked into the image. It is downloaded from GitHub
releases at startup into `MANAGEMENT_STATIC_PATH`
(`/var/lib/cliproxy/static`). Two consequences:

- The pod needs egress to GitHub at start. No egress, no UI - while the proxy
  API itself still works.
- Right after a restart, `/management.html` can 404 for a few seconds until the
  download lands. Check the pod log before concluding anything is wrong.

## pi-bridge plugin (quota for the Pi extension)

[`pi-bridge`](https://github.com/abix5/pi-cliproxyapi-bridge) is a native
CLIProxyAPI plugin that serves provider quota and the model catalogue to the
`pi-cliproxyapi` Pi extension (0.4.0+) using **the same client API key already
used for model calls**. It replaces the standalone `pi-cliproxyapi-wellknown`
sidecar, which we never deployed, so there is nothing to decommission here.

Routes, on the existing host-only IngressRoute, no reverse-proxy change:

```
GET https://cliproxy.epaflix.com/v0/resource/plugins/pi-bridge/capabilities
GET https://cliproxy.epaflix.com/v0/resource/plugins/pi-bridge/well-known
GET https://cliproxy.epaflix.com/v0/resource/plugins/pi-bridge/usage
Authorization: Bearer <one of the api-keys entries>
```

They are **resource** routes, not management routes: they are not behind the
management key, and the plugin authorizes callers against the same `api-keys`
list the proxy already accepts. An unknown key fails closed with `401`. Handing
Pi the management key instead would have given a model client full admin.

### How it is installed here - and why not through the store button

The panel's **Plugins → Store** install button writes a `.so` into `plugins.dir`
and pins a version in `config.yaml`. Neither survives here: the root filesystem
is read-only (#862) and there is no PVC. So git owns both halves:

| Half | Where | Mechanism |
| --- | --- | --- |
| the artifact | `plugins` emptyDir at `/CLIProxyAPI/plugins` | `fetch-pi-bridge` initContainer |
| the config | `plugins:` block of the config row in Postgres | `reconcile-config.psql` |

The version is pinned in **exactly one place**, `VERSION` in
`cliproxy/files/fetch-pi-bridge.sh`, together with the sha256 of the release zip
from its own `checksums.txt`. The config block deliberately sets no
`store.version`: with a single `.so` present the host loads that one, verified
on v7.2.127. A bump is a one-line edit plus the new checksum.

The initContainer **fails closed on a checksum mismatch and open on an
unreachable GitHub**. A tampered artifact must never load; a GitHub outage must
not take the proxy down for an optional quota endpoint. If it logs the warning,
the proxy runs and the four routes 404 until the next pod start succeeds.

Upstream builds the plugin against SDK `v7.2.93` while we run `v7.2.127`, and
its README says the versions must match. They do not have to: the release `.so`
was loaded and exercised against the exact `v7.2.127` digest pinned in
`kustomization.yaml` - under `runAsNonRoot` + `readOnlyRootFilesystem`, the same
posture as the pod - before any of this was committed. After an image bump,
re-check the startup log for `pluginhost: plugin loaded plugin_id=pi-bridge`;
a rejected plugin logs `pluginhost: failed to load plugin` and does not stop the
proxy, so it will not announce itself any other way.

### Verification

```bash
kubectl --context epaflix -n remote-pi logs deploy/cliproxy -c fetch-pi-bridge
kubectl --context epaflix -n remote-pi logs deploy/cliproxy | grep pluginhost
curl -sS -H "Authorization: Bearer <client api key>" \
  https://cliproxy.epaflix.com/v0/resource/plugins/pi-bridge/capabilities
```

`capabilities` returning `"plugin":"pi-bridge"` with the running `cpaVersion` is
the end-to-end check. `usage` with an empty `accounts` array means the plugin
works but no provider account is authorised yet - same signal as an empty
`/v1/models`.

On the Pi side nothing needs configuring: `pi-cliproxyapi` finds the plugin on
the endpoint it already has in `~/.pi/agent/pi-cliproxyapi/config.json`.

### Rollback

Revert the commit and let the next pod start drop it. The plugin is only loaded
because `plugins.enabled` is `true` in the config row, so a reverted
`reconcile-config.psql` does **not** disable it by itself - config lives in
Postgres, and that script only moves values forward. To disable it live, set
`plugins.configs.pi-bridge.enabled` to `false` in the management UI, or delete
the `plugins:` edits there; the pod picks the change up by fsnotify without a
restart.

## Backup and restore

There is no separate backup job for this workload. Its state is rows in the
`cliproxy` database, so it rides the existing CNPG `ScheduledBackup` for
`postgres-cluster` together with every other database in that cluster.

Restoring means restoring the CNPG cluster, not this Deployment. Note one thing
before relying on that: `auth_store` holds provider tokens, and providers expire
and rotate them independently of our backup schedule. A restore that is more
than a little stale will bring back tokens the provider has already invalidated,
and those accounts need re-authorising through the port-forward flow above. Plan
for re-auth as the normal outcome of a restore, not as a failure.

## Rollback

Image rollback: revert the `newTag: vX.Y.Z@sha256:...` entry in
`kustomization.yaml` to the previously approved pin, merge, and manually sync
the Application. `Recreate` stops the current pod before the previous image
starts. Never roll back by moving a mutable tag.

Config rollback: config lives in Postgres, not in git, so reverting a commit
does not undo a management-UI change. Fix it in the UI, or restore the database.

Full removal: `databaseReclaimPolicy` and `databaseRoleReclaimPolicy` are both
`retain`. Deleting the CRs, or pruning them out of git, leaves the `cliproxy`
database and role in place - by design, so an accidental prune cannot drop
provider tokens. Dropping them for real is a deliberate manual step against the
primary, and worth doing only after confirming nothing else needs the data.

## `omp` client config

`omp` takes custom providers as a named block in `~/.omp/agent/models.yml`, not
as environment variables. Register the proxy as its own provider so it sits
beside any other provider instead of replacing the built-in `openai` one. The
OpenAI-compatible surface is under `/v1`:

```yaml
# ~/.omp/agent/models.yml
providers:
  cliproxy:
    baseUrl: https://cliproxy.epaflix.com/v1
    api: openai-completions
    apiKey: <one of the api-keys entries>
    models:
      - id: <model id the proxy advertises>
        name: <display name>
        contextWindow: <int>
        maxTokens: <int>
```

Fill `models:` from what the proxy actually advertises, never from a guess:

```bash
curl -H 'Authorization: Bearer <api key>' https://cliproxy.epaflix.com/v1/models
omp models cliproxy
```

`omp models cliproxy` listing the proxied models is the check that the whole
chain works - DNS, Traefik internal entry point, the client API key, and at
least one authorised provider account behind it. An empty list with a 200 means
the key is fine but no provider account is authorised yet, so `models:` stays
empty until an account is added.

To make it the default without the interactive picker, add to
`~/.omp/agent/config.yml`:

```yaml
modelRoles:
  default: cliproxy/<model id>
```

Do not set `modelRoles.default` while the proxy advertises no models.

Requires the Pi-hole record for `cliproxy.epaflix.com` (see
`.github/instructions/pihole.instructions.md`) and the Cloudflare DNS-only
shadow record, same two prerequisites as `remote-pi.epaflix.com`.
