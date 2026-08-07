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
