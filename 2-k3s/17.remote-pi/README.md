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
accounts, plus GitHub Copilot through a plugin (see below), and re-exposes them
behind OpenAI-, Anthropic- and Gemini-compatible
HTTP APIs, so a local client such as `omp` can point at one endpoint instead of
juggling provider SDKs. Tracking issue #858. Two further upstreams arrive as
`openai-compatibility` providers rather than as accounts: the LAN Ollama on
TrueNAS, and OpenRouter (see "The OpenRouter provider" below).

## The OpenRouter provider

OpenRouter is a pay-per-token `openai-compatibility` upstream, reconciled into
the config row by `reconcile-config.psql` like everything else git owns. Eight
models, all aliased with an `or-` prefix:

| alias | upstream model |
| --- | --- |
| `or-glm-5.3-flash` | `z-ai/glm-5.3-flash` |
| `or-glm-5.3` | `z-ai/glm-5.3` |
| `or-gemini-3.7-flash` | `google/gemini-3.7-flash` |
| `or-deepseek-v4-flash` | `deepseek/deepseek-v4-flash-0731` |
| `or-deepseek-v4-pro` | `deepseek/deepseek-v4-pro-0813` |
| `or-qwen3.8-max` | `qwen/qwen3.8-max` |
| `or-qwen3.8-27b` | `qwen/qwen3.8-27b` |
| `or-minimax-m3` | `minimax/minimax-m3` |

What is deliberately absent is as load-bearing as what is present. No
`anthropic/*`, `openai/*`, `x-ai/*` or `moonshotai/*` model is routed here: this
proxy already serves those from subscription accounts, and adding them through
OpenRouter would bill per token for capacity already paid for flat-rate. The
`or-` prefix exists so that an OpenRouter model can never take a model ID that a
native account owns - the same problem `excluded_model_prefixes` solves for the
Copilot plugin, solved here by naming instead, because an `openai-compatibility`
provider has no prefix-exclusion setting.

Unlike the ollama entry, whose api-key is a placeholder because Ollama does not
authenticate, this provider's key is a real credential. It lives as
`openrouter-api-key` in `cliproxy/cliproxy-secrets.enc.yaml` and reaches the
reconcile initContainer as `OPENROUTER_API_KEY`. Note that it lands in the
config row in cleartext: that row is where CLIProxyAPI keeps every provider
credential, so this is the existing posture rather than a new exposure.

**Rotation is a one-file edit.** Change the value in the encrypted Secret and
sync; the reconcile rewrites the `api-key` line in the config row to match on
the next pod start. Presence of the provider is deliberately not treated as
proof that its credential is current - an earlier draft of this block returned
early on `- name: "openrouter"` and would have left a superseded key live
forever. The final verification block compares the stored credential against the
one git holds and fails the initContainer if they differ.

The key is read with a psql backtick `\set` and assigned under `\o /dev/null`,
because `set_config()` returns the value it sets and a bare `SELECT` would print
the API key into the pod log, which promtail ships to Loki on 31-day retention.
That is the #634/#702/#824/#911 failure mode and it was caught by running the
script, not by reading it.

Use `api: openai-completions` for these on the client - see the endpoint trap
below, which applies to the `gpt-5.6-*` family and not to these.

One gotcha that imitates that trap without being it. Both GLM entries have
mandatory reasoning at `max` effort, so reasoning tokens are drawn from the same
budget as the answer. Measured on `z-ai/glm-5.3`:

```text
max_tokens=32   finish_reason=length  content=''    reasoning_tokens=34
max_tokens=512  finish_reason=stop    content='OK'  reasoning_tokens=45
```

An HTTP 200 with empty content from `or-glm-5.3` is a `max_tokens` that the
model spent thinking, not a broken route. Check `finish_reason` before assuming
the provider is at fault.

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

## Sync cliproxy's credential contract as one unit

Three resources share one contract: a credential, the script that consumes it,
and the wiring between them.

| resource | carries |
| --- | --- |
| `Secret/cliproxy-secrets` | the credential |
| `ConfigMap/cliproxy-scripts` | the script that consumes it |
| `Deployment/cliproxy` | the `env` that delivers it to the initContainer |

Selective sync is fine in general - a Secret-only rotation or an unrelated
Deployment edit is safe, because each still satisfies the contract the others
expect. The rule is narrower: **when one change spans that contract, sync every
resource it touches at the same revision.** Adding a provider does span it, so
all three go together.

Get the scope from a `status.resources` read taken **after** the merge. A
pre-merge `OutOfSync` list describes the old `main` and silently under-reports
every resource the merge is about to change - which is exactly how #1149 went
wrong, and cost nine minutes of downtime.

```bash
argocd app sync remote-pi \
  --resource :Secret:remote-pi/cliproxy-secrets \
  --resource :ConfigMap:remote-pi/cliproxy-scripts \
  --resource apps:Deployment:remote-pi/cliproxy
```

### A partial sync does not fail, it arms

This is the part worth internalising. `configMapGenerator` sets
`disableNameSuffixHash: true`, so the ConfigMap has a stable name and syncing it
changes no pod spec field. **Nothing rolls.** The running pod keeps its old
script, stays healthy, and the cluster looks correct.

