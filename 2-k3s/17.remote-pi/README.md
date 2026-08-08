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
that digest to the `v0.2.3` and `latest` tags. There is no observed `v0.3.0`
tag; `v1.0.0` and `v1.0.1` are older stale pushes. The deployment does not use
a mutable tag and does not describe this artifact as relay 0.3.

## Storage and backup

`remote-pi-data` is a 1 GiB `ReadWriteOnce` claim using the cluster's default
`local-path` StorageClass. It is mounted at `/data`, and the database path is
`/data/mesh.db`.

`local-path` is node-local and has a `Delete` reclaim policy. It is not a
replicated or retained backup: loss of the selected worker, the backing VM
disk, or the claim can lose the database. The database contains signed
membership metadata only. Recovery after loss means restoring a backup or
pairing again.

No automated application-consistent backup is created by these manifests.
Before an image or schema change, either stop the single relay and copy
`mesh.db` to an encrypted backup destination, or use SQLite backup semantics
from a controlled one-shot utility that mounts the claim. Do not make an
arbitrary live file copy because SQLite may have a transient rollback journal.
Test restoration before relying on a backup.

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

## Security posture - how this differs from the relay, and why

The relay pod next door runs as UID 1000 with `readOnlyRootFilesystem: true`.
The proxy pod does neither. This is deliberate and recorded, not an oversight:

- `readOnlyRootFilesystem: false`. The process rewrites
  `/CLIProxyAPI/config.yaml` in place, inside its own WORKDIR next to the
  binary. That is how it converts a plaintext `remote-management.secret-key`
  into a bcrypt hash on first start, and how management-UI changes persist. On
  a read-only root filesystem that write fails.
- Runs as the image default, which is root. The auth directory is
  `/root/.cli-proxy-api`. The only way to move it is the `auth-dir` key in
  `config.yaml` - which the process does not read until after it has booted and
  written that file. There is no ordering that lets us relocate it to a non-root
  home before first start.

What still holds the blast radius down: no service account token
(`automountServiceAccountToken: false`), no host mounts, all capabilities
dropped, `allowPrivilegeEscalation: false`, `seccompProfile: RuntimeDefault`,
and an `ephemeral-storage` limit of 2Gi that caps the writable container layer.
The `/var/lib/cliproxy` emptyDir (512Mi) holds the Postgres spool and the
downloaded management SPA so neither grows that layer.

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
