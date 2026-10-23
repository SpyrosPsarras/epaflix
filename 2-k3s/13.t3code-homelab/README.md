# Homelab T3 backend

The `t3code-homelab` Application deploys this overlay into its own namespace,
with a fresh PVC, T3 identity, histories and personal project checkout. The
homelab backend will replace the legacy mixed backend. The mixed backend stays
running until the owner approves retirement. No mixed-home files, histories,
credentials or cloud caches are imported.

The runtime Application automatically syncs merged revisions with pruning,
without self-healing. `OnDelete` keeps the running pod until the idle-rollout
job replaces it after 30 idle minutes. For input rotation, pause both
`app-of-apps` and this Application before scaling down, as described in the
[shared procedure](../13.t3code/env/isolated-runtime.md#input-rotation).
The separate access Application remains manual-sync.

The shared `13.t3code` component supplies the maintained installer, provider
setup, hub clients, image-prepull and idle-rollout jobs. The
homelab image excludes Azure CLI and kubelogin. Its MCP clients use only the
homelab hub. See the [hub private-input contract](../23.mcp-hub/instances/README.md).

## Runtime vault reads

Use the image's `vault` runner to read a password, custom field or attachment
without exposing its value to the model:

```sh
vault run --name "$ITEM_NAME" --password-env VAULT_PASSWORD --username-env VAULT_USER \
  --field 'api-key=VAULT_API_KEY' --attachment credentials.json \
  --attachment-env VAULT_FILE -- python3 /private/consume.py
```

Every command that reads an item (`run`, `list`, `update`, `trash`, `attach`)
syncs the sidecar's local cache first, so an item edited in the Bitwarden apps
is visible on the next call. `bw serve` does
not sync by itself; before this, the cache was only refreshed at sidecar start.
Use `--item "$ITEM_ID"` instead of `--name` for an exact ID. Names and attachment
names must match exactly and be unique in the approved scope. Target environment
names must start with `VAULT_`. `--timeout` defaults to 120 seconds and accepts
0.1 through 120 seconds. The child receives the selected values and an attachment
path, never the vault login or session. Attachments use a 0600 file in a 0700
temporary directory. Success, failure, timeout and SIGTERM remove the directory
and kill remaining child processes. The runner discards child stdin, stdout and
stderr and prints only status and an exit code. Exact-value redaction would miss
encoded or transformed secrets, so use child exit codes rather than logs to
check success.

The unpatched `@bitwarden/cli` is pinned to 2026.8.0. `bw serve` supports
attachments and organization writes needed by later provisioning, unlike `rbw`.
Disposable Vaultwarden 1.37.3 measurements were 23 ms for a warm password,
18 ms for a field and 103 ms for an attachment. Start, unlock and first read took
9.8 s with cached login data, versus 25.0 s from a fresh home. RSS was about
195 MiB. These are client measurements, not a live deployment acceptance result.

Only the UID-1001 `vault` sidecar receives the five `BW_*` keys from the
namespace's `vaultwarden-agent` Secret. T3 and sshd remain UID 1000 with no vault
Secret or client-data mount, so they cannot read the sidecar's process
environments or cache through `/proc`. T3 startup rejects every `BW_*` and
`BITWARDENCLI_*` variable, including empty ones. The sidecar uses a read-only
root filesystem and its own temporary volume. A storage init container restores
UID-1001 ownership and mode 0700 on the retained 1 GiB PVC root and client
directory before each pod start, explicitly clearing setgid after fsGroup
permission changes. It takes ownership as root before chmod, then transfers it
to UID 1001, so its only added capability is CHOWN. CI runs the two-UID process
and cache permission test as root through sudo. Encrypted client data persists
under `/vault/client`; it is not part of the T3 home.

The sidecar refuses a cached account or server mismatch. The owner login must
never be provisioned here. Fresh-volume login uses `--passwordenv`. Cached
restarts with persisted `client/data.json` start `bw serve` directly, without
a password or session in its environment. Its HTTP `/status` must match the
agent account and server before unlock through the stock loopback API. Before
reading status and again before sending the password, the sidecar checks the
kernel's TCP listener UID and socket inode against the serve child's file
descriptors. Foreign listeners stop startup without a login fallback, including
IPv4 wildcard and IPv6 listeners that could intercept the loopback address.
The password stays in the POST body, never argv or logs. An unauthenticated
cache stops serve before taking the existing fresh-login path. Login and unlock
each make at most three attempts. The sidecar bounds CLI commands and resident
`/sync` requests to 45 seconds. Other local HTTP requests use 10 seconds.
The sidecar publishes scope only after sync succeeds and the account is still
unlocked. After
the client is ready, three consecutive status failures remove its published
scope and stop the sidecar for a kubelet restart. A successful check resets the
count. T3 keeps running independently and its normal input preparation runs only
once. CLI diagnostics never enter pod logs.

The sidecar publishes only the runtime variant, organization ID and collection
ID in `/run/t3-vault/scope.json`, mounted read-only in T3. The runner requires
both IDs to match each selected item. Foreign and nonexistent IDs fail without
running the child. Requests bypass HTTP proxies and redirects. The API binds
only `127.0.0.1:8087`; there is no Service or container port for it, and sshd
permits forwarding only to 3773.

The local API has no authentication. A deliberate command in any pod container
can read homelab items through it. `vault run` is the sanctioned path that
suppresses secret output; it does not prevent deliberate exfiltration.
Work-organization isolation relies on the agent-account setup in #1838.
The restricted helper and reader have been removed. See the
[post-merge retirement steps](../23.mcp-hub/instances/README.md#restricted-reader-retirement-after-merge)
for live resource and owner-approved account cleanup.

## Vault-backed outbound SSH

The owner marks a key for this pod by storing it as a Bitwarden SSH-key item,
type 5, in the approved homelab organization and collection. Names and custom
fields do not select keys. Every SSH-key item in that scope must contain an
unencrypted private key that OpenSSH can load. With zero scoped SSH-key items,
the sidecar starts an empty agent and logs only
`vault: no scoped SSH keys; agent is empty`. T3 and vault reads remain usable,
but outbound SSH cannot authenticate until the owner moves the intended keys
into scope. The sidecar loads them on its next 10-second sync. A key item
with a missing, invalid or passphrase-protected private key still rejects the entire key set.
An unreachable vault prevents startup. The loader never tries the owner's
login, attachments, other collections or identity files. Ordinary non-SSH items
are ignored.

After the account check, unlock, sync and post-sync account/status check, the
UID-1001 vault sidecar rechecks that the local HTTP listener belongs to the
`bw serve` child. Only then does it fetch the scoped items through that API
and pipe each private key to `ssh-add` on stdin.
It starts a fresh foreground `ssh-agent` as UID 1001. Key bytes remain in the
UID-1001 loader, ssh-add and agent memory, never in UID-1000 processes, files,
environment variables or logs during startup. Core dumps are disabled. The
agent's private socket is in a UID-1001-only temporary directory. Any failed
key load destroys the entire agent, including keys loaded earlier in that run.

OpenSSH checks the Unix peer UID and refuses a different non-root UID even
when the socket is writable. See the peer-UID check in [OpenSSH's agent
source][openssh-agent]. A UID-1001 Python socket proxy therefore accepts
UID-1000 clients at `/run/t3-vault/ssh-agent.sock`. The directory is owned by
UID 1001 and mounted read-only in T3 and sshd. The proxy permits only agent
protocol messages for listing public identities and signing. It denies key
addition, removal and other operations. Each proxy refusal logs one fixed line
without request data. T3 cannot use the private socket directly. No
private-key export exists in the allowed protocol. This adds a small proxy
rather than putting private keys in a UID-1000 agent's memory.

The sidecar publishes its ready scope only after all keys load and the proxy
starts. T3 waits up to 600 seconds for that scope and a reachable agent,
including an empty one, before starting the backend. A dead agent or three
consecutive vault status failures stop the sidecar and remove the socket.
Every 10 seconds the sidecar syncs the vault. When the scoped SSH key set
changes, it loads a complete replacement agent, swaps it in and logs
`vault: SSH agent reloaded; N scoped keys`. A failed sync or a bad key keeps
the running agent. Agent connections opened before a reload fail once and
must reconnect.

The existing local vault API is unauthenticated and reachable by every pod
container. This design keeps automatic key loading out of UID-1000 memory; it
does not stop a deliberate command from reading vault items through that API.
Treat pod command execution as trusted, as documented above.

The homelab-only script projection includes the generated SSH config from
`1-proxmox/ssh/homelab.conf`. `sync-shared.sh` keeps the copy current.
Inventory hosts select this agent and the existing `accept-new` host-key
policy through the generated final-match identity include. `github.com`
selects the same agent with GitHub's published host keys pinned and
`StrictHostKeyChecking yes`. Every other host selects no agent and requires
strict host-key checking, even when `SSH_AUTH_SOCK` points to the homelab
agent. All hosts use public-key authentication without
fallback identity files or password prompts. Inventory host keys persist in
the homelab home PVC's known_hosts. A changed host key fails. First contact is
not independently pinned. Work receives neither the homelab config nor its
agent socket. The projected SSH guide has one ConfigMap source.

[openssh-agent]:
  https://github.com/openssh/openssh-portable/blob/V_9_2_P1/ssh-agent.c

### Post-merge SSH acceptance

An empty collection does not block deployment, but real SSH acceptance must
wait until the owner moves the intended SSH-key items into scope. Wait for the
new runtime image to build and the homelab Application to sync automatically.
Let the idle-rollout job replace the pod, or replace it in an approved idle
window. This StatefulSet uses `OnDelete`; syncing the manifest alone does not
replace the current pod. No work-pod restart is needed for this feature.
Updating `instructions.md` does not rewrite an already provisioned instruction
bundle. Refresh that bundle through the normal reviewed input rotation so it
no longer describes outbound SSH as unavailable. The runtime SSH guide is
projected independently.

After the intended keys are in scope and the replacement is Ready, run these
checks without printing vault data:

```sh
kubectl --context epaflix -n t3code-homelab exec homelab-0 -c t3 -- bash -ec '
  SSH_AUTH_SOCK=/run/t3-vault/ssh-agent.sock ssh-add -l >/dev/null
  ssh homepc true
  ssh k3s-master-51 true
  ssh laptop true
'
```

T3 processes and SSH login sessions do not receive `SSH_AUTH_SOCK`. OpenSSH
selects the agent only through the inventory's `IdentityAgent` setting.
Readiness checks and explicit `ssh-add` diagnostics set the variable for that
command only. Non-OpenSSH clients do not see the agent by default. If the
laptop is away from home, its LAN alias times out and exits 255 after the
configured 10-second connect timeout. This is a conditional acceptance result,
not a key failure. Run the laptop check again when it is home. Adding a reverse
Tailscale route is outside this change.

In an approved follow-up window, change the scoped key set and wait for the
sidecar's `vault: SSH agent reloaded` log line. Check only expected public
fingerprints or signing results to confirm
removed keys are gone. Use synthetic keys for failure checks, and permanently
delete any synthetic vault items afterwards. An empty set must leave a ready
but empty agent that cannot authenticate SSH. At pod start, an invalid key item
or unavailable vault must leave no ready scope or working agent socket. On a
running pod, the same failures must keep the loaded keys. Do not
remove the owner's real keys merely to exercise that failure case on the live
backend.

See [scoped vault writes](../13.t3code/env/vault-writes.md).

## Kubernetes administration

The manual `t3code-homelab-access` Application tracks `rbac/` separately from
the runtime overlay. It binds `system:serviceaccount:t3code-homelab:homelab-agent`
to the built-in `cluster-admin` ClusterRole, matching the mixed pod's Kubernetes
access. The former inventory ClusterRole and namespace `view` bindings are
removed. The new binding has its own name because a binding's `roleRef` is
immutable.

The existing kubeconfig still uses the same ServiceAccount token, inline CA
and HTTPS API endpoint. Neither `inputs.py` nor startup validation restricts
the token's RBAC grants. No input rotation or token replacement is needed.

Cluster-admin can access work resources too. The work boundary is now a written
rule, not RBAC enforcement. The independently projected
[`/scripts/homelab-ssh.md`](files/homelab-ssh.md) prohibits work access, Azure/Entra
logins and work credentials, and documents resource-scoped Argo sync with a
dry run at the merged SHA. It also requires owner confirmation before deleting
data. Updating this ConfigMap does not require private-bundle rotation, but
its hash-suffixed name changes. The existing pod keeps the old ConfigMap.

Before granting cluster-admin, the owner or an admin must wait for the runtime
Application to sync the merged SHA and for an approved idle pod replacement.
`OnDelete` does not replace the pod on sync. Confirm the replacement is Ready
and compare its `/scripts/homelab-ssh.md` with the merged guide. Only after
the new guide is confirmed inside the pod may the owner or an admin dry-run
and sync `t3code-homelab-access` at that SHA with pruning. The homelab agent
cannot patch Applications before this first grant.

Homelab runtime egress permits the cluster, LAN, Argo CD and inventory SSH.
Direct pod traffic to `t3code-work` and `mcp-hub-work` remains excluded by a
namespace `NotIn` selector. API access, SSH and shared proxies can bypass that
network filter and must never be used to cross the written work boundary.
See [network policies and post-merge checks](../13.t3code/env/network-isolation.md).

## Provisioning

Run the first provisioning from the repository root inside the T3 pod with
the homelab `epaflix` Kubernetes context. `--skill-file` can select an explicitly
reviewed public ponytail skill file elsewhere. The default is the installed
public skill under `/tools`. The helper captures subprocess diagnostics and
never prints secret values. Its namespace, grants and kubeconfig are independent
of the mixed backend.

```sh
python3 2-k3s/13.t3code-homelab/tools/inputs.py provision \
  --authorized-keys /private/approved-homelab-public-login-keys
```

The public-key file contains option-free Ed25519 login keys. Private keys
stay on each client. The first provisioning refuses an existing input Secret.
If an intermediate operation fails, it retains already-created grants rather
than minting replacements. Inspect resource presence, not Secret values,
before retrying. A later input rotation needs an explicit review.

The helper creates these external inputs, not an alternate deployment path:

- I1: `t3-runtime-inputs` contains `runtime.json`, `bundle.json`, `policies.json`,
  `services.json`, `kubeconfig` and `authorized_keys`. It derives policy and
  instruction content from this repository and the installed public ponytail
  skill. It does not export any agent home. The policy acceptance checks invoke
  the installed, hash-verified cc-safety-net CLI with harmless allow/deny
  payloads. They never execute the dangerous commands in those payloads.
- I2: `t3-runtime-grants` holds a new model client key and a fresh homelab hub
  token. The model key has a reserved `t3-homelab-` prefix and is registered through
  cliproxy's management API without replacing other keys. Its authoritative
  client list persists in cliproxy's PostgreSQL store. The hub receives only
  this client's token digest, preserving its existing clients and upstreams.
- I3: `homelab-agent-kubeconfig` supplies a token for the ServiceAccount owned
  by the tracked access Application. The provisioner creates no RBAC. The pod's
  ordinary ServiceAccount remains unmounted. The tracked access Application
  grants this token cluster-admin without changing its input bytes.

`runtime.json` binds the exact input and grant bytes with digests. The helper
calculates them and records issue #1836 as the provisioning reference. There
is no manual digest-copying or separate approval-reference ceremony. Startup
still rejects wrong environments, extra inputs, changed grants, unsafe
kubeconfigs, missing policies, foreign homes and cloud caches. The instructions
in `instructions.md` retain the public-repository and organizational rules.
Input validation does not replace API RBAC or network policy.

Secret changes alone do not restart this `OnDelete` StatefulSet. Follow the
shared [input rotation procedure](../13.t3code/env/isolated-runtime.md#input-rotation)
before changing either Secret. PVC deletion, pruning and scaling retain the home.
The shared contract also documents [policy inputs](../13.t3code/env/isolated-runtime.md#policy-input),
[homelab kubeconfig constraints](../13.t3code/env/isolated-runtime.md#homelab-kubeconfig)
and [provider retention and Codex requirements](../13.t3code/env/isolated-runtime.md#provider-preferences-and-codex-requirements).
Both isolated images append [runtime facts](../13.t3code/env/isolated-runtime.md#image-shipped-runtime-facts)
without private-input rotation. The direct Codex instance is disabled; use Pi
with `cliproxy/<route>/<model>` IDs.

The init container creates `.t3/userdata` as the runtime user before Kubernetes
mounts `server-runtime.json`. Otherwise Kubernetes creates a root-owned parent
that prevents the first settings write.

## Owner GitHub step

Create a new fine-grained PAT in GitHub's personal access token settings.
Choose the personal account as **Resource owner**, never an organization.
Select only the personal repositories needed here. For the epaflix workflow,
Contents, Issues and Pull requests read/write are sufficient. Set an expiry.
Do not reuse a classic PAT, a work-resource-owner PAT or the mixed token.
The GitHub login can be the same account used elsewhere. The resource owner
and selected repositories define this grant's boundary.

During the stopped rotation window, use an interactive terminal with this
checkout and the `epaflix` kubeconfig:

```sh
python3 2-k3s/13.t3code-homelab/tools/inputs.py github
```

The installer asks for personal-resource-owner confirmation, reads the PAT
without echo and pipes it into the grants Secret through kubectl's stdin.
It rejects classic-token formats and updates the runtime's authentication
mode and grant digest. It refuses to run while a pod exists or the StatefulSet
is set to recreate one. It cannot inspect the PAT creation UI. The owner must
verify the resource owner and selected repositories there. Do not paste the
PAT into an agent conversation, command argument or evidence file.

Without this step, public checkout and model discovery work, but `gh` remains
unauthenticated. Complete the rotation procedure before restoring the backend.

## Post-merge deployment

These are orchestrator steps, not commands for an unmerged implementation.
For a first install, provision the private inputs and owner GitHub grant after
the access Application is healthy. Complete provisioning and routing review
before merging the runtime revision, which syncs automatically. After staging
hub client digest changes, advance the homelab hub's tracked input revision
through review and merge. The hub syncs automatically with pruning and
self-healing, and the new pod loads its client digest map at startup.
Check for active shared-proxy requests before the `remote-pi` sync because
its transcript ConfigMap change rolls that shared Deployment. The existing
mixed client keys remain registered.

```sh
argocd app sync t3code-homelab-access
argocd app wait t3code-homelab-access --sync --health --timeout 300
argocd app sync remote-pi
argocd app wait remote-pi --sync --health --timeout 600
argocd app wait mcp-hub-homelab --sync --health --timeout 300
kubectl --context epaflix -n mcp-hub-homelab rollout status deployment/mcp-hub --timeout=180s
argocd app wait t3code-homelab --sync --health --timeout 1200
kubectl --context epaflix -n t3code-homelab wait --for=condition=Ready pod/homelab-0 --timeout=300s
```

Before merging the first runtime revision, confirm that the LoadBalancer pool
and reachable network are suitable for this homelab SSH endpoint. No public
HTTP ingress is added.
The SSH address is pinned to `192.168.10.113` at `t3code-homelab.epaflix.lan`.
Confirm the Service address with:

```sh
kubectl --context epaflix -n t3code-homelab get service t3code-ssh
```

### Backend checks

`kubectl exec` does not inherit exports made by the container entrypoint.
Set the kubeconfig explicitly for these administrative checks.

```sh
kubectl --context epaflix -n t3code-homelab exec homelab-0 -c t3 -- bash -c '
  set -eu
  export KUBECONFIG=/run/t3-inputs/kubeconfig
  curl -fsS -o /dev/null http://127.0.0.1:3773/health
  test "$(kubectl config current-context)" = homelab
  kubectl get namespaces -o name >/dev/null
  ! command -v az >/dev/null
  test ! -e "$HOME/.azure"
  agent_status=0
  SSH_AUTH_SOCK=/run/t3-vault/ssh-agent.sock ssh-add -l >/dev/null || agent_status=$?
  test "$agent_status" -le 1
  test "$(gh api user --jq .login)" = SpyrosPsarras
  gh api repos/SpyrosPsarras/epaflix --silent
  python3 "$HOME/.config/t3-policy/check.py" .local/share/pi/cc-safety-net/2.6.4/dist/bin/cc-safety-net.js
'
kubectl --context epaflix -n t3code-homelab exec homelab-0 -c sshd -- bash /scripts/sshd.sh check
```

After the positive personal-account check, the owner runs this from homePC or
the laptop, not the mixed pod:

```sh
python3 2-k3s/13.t3code-homelab/tools/check-github-scope.py
```

It privately discovers an organization-owned private repository through the
owner's existing `gh` login, passes only its metadata to the homelab backend,
and requires an HTTP 404 from a HEAD request using the homelab PAT. It prints
neither the repository name nor credentials. It does not fetch repository
content or copy the owner's credential into the pod.

After the access Application sync, every administrative RBAC check below must
return `yes`. These are authorization checks only. Do not actually access the
work namespaces or the legacy pod's home:

```sh
identity=system:serviceaccount:t3code-homelab:homelab-agent
kubectl --context epaflix auth can-i '*' '*' --all-namespaces --as="$identity"
kubectl --context epaflix auth can-i patch applications.argoproj.io -n argocd --as="$identity"
kubectl --context epaflix auth can-i create pods/exec -n servarr --as="$identity"
kubectl --context epaflix auth can-i get secrets --all-namespaces --as="$identity"
kubectl --context epaflix auth can-i get configmaps -n t3code-work --as="$identity"
kubectl --context epaflix auth can-i get configmaps -n mcp-hub-work --as="$identity"
kubectl --context epaflix auth can-i list namespaces --as="$identity"
```

Inspect managed MCP configuration without dumping credential-bearing process
or provider environments. Every configured MCP server URL must start with
`http://mcp-hub.mcp-hub-homelab.svc.cluster.local:8000/`. There must be no work
hub token or work/cloud login cache. Check that the mixed backend remains
healthy, without reading its home or histories.

### T3 Connect

This pod is live on T3 Connect. T3 binds only `127.0.0.1:3773` and advertises
no direct LAN endpoint. App access uses T3 Connect or SSH only, not the pod IP.
The `t3code` Service retains the StatefulSet identity with SSH only on 2222;
it no longer exposes HTTP. Follow the shared
[T3 Connect and Zed SSH procedure](../13.t3code/env/client-access.md).
The one-time owner link command for this pod is:

```sh
kubectl --context epaflix -n t3code-homelab exec -it homelab-0 -c t3 -- \
  t3 connect link --headless --base-dir /home/t3/.t3
```

### homePC and laptop acceptance

The SSH tunnel below is an acceptance and diagnostic check, not the normal
client connection. Use T3 Connect for app access.

Prepare a dedicated identity on each client and provision only its public key
in the stopped input-rotation window. Never share a private key between clients.

Obtain the backend's public host key through authenticated kubectl, not
unauthenticated `ssh-keyscan`. The following command returns only a public key:

```sh
kubectl --context epaflix -n t3code-homelab exec homelab-0 -c sshd -- \
  python3 -c 'import pathlib; print((pathlib.Path.home()/".config/t3-sshd/ssh_host_ed25519_key.pub").read_text(), end="")' \
  > /tmp/verified-homelab-host-key.pub
```

Transfer that public file to each client through authenticated SSH. On homePC,
use the new LoadBalancer address and user `node`:

```sh
python3 2-k3s/13.t3code/env/tools/prepare-laptop.py \
  --directory "$HOME/.config/t3-homelab/ssh" \
  --host "$HOMELAB_SSH_ADDRESS" --user node \
  --host-key-file /tmp/verified-homelab-host-key.pub
bash 2-k3s/13.t3code/env/tools/connect-laptop.sh \
  "$HOME/.config/t3-homelab/ssh/ssh_config" 13773
```

Keep the tunnel running. From another homePC terminal, check
`http://127.0.0.1:13773/health` and
`http://127.0.0.1:13773/.well-known/t3/environment`. Select this backend in T3
through the owner's T3 account. Keep the mixed environment entry.

Create a T3 thread in the homelab project with the Pi provider and an enabled
`cliproxy/` model. Send exactly:

```text
Reply exactly HOMELAB_OK. Do not use tools, read files or change anything.
```

A passing result is `HOMELAB_OK` in a thread belonging to the homelab backend,
not a local Pi session or a mixed-backend thread. Record only the thread ID,
backend ID and pass/fail result. Repeat on the laptop after provisioning its
separate public key and updating the tracked input revision.

After that thread, Loki must contain a transcript record with
`"client_backend": "homelab"`. The emitter derives this fixed label only from
cliproxy's masked downstream key header. It never emits the key or its suffix.
Use this Loki query, scoped to the harmless homelab thread's time window:

```logql
{namespace="remote-pi", container="transcripts"} |= "\"client_backend\": \"homelab\""
```

This is a request-attribution label, not an authentication boundary. Do not
query or copy unrelated shared-proxy transcripts as acceptance evidence.

### Restart and update jobs

After the harmless thread and while the homelab backend has no active work,
record its discovery environment ID and thread ID, then run:

```sh
kubectl --context epaflix -n t3code-homelab delete pod homelab-0
kubectl --context epaflix -n t3code-homelab wait --for=create pod/homelab-0 --timeout=120s
kubectl --context epaflix -n t3code-homelab wait --for=condition=Ready pod/homelab-0 --timeout=600s
kubectl --context epaflix -n t3code-homelab get jobs
kubectl --context epaflix -n t3code-homelab create job \
  --from=cronjob/t3code-idle-rollout t3code-idle-rollout-acceptance
kubectl --context epaflix -n t3code-homelab wait \
  --for=condition=Complete job/t3code-idle-rollout-acceptance --timeout=300s
```

Verify the same discovery environment ID, SSH host key, checkout and completed
thread after restart. The successful first sync proves the PreSync image-prepull
Job ran. The idle-rollout test must complete without deleting a busy pod. Its
Role is confined to this namespace's StatefulSet, named pod and exec endpoint.
The normal job waits for 30 minutes of idle time before an `OnDelete` rollout.
Confirm the mixed backend is still healthy.

## Verification

Run these offline checks from the repository root:

```sh
python3 2-k3s/13.t3code-homelab/tools/inputs.test.py -v
python3 2-k3s/13.t3code/env/tools/vault-client.test.py -v
python3 2-k3s/13.t3code/env/tools/vault-startup.test.py -v
python3 2-k3s/13.t3code/env/tools/homelab-ssh.test.py -v
python3 2-k3s/13.t3code/env/tools/startup.test.py -v
python3 2-k3s/13.t3code/env/tools/image-commands.test.py -v
python3 2-k3s/13.t3code-homelab/tools/rbac.test.py -v
python3 2-k3s/13.t3code-homelab/tools/github-scope.test.py -v
python3 2-k3s/17.remote-pi/cliproxy/tools/client-backend.test.py -v
python3 2-k3s/13.t3code/env/tools/isolated-runtime.test.py -v
bash 2-k3s/13.t3code/env/tools/runtime-variants.test.sh
bash 2-k3s/13.t3code/env/tools/rollout-hash.test.sh
bash 2-k3s/13.t3code/one/tools/sync-shared.sh --check
kustomize build 2-k3s/13.t3code-homelab >/dev/null
kustomize build 2-k3s/13.t3code-homelab/rbac >/dev/null
kustomize build 2-k3s/11.argocd/apps >/dev/null
```

When Docker is available, also run the component's mixed and homelab smoke:

```sh
DOCKER_HOST=unix:///var/run/docker.sock bash 2-k3s/13.t3code/env/tools/smoke.sh
DOCKER_HOST=unix:///var/run/docker.sock T3_RUNTIME_VARIANT=homelab \
  bash 2-k3s/13.t3code/env/tools/smoke.sh
```