The mismatch surfaces only at the next pod replacement - a node drain, an
eviction, an image bump, a `kubectl rollout restart`. `reconcile-config` then
starts under the new script without the env the new script requires, raises,
and because this Deployment is `Recreate` at one replica the old pod is already
gone:

```text
ERROR:  OPENROUTER_API_KEY is empty - the cliproxy-secrets key did not reach the initContainer
```

So the window between an incomplete sync and the outage is unbounded, and the
detonation is usually unattended. In #1149 it was a deliberate restart two
minutes later, with someone watching. That was luck, not design.

Two things that do NOT protect you here, both checked:

- **A Reloader annotation.** It is the obvious fix for the no-rolling-trigger
  problem above, and it is the wrong one: the missing piece was the Deployment,
  not the trigger, so Reloader would only have reached the same crash sooner and
  with nobody watching. (A manual `kubectl rollout restart deployment/cliproxy`
  is still needed after any change to `cliproxy/files/`.)
- **A dry run against a copy of the live config row.** One was done for #1149
  and passed, because the operator supplied the key by hand. It exercised the
  script against real data and never exercised the delivery of the secret, which
  is the half that was about to be missing.

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

## Logs - one pane, and how the messages get there (#1007, #1016)

Everything is in Grafana. That took three tries to get right, so the shape is
recorded here along with what was rejected:

| Question | Where | How it gets there |
|---|---|---|
| Which calls happened, when, status, duration | Loki / Grafana, 31 days | proxy stdout, promtail |
| What was said - prompt, reply, tool calls | Loki / Grafana, same query | `transcripts` sidecar, one JSON line per request |
| The complete raw payload of one call | the pod, last ~230 requests | `request-log: true`, read with `cliproxy/tools/cliproxy-payload.sh` |

One query gets both halves of a single call:

```
{namespace="remote-pi"} | json | request_id = "f4b1dfdc"
```

And the transcripts alone, newest first:

```
{namespace="remote-pi", container="transcripts"} | json
```

Useful fields on the JSON line: `request_id`, `model`, `account`,
`upstream_status`, `turn_role`, `turn`, `turn_tools`, `reply`, `reply_tools`,
`message_count`, `prompt_chars`, `payload_file`, `payload_bytes`. The last two
let you jump from a Grafana line to the full file on the pod;
`message_count` / `prompt_chars` are context growth per call, which nothing else
here exposes.

### Why the sidecar emits a delta and not the file

An agent replays the entire conversation on every call, so a payload file is
~1.1MB and almost all of it is history Loki already has. At the observed rate -
106 `/v1/messages` in one hour - shipping files whole would be **~2.8GB/day
against a 15Gi Loki PVC**, dead inside a week. Only the last message is new, so
the sidecar emits that plus the reply: **~1KB per request**, ~190MB per 31-day
window. Measured, not estimated: a 2,081,868-byte payload produced a 900-byte
line.

Two implementation facts that are load-bearing:

- **The emitted line starts with an ISO timestamp.** promtail's multiline stage
  uses `firstline: '^\d{4}-\d{2}-\d{2}|^[A-Z]{1}\d{4}'`, so a bare `{...}` would
  be glued onto the previous log entry and corrupt both.
- **No promtail or Loki change was needed.** promtail scrapes every container in
  a pod, which `loki-0` already proves by shipping both `loki` and
  `loki-sc-rules` as separate `container` values.

The sidecar is a native sidecar (`initContainer` + `restartPolicy: Always`, GA
since 1.29; this cluster is v1.35.5+k3s1) so it is running before the proxy
serves its first request, and it dies with the pod. It mounts the work volume
**read-only** - it reports on payloads, it must not be able to damage them.

### What is redacted, and what is not

CLIProxyAPI truncates bearer values itself (`Bearer omp-...db27`), so a payload is
not a credential leak on its own. The risk is the other direction: a prompt or a
tool result that quotes a secret. Everything the sidecar emits passes through
`payload_lib.redact()` first - `sk-ant-`, `sk-`, `ghp_`, `github_pat_`, `xox*-`,
`AKIA`, `AIza`, `omp-`, `Password=`, `"token"/"secret"/"api_key":` and PEM private
key blocks. Prefix-anchored on purpose: an entropy heuristic would silently eat
legitimate output, and a transcript nobody can read is worthless.

**This is a bound, not a guarantee.** A secret in an unusual format reaches a
31-day searchable index. That trade was accepted deliberately in exchange for one
pane; #602 is what the cost looks like when it goes wrong.

### Access lines: Grafana, not files

The proxy writes a gin access line per call to stdout and promtail already ships
it. Query it:

```
{namespace="remote-pi", container="cliproxy"}
```

Grafana Explore, Loki datasource (uid `P8E80F9AEF21F6940`, tenant
`X-Scope-OrgID: 1` - a manual `curl` without that header gets `no org id`):
<https://grafana.epaflix.com/goto/afvfszfwash6oc?orgId=default>

Each line carries the request ID in brackets. That ID is the join key to the
transcript line and to the payload file - it is the same value in all three:

```
[2026-08-17 17:33:00] [2ea6fa4f] [info ] [gin_logger.go:97] 200 | 5.575s | 10.42.0.0 | POST "/v1/messages"
                       ^^^^^^^^
```

### Payloads: `request-log`, fetched by request ID

