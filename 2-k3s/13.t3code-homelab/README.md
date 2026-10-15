# Homelab T3 backend

The `t3code-homelab` Application deploys this overlay into its own namespace,
with a fresh PVC, T3 identity, histories and personal project checkout. The
mixed backend stays running. No mixed-home files, histories, credentials or
cloud caches are imported.

The shared `13.t3code` component supplies the maintained installer, provider
setup, hub clients, image-prepull and idle-rollout jobs. The
homelab image excludes Azure CLI, kubelogin and the raw vault CLI. The runtime
uses only the homelab hub. Its restricted vault helper holds an ordinary hub
client token, not a vault login. See the
[hub private-input contract](../23.mcp-hub/instances/README.md).

## Scoped Kubernetes access

The manual `t3code-homelab-access` Application tracks `rbac/` separately so
namespace transformation in the runtime overlay cannot redirect cross-namespace
RoleBindings. Sync it before provisioning. Its cluster grant permits inventory
reads for namespaces, nodes, persistent volumes, storage/CSI kinds, ingress
classes and CRDs. It does not grant cluster-wide namespaced-resource access.

`rbac/namespaces.txt` is the explicit homelab namespace allowlist. Each entry
gets a RoleBinding to built-in `view`, which excludes Secrets. Work hubs and
pods, the mixed pod and legacy hub, shared proxy and vault login namespaces
are excluded. The shared search namespace is excluded because its logs can
contain work queries. Temporary vault setup namespaces are not eligible.
Adding a namespace requires editing this sorted list, regenerating and
reviewing the bindings:

```sh
python3 2-k3s/13.t3code-homelab/tools/sync-rbac.py
kustomize build 2-k3s/13.t3code-homelab/rbac >/dev/null
```

The kubeconfig grants no network isolation. Do not use other services to bypass
its read boundary.

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
  ordinary ServiceAccount remains unmounted. Broader access requires a separate
  reviewed grant change.

`runtime.json` binds the exact input and grant bytes with digests. The helper
calculates them and records issue #1836 as the provisioning reference. There
is no manual digest-copying or separate approval-reference ceremony. Startup
still rejects wrong environments, extra inputs, changed grants, unsafe
kubeconfigs, missing policies, foreign homes and cloud caches. The instructions
in `instructions.md` retain the public-repository and organizational rules.
This configuration does not claim administrator or network isolation.

Secret changes alone do not restart this `OnDelete` StatefulSet. Follow the
shared [input rotation procedure](../13.t3code/env/isolated-runtime.md#input-rotation)
before changing either Secret. PVC deletion, pruning and scaling retain the home.
The shared contract also documents [policy inputs](../13.t3code/env/isolated-runtime.md#policy-input),
[homelab kubeconfig constraints](../13.t3code/env/isolated-runtime.md#homelab-kubeconfig)
and [provider retention and Codex requirements](../13.t3code/env/isolated-runtime.md#provider-preferences-and-codex-requirements).

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
the access Application is healthy and before syncing the runtime Application.
Check for active shared-proxy requests before the `remote-pi` sync because
its transcript ConfigMap change rolls that shared Deployment. The existing
mixed client keys remain registered.

```sh
argocd app sync t3code-homelab-access
argocd app wait t3code-homelab-access --sync --health --timeout 300
argocd app sync remote-pi
argocd app wait remote-pi --sync --health --timeout 600
kubectl --context epaflix -n mcp-hub-homelab rollout restart deployment/mcp-hub
kubectl --context epaflix -n mcp-hub-homelab rollout status deployment/mcp-hub --timeout=180s
argocd app sync t3code-homelab
argocd app wait t3code-homelab --sync --health --timeout 1200
kubectl --context epaflix -n t3code-homelab wait --for=condition=Ready pod/t3code-0 --timeout=300s
```

The hub reload is required because its client digest map loads at startup.
Before the first sync, confirm that the LoadBalancer pool and reachable network
are suitable for this homelab SSH endpoint. No public HTTP ingress is added.
Get the assigned SSH address with:

```sh
kubectl --context epaflix -n t3code-homelab get service t3code-ssh
```

### Backend checks

`kubectl exec` does not inherit exports made by the container entrypoint.
Set the kubeconfig explicitly for these administrative checks.

```sh
kubectl --context epaflix -n t3code-homelab exec t3code-0 -c t3 -- bash -c '
  set -eu
  export KUBECONFIG=/run/t3-inputs/kubeconfig
  curl -fsS -o /dev/null http://127.0.0.1:3773/health
  test "$(kubectl config current-context)" = homelab
  kubectl get namespaces -o name >/dev/null
  ! command -v az >/dev/null
  test ! -e "$HOME/.azure"
  test -z "${SSH_AUTH_SOCK:-}"
  test "$(gh api user --jq .login)" = SpyrosPsarras
  gh api repos/SpyrosPsarras/epaflix --silent
  python3 "$HOME/.config/t3-policy/check.py" .local/share/pi/cc-safety-net/2.6.4/dist/bin/cc-safety-net.js
'
kubectl --context epaflix -n t3code-homelab exec t3code-0 -c sshd -- bash /scripts/sshd.sh check
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

The administrative RBAC checks must return `no`, `no`, `no`, `no`, `no`, `yes`, `yes`:

```sh
identity=system:serviceaccount:t3code-homelab:homelab-agent
kubectl --context epaflix auth can-i get pods/log -n remote-pi --as="$identity"
kubectl --context epaflix auth can-i get pods/log -n mcp-hub --as="$identity"
kubectl --context epaflix auth can-i get pods --subresource=log -n searxng --as="$identity"
kubectl --context epaflix auth can-i get configmaps -n t3code-work --as="$identity"
kubectl --context epaflix auth can-i get secrets --all-namespaces --as="$identity"
kubectl --context epaflix auth can-i list pods -n servarr --as="$identity"
kubectl --context epaflix auth can-i list namespaces --as="$identity"
```

Inspect managed MCP configuration without dumping credential-bearing process
or provider environments. Every configured MCP server URL must start with
`http://mcp-hub.mcp-hub-homelab.svc.cluster.local:8000/`. There must be no work
hub token or work/cloud login cache. Check that the mixed backend remains
healthy, without reading its home or histories.

### homePC and laptop acceptance

Prepare a dedicated identity on each client and provision only its public key
in the stopped input-rotation window. Never share a private key between clients.

Obtain the backend's public host key through authenticated kubectl, not
unauthenticated `ssh-keyscan`. The following command returns only a public key:

```sh
kubectl --context epaflix -n t3code-homelab exec t3code-0 -c sshd -- \
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
`http://127.0.0.1:13773/.well-known/t3/environment`. Select this backend in T3,
using the supported SSH target or private headless pairing flow. If pairing
is needed, run `t3 pair --base-dir /home/t3/.t3 --ttl 5m` only in an owner's
private terminal in this backend. Its output contains a credential and must
not enter agent logs or issue evidence. Keep the mixed environment entry.

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
kubectl --context epaflix -n t3code-homelab delete pod t3code-0
kubectl --context epaflix -n t3code-homelab wait --for=create pod/t3code-0 --timeout=120s
kubectl --context epaflix -n t3code-homelab wait --for=condition=Ready pod/t3code-0 --timeout=600s
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
python3 2-k3s/13.t3code/env/tools/startup.test.py -v
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