The prompt and the completion are **never** on stdout, at any log level, so no
promtail or Loki change can ever surface them. `request-log: true` writes one
file per call under `WRITABLE_PATH/logs` = `/var/lib/cliproxy/logs`, named
`<path>-<timestamp>-<requestID>.log`, containing the request body, the response
body, both header sets, and the upstream API request/response.

For day-to-day use you want the transcript line in Grafana, not this. Reach for
the raw file when you need what the delta drops: the full history, every header,
the upstream request as sent, or a reply longer than the 4000-char cap.

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
- **How much history 256MB actually buys, measured at steady state (#1013):**
  **~23 hours, about 440 files.** Two samples three minutes apart on a pod with
  37h uptime:

  ```
  260 MB  448 files  oldest=v1-messages-2026-08-22T184941-22b75fdd.log
  257 MB  439 files  oldest=v1-messages-2026-08-22T185540-d432c7cc.log
  ```

  The oldest file advances while the count falls, which is the cleaner evicting
  oldest-first; the directory oscillates at 257-260MB against the cap. Mean file
  size is **~0.59MB**, not the ~1.1MB that #1011 extrapolated from a 7-file
  sample taken minutes after rollout - so the earlier "~230 requests, a few
  hours, not days" estimate in this bullet was roughly 2x pessimistic. A full
  day of history means "something broke this morning" is still answerable, which
  is what the cap exists for. Largest single file seen remains 2.9MB.

  The first attempt at `64` held ~55 requests, roughly 20-30 minutes, short
  enough to have already deleted the evidence by the time you go looking.
  Re-measure with `du -sm /var/lib/cliproxy/logs` before assuming the current
  number is right - throughput sets the retention, so a busier week shortens it
  - and if you raise it, raise the `work` emptyDir `sizeLimit` and the
  container's `ephemeral-storage` limit with it.
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
and an `ephemeral-storage` limit of 1Gi that caps the writable container layer.
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
   `management-password` key of `cliproxy-secrets`. A human reads it from the
   credential store:
   `sops -d --extract '["cliproxy_management_password"]' .github/instructions/secrets.enc.yaml`
   (added by PR #1071). Rotate via SOPS plus a pod restart, never through the
   management UI - a UI-side change writes only a bcrypt hash into the Postgres
   `config_store` row and silently orphans both the Secret and the store copy
   (`reconcile-config.psql` does not touch `remote-management.secret-key`). If a plaintext
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
rebuild does not lose it. The same value also sits in the credential store as
`cliproxy_api_key` (PR #1071) - one value under two names, so a rotation must
update both. Nothing consumes that value automatically yet - it is
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

GitHub Copilot is the one exception: it uses a device code, not a loopback
redirect, so it authorises over `cliproxy.epaflix.com`. See the Copilot section
below.

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

The version is pinned in **exactly one place**, the `fetch-pi-bridge`
initContainer's `args` in `cliproxy/deployment.yaml`, together with the sha256 of
the release zip from its own `checksums.txt`. The fetch logic itself lives in the
shared `cliproxy/files/fetch-plugin.sh`, which both plugin initContainers call
with `<id> <version> <sha256> <owner/repo>`. The config block deliberately sets
no `store.version`: the host loads the `.so` that is present, verified on
v7.2.140.

### Bumping a plugin, and why it is half automated (#997)

Renovate watches both plugin releases through a regex custom manager over the
`# renovate:` marker above each version arg. It opens the PR and rewrites the
version. It **cannot** rewrite the sha256 on the next line, because nothing can
compute a release zip's checksum from a version number.

That asymmetry is dangerous rather than merely annoying, so it is fenced twice:

- A `packageRule` blocks auto-merge for both plugin packages. It has to sit after
  the repo-wide patch auto-merge rule, since later rules win. Without it a
  `0.9.1 -> 0.9.2` patch bump would auto-merge with a stale checksum, and
  `fetch-plugin.sh` fails closed with exit 1, which fails the initContainer and
  stops the proxy. Not a degraded plugin, a dead proxy.
- The `validate` gate step "CLIProxyAPI plugin pins match upstream checksums"
  downloads each release's own `checksums.txt` and compares. A stale checksum is
  a red check, so the outage above cannot be merged. A 404 is also fatal, because
  `fetch-plugin.sh` fails *open* on one and would leave the plugin silently
  absent. Any other network error is a warning, so a GitHub outage does not
  redden the gate for unrelated PRs.

So a bump is: let Renovate open the PR, copy the value from the release's
`checksums.txt` into the args, push it to the Renovate branch, watch the gate go
green, merge, then sync. The gate is the thing that makes the manual half safe;
do not disable it to "unblock" a bump.

The release URL is composed by the script from `<owner/repo>`, the version and
the plugin id, rather than passed in. Both projects publish
`.../releases/download/v<version>/<id>_<version>_linux_amd64.zip`, and the point
is that the version then appears exactly once per plugin: a regex manager
rewrites one captured span, so a version repeated in a tag, a filename and an arg
would come out inconsistent. A future plugin that names its asset differently
will 404 and fail open, which looks exactly like a GitHub outage, so check the
release page before blaming the network.


The initContainer **fails closed on a checksum mismatch and open on an
unreachable GitHub**. A tampered artifact must never load; a GitHub outage must
not take the proxy down for an optional quota endpoint. If it logs the warning,
the proxy runs and the four routes 404 until the next pod start succeeds.

Upstream builds the plugin against SDK `v7.2.93`, far behind whatever
`kustomization.yaml` currently pins, and its README says the versions must
match. They do not have to: the release
`.so` was loaded and exercised against the pinned digest - under `runAsNonRoot` +
`readOnlyRootFilesystem`, the same posture as the pod - before any of this was
committed, first on v7.2.127 and re-checked on v7.2.140 when the Copilot plugin
landed, both plugins loaded together. After an image bump,
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

## GitHub Copilot provider (`gpt-5.6-sol`)

Copilot is **not** a native provider in the CLIProxyAPI we run. Upstream keeps
its Copilot integration in the separate CLIProxyAPIPlus product. What is
deployed here is
[`arthur-sommer-etc/cliproxyapi-copilot-plugin`](https://github.com/arthur-sommer-etc/cliproxyapi-copilot-plugin)
(MIT), a third-party plugin against the official plugin ABI. It registers an
AuthProvider (GitHub device-code OAuth), a ModelProvider (discovery from
Copilot's `/models`), and an executor, and it is the only reason `gpt-5.6-sol`
appears in `/v1/models`. That model is always routed to Copilot's `/responses`
endpoint; the plugin carries the Claude Messages to OpenAI Responses bridge that
the official translators do not have.

`gpt-5.6-terra`, which the plugin also claims, is not new here. The Codex account
already serves `gpt-5.6-terra` and `gpt-5.6-luna`, so Copilot is the second
source for that name, not the only one. `gpt-5.6-sol` is the one model this
plugin actually adds to the catalogue.

Measured on the live deployment after authorising one Copilot account: the model
count went from 27 to 52. The 25 additions, verbatim:

```text
gpt-3.5-turbo                    gpt-4o-2024-08-06
gpt-3.5-turbo-0613               gpt-4o-2024-11-20
gpt-4                            gpt-4o-mini
gpt-4-0125-preview               gpt-4o-mini-2024-07-18
gpt-4-0613                       gpt-5-mini
gpt-4-o-preview                  gpt-5.3-codex
gpt-4.1                          gpt-5.4
gpt-4.1-2025-04-14               gpt-5.6-sol
gpt-41-copilot                   kimi-k2.7-code
gpt-4o                           mai-code-1-flash-picker
gpt-4o-2024-05-13                trajectory-compaction
text-embedding-3-small           text-embedding-ada-002
text-embedding-3-small-inference
```

No `claude-*` or `gemini-*` appeared, which is `excluded_model_prefixes` doing
its job.

Installed the same way as pi-bridge, for the same reason: the read-only rootfs
(#862) leaves nothing durable for the panel's store-install button to write to,
so git owns both halves.

The panel's Plugins -> Store **can** install this plugin, and that is in fact how
it first reached the live pod. `cliproxyapi-copilot` is in the official registry
(`CLIProxyAPI-Plugins-Store`) at the same 0.3.3 pinned here, and the button put
it at `/CLIProxyAPI/plugins/linux/amd64/cliproxyapi-copilot-v0.3.3.so`. It does
not survive: that directory is an emptyDir, and the only code path that
re-downloads a store plugin at startup is gated on `cfg.Home.Enabled`
(`sdk/cliproxy/home_plugins.go`), the router-for.me hosted control plane, which
this deployment does not use. The config half does survive, because config is a
Postgres row, so a restart without the initContainer leaves a config that enables
a plugin whose library is gone. The models then disappear with no error and no
CrashLoop. That is the failure this initContainer exists to prevent.

| Half | Where | Mechanism |
| --- | --- | --- |
| the artifact | `plugins` emptyDir at `/CLIProxyAPI/plugins` | `fetch-copilot-plugin` initContainer |
| the config | `plugins.configs` of the config row in Postgres | `reconcile-config.psql` |

It shares `cliproxy/files/fetch-plugin.sh` with pi-bridge; the id, version,
sha256 and `owner/repo` are the `fetch-copilot-plugin` initContainer's `args` in
`cliproxy/deployment.yaml`. Fails closed on a checksum mismatch, open on an
unreachable GitHub. Renovate watches this version too, and the same rule applies:
it bumps the version, you supply the checksum, and the gate blocks the pair from
diverging. See "Bumping a plugin" above.

The plugin's install doc says CLIProxyAPI derives the plugin id from the
filename, so that the id must match the `plugins.configs` key. That is not true
of this build, and it matters because it decides whether the `.so` can carry a
version suffix. Measured on v7.2.140 by loading the release as
`cliproxyapi-copilot-v0.3.3.so`: the host logged
`plugin_id=cliproxyapi-copilot version=0.3.3`, so the id comes from the plugin's
own metadata. The live pod has been demonstrating the same thing for pi-bridge
all along, with `pi-bridge-v0.9.1.so` reporting `plugin_id=pi-bridge`.

The library is 37.3 MiB. The `plugins` emptyDir went from 64Mi to 128Mi for it,
though 64Mi would in fact have held: peak during the fetch is pi-bridge's 10.0
MiB plus the 12.6 MiB zip plus the 37.3 MiB library, 60.0 MiB against 64.0. That
is 6% of headroom on a shared volume, which is not enough to leave alone.

### The version skew is fine, and that was measured

The release is built against SDK `v7.2.118`, well behind whatever
`kustomization.yaml` currently pins.
A Go plugin normally refuses to load across a package-version mismatch, so this
was proven before it was committed rather than assumed from the pi-bridge
precedent. The exact digest pinned in `kustomization.yaml` was run locally with
both plugins present, a local file store and no Postgres, and it logged:

```text
pluginhost: plugin loaded plugin_id=cliproxyapi-copilot version=0.3.3
pluginhost: plugin registered plugin_id=cliproxyapi-copilot plugin_name=GitHub Copilot subscription provider version=0.3.3
pluginhost: plugin loaded plugin_id=pi-bridge version=0.9.1
pluginhost: plugin registered plugin_id=pi-bridge plugin_name=pi-bridge version=0.9.1
```

`GET /v0/management/plugins` then reported `registered: true`,
`supports_oauth: true`, `oauth_provider: copilot`, and
`/v0/management/copilot-auth-url` returned a live `github.com/login/device`
URL. Re-check the `plugin registered` line after any image bump: a rejected
plugin logs `pluginhost: failed to load plugin` and does not stop the proxy, so
the only other symptom is Copilot models quietly missing from `/v1/models`.

### Config, and why it is two lines

```yaml
    cliproxyapi-copilot:
      enabled: true
      priority: 100
      excluded_model_prefixes:
        - "claude-"
        - "gemini-"
```

The live config row already holds exactly this, set through
`PATCH /v0/management/plugins/cliproxyapi-copilot/config` after the store button
wrote only `enabled: true`. So the block above is what `reconcile-config.psql`
seeds into a **fresh** database; on the current one its insert is skipped,
because the loop leaves an existing key alone rather than fighting the operator
for it. If you ever change these values in git, change them in the UI too, or
restore the database. That is the same one-way rule the top of this file states
for every config value here.

The live row also carries a `store:` sub-block the store install wrote: registry
id, repository, `release-tag: v0.3.3`, source URL. `"store": null` in a PATCH
does not remove it, the persist path puts it back for a store-installed plugin.
It is inert. Nothing in the load path reads it, the plugin's own `ParseConfig`
ignores unknown keys, and the plugin re-registered fine with it present. It is
not the `store.version` pin the pi-bridge section warns about; that warning is
about letting the store choose which `.so` to load, which is not happening here.

The plugin's install doc lists nine more fields (`github_client_id`, three base
URLs, three timeouts). Every one of them is already that exact value in the
plugin's `DefaultConfig()`, so writing them here would only create a second copy
to keep in sync. `excluded_model_prefixes` is the one that earns its place:
Copilot re-offers Claude and Gemini models, and the native Anthropic and Gemini
OAuth accounts own those model IDs here. Drop a line to let Copilot serve them
too.

The default GitHub OAuth client ID `Iv1.b507a08c87ecfe98` is the public VS Code
Copilot device-flow client. It is not a secret and there is no client secret, so
nothing here goes into SOPS.

### Authorising the account

Nothing below works until a human syncs the `remote-pi` Application. Its
`syncPolicy: {}` means merging these files changes nothing that is running, and
the same sync also replaces the proxy pod, so both plugin fetches and
`reconcile-config.psql` run in the new pod's init sequence.

That sync usually carries something this change did not ask for. Renovate keeps
bumping the `cli-proxy-api` pin and nothing syncs the Application, so the live pod
is normally several patch versions behind git, and the same click rolls the image
forward as well. Do not restate the two versions here - that is how this paragraph
came to name a live version and a pin that were both wrong. Read them off the
cluster instead, then check the upstream release notes for the span between:

```bash
kubectl --context epaflix -n remote-pi get pod -l app.kubernetes.io/name=cliproxy \
  -o jsonpath='{.items[0].spec.containers[?(@.name=="cliproxy")].image}'
grep -A1 'cli-proxy-api' 2-k3s/17.remote-pi/kustomization.yaml | grep newTag
```

What has NOT been done is a load test of the plugins against every pin. Both were
loaded locally at the two checkpoints named above, and Renovate's bumps since then
have not been exercised that way - CI only verifies each plugin's own release
checksum, not that it loads against the current CPA build. So the startup-log
check after a sync is the real safety net, not a prior guarantee: a plugin the
host rejects logs `pluginhost: failed to load plugin` and the proxy still starts,
which is why the four `pi-bridge` routes can 404 on a pod that looks healthy.

Then: device code, not a loopback redirect, so this is the one provider that does
not need the port-forward:

```bash
curl -sS -H "Authorization: Bearer <management key>" \
  https://cliproxy.epaflix.com/v0/management/copilot-auth-url
# open the returned github.com/login/device URL, approve the code, then poll
# no faster than every five seconds:
curl -sS -H "Authorization: Bearer <management key>" \
  "https://cliproxy.epaflix.com/v0/management/get-auth-status?state=<state>"
```

The management UI has a Copilot login button that drives the same endpoints. The
GitHub access and refresh material is stored through the normal auth storage, so
it lands in `auth_store` in Postgres and survives a pod replacement. The
short-lived Copilot token from `copilot_internal/v2/token` is cached in process
memory only.

`get-auth-status` answers `{"status":"ok"}` both while it is still waiting and
once the credential is saved, so it is not a completion signal. Check
`/v0/management/auth-files` for a `copilot-<login>.json` entry instead, or just
ask for the model list.

### Which endpoint a Copilot model answers on

Measured against the live deployment with one Copilot account authorised. A blank
cell was not tested, not tested and found broken:

| Model | `/v1/messages` | `/v1/responses` | `/v1/chat/completions` |
| --- | --- | --- | --- |
| `gpt-5.6-sol` (Copilot) | works | works | empty 200 |
| `gpt-4.1` (Copilot) | | | empty 200 |
| `gpt-5.6-terra` (Codex) | | | empty 200 |
| `claude-sonnet-5` (Anthropic) | | | works |
| `ollama-qwen3.5-9b` (Ollama) | | | works |

An empty 200 means a well-formed response whose content is `''` and whose usage
counts are all zero. No error, no log line saying why. The `gpt-5.6-*` family is
Responses-native and does not answer on `chat/completions` through this proxy at
all, which is not a Copilot problem: Codex's own `gpt-5.6-terra` does the same.
Copilot's `gpt-4.1` behaving that way is a plugin gap.

The practical consequence is for client config. The `omp` block at the end of
this file uses `api: openai-completions`, which for `gpt-5.6-sol` yields silent
empty replies. Point a Copilot model at the Anthropic Messages shape or at
`/v1/responses` instead.

Verify, in this order. Listing a model proves discovery; only a call proves the
executor:

```bash
curl -sS -H "Authorization: Bearer <client api key>" \
  https://cliproxy.epaflix.com/v1/models | grep -o 'gpt-5\.6-[a-z]*'

curl -sS -H "x-api-key: <client api key>" -H 'anthropic-version: 2023-06-01' \
  -H 'Content-Type: application/json' \
  --data '{"model":"gpt-5.6-sol","max_tokens":64,"messages":[{"role":"user","content":"Reply with exactly: sol-ok"}]}' \
  https://cliproxy.epaflix.com/v1/messages
```

### What we are accepting by running this

Two things, both worth stating rather than discovering later:

- The plugin authenticates as a VS Code Copilot client and sends the recognised
  `Copilot-Integration-Id` headers, because Copilot rejects unrecognised ones.
  That is a Copilot subscription being consumed by something that is not the
  editor it was sold for. GitHub can rate-limit or suspend the account, and
  nothing here makes that our decision to make.
- It is one maintainer's repository in the request path for whatever prompt you
  send. The checksum pin means we get the artifact we reviewed and not a
  substituted one; it says nothing about the code inside it. Read the diff
  before bumping the version.

And one thing worth knowing rather than accepting: `/v1/responses` returns a
`copilot_usage` block with per-token-type counts and a `total_nano_aiu` figure,
so Copilot spend is visible per call. Nothing here collects it yet.

### Rollback

Revert the commit and let the next pod start drop the `.so`. As with pi-bridge,
config lives in Postgres and `reconcile-config.psql` only moves values forward,
so the revert alone does not disable an already-written config entry. To disable
it live, set `plugins.configs.cliproxyapi-copilot.enabled` to `false` in the
management UI; the pod picks that up by fsnotify without a restart. Removing the
stored GitHub credential is a separate deliberate step in the UI.

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

`omp models cliproxy` listing the proxied models checks most of the chain - DNS,
Traefik internal entry point, the client API key, and at least one authorised
provider account behind it. It does not check that a given model answers: that is
discovery, not execution. An empty list with a 200 means the key is fine but no
provider account is authorised yet, so `models:` stays empty until an account is
added.

`api: openai-completions` is right for the Claude and Ollama models and wrong for
the `gpt-5.6-*` family, which returns an empty 200 on `/v1/chat/completions`. See
"Which endpoint a Copilot model answers on" above before adding one of those to
`models:`, or the model will list correctly and reply with nothing.

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

# CPA Manager Plus (request history, token and cost analytics)

[CPAMP](https://github.com/seakee/CPA-Manager-Plus) full mode runs as a second
Deployment in this namespace, `cpa-manager-plus`. It exists because **CPA stores
no usage history of its own**: `internal/store/postgresstore.go` creates exactly
three tables — `config`, `auth`, `cooldown` — and the only request logging is a
file logger writing to an emptyDir that dies with the pod. Persistent per-request
history, token counts and cost per model/provider/account/API key live here or
nowhere.

## Two usage transports — read before adding any usage consumer

CPA can hand out usage events two ways, with opposite rules. Both fail silently
when confused, which is why this section exists.

**1. RESP `SUBSCRIBE` — what this deployment actually uses. Fan-out, safe.**
CPA multiplexes protocols on the same 8317 port: `internal/api/protocol_multiplexer.go`
peeks the first byte of each connection and routes RESP-looking ones to
`internal/api/redis_queue_protocol.go`, whose `SUBSCRIBE` handler calls
`redisqueue.SubscribeUsage()`. Every subscriber gets its own cloned channel, so
any number of them receive events independently and take nothing from each
other. One caveat, for symmetry with the OOMKill note below: the send is
non-blocking, and a subscriber whose 256-message buffer is full is dropped and
its channel closed, silently (`internal/redisqueue/queue.go:12,148-156`). At the
~160 requests/day observed here that is unreachable, but it is a drop, not
backpressure.
CPAMP defaults to `USAGE_COLLECTOR_MODE=auto`, and `auto` tries subscribe
**first** (mode dispatch at
`apps/manager-server/internal/collector/collector.go:139-158`), falling back to
HTTP if the dial, the `AUTH`, or the `SUBSCRIBE` command itself fails — three
branches at `:180`, `:190` and `:200` inside `runSubscribe` (`:159`). If HTTP
also fails, `auto` tries `runRESP` (`:156`) against a real Redis endpoint, which
this deployment does not have, so that third stage is inert here.
`resp.Dial` plain-TCP dials the CPA URL host, and `cliproxy` is a ClusterIP TCP
passthrough on 8317, so it connects.

**2. `GET /v0/management/usage-queue` — the fallback. Destructive.**
`internal/api/handlers/management/usage.go:36` calls `redisqueue.PopOldest`,
which removes what it returns, with a 60s default retention
(`redis-usage-queue-retention-seconds`, clamped to 3600). At most one poller of
that endpoint can work. Despite the package name this is an in-memory buffer,
not an external Redis, so nothing extra is deployed.

**The trap is the interaction.** `Enqueue` publishes to subscribers and returns
**without queueing** when any subscriber exists
(`internal/redisqueue/queue.go:72-76`). So:

- While CPAMP holds its subscription, the HTTP queue stays empty. Anything
  polling it — the `dms-cliproxy-quota` widget, a debugging one-liner during an
  incident — reads nothing and looks merely idle rather than misconfigured.
- If CPAMP ever falls back to HTTP while another subscriber is attached, CPAMP
  is the one that gets nothing.
- Neither case logs an error anywhere.

So the practical rules:

- `replicas: 1` and `strategy: Recreate`. The binding reason is the **RWO SQLite
  volume**, not queue contention — two subscribers would each get a full copy,
  but two writers to one SQLite file corrupt it. Upstream also asks for one
  Manager Server per queue.
- Do not poll `/v0/management/usage-queue` while this runs. Not because it
  steals CPAMP's subscription, but because it is destructive to any other poller
  and will read empty here anyway.
- `pi-bridge` uses neither transport today (quota, models and `/api-call` only).
  A plugin that wants token data should use the in-process
  `pluginapi.UsagePlugin` fan-out — `Manager.dispatch()` hands every registered
  plugin its own copy.

## The prerequisite that fails silently

`usage-statistics-enabled` defaults to **false** in
`internal/config/config_load.go`, and while it is false `redisqueue` enqueues
nothing. CPAMP would then show empty panels with no error in either pod. Git
owns it, in `reconcile-config.psql`, next to `request-log` — CPAMP's setup
wizard can also flip it, but a value living only in the app's own DB is exactly
what the next rebuild reverts.

## First setup — one human, one port-forward

There is deliberately **no Service and no IngressRoute**. Nothing in-cluster
consumes CPAMP yet, and one less admin panel on the network is worth a
port-forward. Add a Service when `pi-bridge`'s `cpam_url` is actually pointed at
it; add a route (plus the Pi-hole record on `.102` for the `internal` entry
point) only if it must be reachable without one.

```bash
kubectl --context epaflix -n remote-pi port-forward deploy/cpa-manager-plus 18317:18317
# then open http://127.0.0.1:18317/management.html
```

The admin key is **supplied by git**, in the sops-encrypted
`cpamanager/cpamanager-secrets.enc.yaml`, so CPAMP never generates or logs one.
Retrieve it into the clipboard without ever printing it:

```bash
sops -d --extract '["stringData"]["admin-key"]' \
  2-k3s/17.remote-pi/cpamanager/cpamanager-secrets.enc.yaml | wl-copy
```

**Do not** `grep` the pod log for it and do not `cat` the decrypted file. Left
unset, upstream prints the generated key to stdout
(`apps/manager-server/cmd/cpa-manager-plus/main.go:128`), and this namespace's
stdout goes to Loki for 31 days — which is exactly why it is supplied
instead. With the Secret in
place the startup line is the value-free `CPA Manager Plus admin credential
initialized`; seeing `admin key generated:` instead means the env var did not
reach the container, and that log line now needs deleting from Loki.

Also in the wizard: CPA URL `http://cliproxy:8317` and the CPA Management Key.
That key is the `management-password` entry of the ksops-managed
`cliproxy-secrets`, which the pod consumes as `MANAGEMENT_PASSWORD`
(`cliproxy/deployment.yaml`) — **not** `remote-management.secret-key`, which is
the `config.yaml`/Postgres field this repo deliberately leaves alone (see "First boot"
above: `reconcile-config.psql` does not touch it, and the process rewrites a
plaintext value there as a bcrypt hash on first start). Read it the same way as
every other credential here, without printing it:

```bash
sops -d --extract '["cliproxy_management_password"]' \
  .github/instructions/secrets.enc.yaml | wl-copy
```

The CPA key is then stored encrypted in CPAMP's own SQLite under
`/data/data.key`; the config API only ever reports `managementKeyConfigured` and
never returns it. That is why no CPA key is mounted into this pod.

## Verification

```bash
kubectl --context epaflix -n remote-pi get deploy cpa-manager-plus
# unauthenticated, safe to curl - and it does NOT drain the queue
curl -s http://127.0.0.1:18317/usage-service/info   # through the port-forward
```

Before setup that reports `"setupRequired":true`, `"configured":false`,
`"mode":"embedded"`. `/health` answers
`{"ok":true,"service":"cpa-manager-plus"}`.

`"hasHistoricalData"` is **not** a live data-flow check, so do not use it as one.
It is computed once during bootstrap
(`apps/manager-server/internal/service/bootstrap/service.go:35,80`) and the info
endpoint returns that stored value
(`apps/manager-server/internal/service/setup/service.go:96`); setup's `markBootstrapReady()` does not
refresh it. A fresh instance keeps reporting `false` after successful collection
until the next Manager Server start. To confirm events are actually landing, look
at the dashboard in the panel, or check the database is growing:

```bash
kubectl --context epaflix -n remote-pi exec deploy/cpa-manager-plus -- \
  du -sh /data/usage.sqlite /data
```

`"mode":"embedded"` is **not** a lesser mode and does not mean the Manager
Server is missing. It is a hardcoded constant in the Manager Server's own setup
service (`apps/manager-server/internal/service/setup/service.go:88`) describing
the panel asset as embedded in the binary rather than loaded from disk. The
lightweight thing upstream calls the "CPAMP panel" is a single HTML file served
by CPA itself on 8317 via `panel-github-repository` — no Manager Server, no
SQLite, no second port. This endpoint answering on 18317 is what proves full
mode is running.

## How much history you actually get

No production path in CPAMP age-prunes the raw usage history, so **the volume
size is the retention policy** — see `cpamanager/storage.yaml` and the entry in
`docs/accepted-risks.md`. The ~7 days the `dms-cliproxy-quota` widget wants is a
subset of "everything until the disk is full", so there is no retention value to
set here and none to check.

Worse, the claim itself bounds nothing: `local-path` is a bind mount with no
quota, so 2Gi is advisory and the real limit is the **node root disk**, shared
with every other pod on that node — the #463 shape. Accepted on the arithmetic
(~160 requests/day observed → ~100 MiB/year), with measurable reopen conditions
in `docs/accepted-risks.md`. Measure with `du -sh /data`, not `df`, which reports
the host filesystem.

If it does fill, events are **lost, not deferred**: the collector pops a batch
from CPA before it writes, and in the default `auto` mode CPA hands payloads
straight to subscribers without queueing them, so nothing is left behind to retry
or age out. CPA keeps serving model requests throughout.

What it does **not** fix: **Copilot quota**. `grep -ril copilot` across the whole
CPAMP repo returns nothing — no Copilot support at all, and its credential-quota
views read CPA the same way `pi-bridge` does, so they hit the same failure. CPA
resolves a `$TOKEN$` placeholder from auth metadata `accessToken`,
`access_token`, `token` (string, or a map carrying either of the first two),
`id_token` or `cookie` (`tokenValueFromMetadata`,
`internal/api/handlers/management/api_tools.go:422-464`) — five key names, the
`token` one accepted as a string or as a map carrying either of the first two —
and failing all of them, from attribute `api_key` (`tokenValueForAuth`,
`:229-242`). The Copilot plugin
stores its credential as `github_access_token` inside StorageJSON and publishes
metadata of only `{type, github_login}` — none of those five keys, and no
`api_key` attribute either. So the
substitution yields an empty token and `/api-call` answers 400 `auth token not
found` (`internal/api/handlers/management/api_tools.go:151-156`) before any request reaches GitHub. Same chain in
`docs/superpowers/plans/2026-08-28-cpamp-manager-server-deployment.md` →
"What CPAMP does NOT fix".

## Backup and restore

**CPAMP's SQLite on the `cpa-manager-plus-data` claim is not backed up by
anything.** The CNPG `ScheduledBackup` for `postgres-cluster` covers the
`cliproxy` database, not this volume, and `local-path` is node-local, so losing
the bound node loses the request history. Recorded, with the reopening
conditions, in `docs/accepted-risks.md` — "2026-08-28 CPAMP request history is
unbacked, node-local and growth-unbounded".

Two manual copy paths exist, neither automated here: CPAMP's JSONL export, and
its `manager-data-snapshot` command (`apps/manager-server/cmd/cpa-manager-plus/main.go:59`). The
snapshot archive includes `data.key` alongside the database and WAL
(`apps/manager-server/internal/command/managerdatasnapshot/command.go:21-26`, `snapshotFiles`), so a
snapshot is secret material — do not park one in a shared directory or commit it.

## Rollback

Remove the three `cpamanager/` entries from `kustomization.yaml` — the two under
`resources:` and the generator — then merge and sync. The
PVC is not garbage-collected with the Deployment, so the history survives a
revert and comes back on re-add. Deleting the claim is the deliberate,
irreversible step — nothing backs it up today. Two manual copy paths exist and
neither is automated here: CPAMP's JSONL export, and its `manager-data-snapshot`
command, whose archive carries `data.key` and is therefore secret material. See "Backup and
restore" above.

`usage-statistics-enabled: true` stays behind in the config row after a revert,
as with every other value `reconcile-config.psql` moves forward. Harmless: with
no drainer the queue just ages out every 60s.
