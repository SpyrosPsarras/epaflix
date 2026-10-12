# Work T3 backend

This is a separate backend, not a deployment claim. Issue #1776 still needs
homelab live acceptance, and #1777 still needs work live acceptance. Mixed and
homelab keep their Applications and homes.

The work namespace `t3code-work` has its own PVC, clean home, T3 identity,
provider grants, histories, SSH host key and `projects/work` checkout. The
sibling overlay uses the small `13.t3code` component for startup, safety,
settings, SSH discovery and update jobs. The extra work component adds only
the pinned authentication helper. Its maintained source is
`../13.t3code/env/files/work-aks-auth.py`; `sync-shared.sh` copies it into
`auth/` for Kustomize's file loader.

The work image includes Azure CLI. Neither isolated image includes the vault
CLI. Homelab still has no Azure CLI, work helper, Azure cache or work grants.
No image contains private instructions, service inventories or credentials.

## Required private provisioning

Review exact inputs and grants for work use. A context name, hostname or
account display name does not prove organizational ownership. The approval
references and digests bind operator-reviewed bytes and grants; they are not
an automatic account classifier or a signature from an external authority.
Do not infer inputs from legacy files or export a desktop or mixed home.

Provision external, stable-name Secrets in `t3code-work` through the approved
private provisioner. Argo owns only the public overlay, never these Secrets.
Keep private values, approval references, hashes and enrollment output out of
public Git, issue comments and logs.

1. I1: `t3-runtime-inputs` requires `runtime.json`, `bundle.json`,
   `policies.json`, `services.json`, `kubeconfig`, `authorized_keys` and
   `work-auth.json`. All are required; there is no default work inventory,
   policy, account or endpoint.
2. I2: `t3-runtime-grants` requires a reviewed `model-token` and an independent
   `work.`-prefixed `hub-token`. Add a reviewed `github-token` only with
   `gitAuth: github-token`. Public Git mode rejects a supplied Git grant.
3. I3: Review SSH routing, the LoadBalancer pool and worker placement BEFORE
   the first manual sync. Sync allocates an address even if startup refuses
   inputs. Put required affinity in the tracked overlay first. Both jobs
   inherit that affinity.

Use the private input formats documented in
[`../13.t3code-homelab/README.md`](../13.t3code-homelab/README.md), with these
explicit work differences:

- `environment` is `work` in runtime, policy and service files.
- `hubUrl` is exactly
  `http://mcp-hub.mcp-hub-work.svc.cluster.local:8000`. No mixed or homelab hub
  is registered. Only the work-prefixed hub grant is accepted.
- `projectUrl`, `modelUrl`, instruction/skill bundle and grant hashes come
  from their work reviews. No model or Git grant is copied from another home.
- `runtime.json.approvals` requires `git`, `models`, `policies`, `kubeconfig`,
  `hub`, `laptop`, `routing` and `workAuth`. The last review covers the exact
  tenant, audience, subscription, account and Kubernetes scope together.
- `runtime.json.sha256` covers the five common reviewed input files and `work-auth.json`.
  `grantSha256` covers the supplied model, hub and optional Git grants.
- `services.json` is the reviewed work hub inventory from #1775. Vault,
  unknown, empty, duplicate or foreign-environment inventories fail closed.
  The synthetic `searxng` fixture is not a deployable work inventory.

`work-auth.json` has exactly these fields. Placeholders are non-deployable;
provide reviewed private values instead:

```json
{
  "environment": "work",
  "tenantId": "<reviewed-tenant-UUID>",
  "audience": "<reviewed-AKS-audience-UUID-or-HTTPS-resource>",
  "subscriptionId": "<reviewed-subscription-UUID>",
  "account": {"name": "<exact-reviewed-account>", "type": "user"},
  "contexts": [
    {
      "name": "<approved-context>",
      "cluster": "<approved-cluster-reference>",
      "user": "<approved-user-reference>",
      "server": "<exact-approved-HTTPS-API-URL>",
      "certificateAuthoritySha256": "<SHA256-of-decoded-inline-CA>"
    }
  ]
}
```

Multiple contexts may be approved. The kubeconfig must contain exactly the
reviewed context/cluster/user sets, with consistent references and an approved
current context. Each API needs the exact reviewed HTTPS endpoint and inline
CA. Arbitrary exec plugins, plugin environment variables, cloud auth-provider
entries, static foreign credentials and external credential files are rejected.
Changing a homelab entry's display name does not make it eligible.

Each work user has only this exec configuration:

```json
{
  "exec": {
    "apiVersion": "client.authentication.k8s.io/v1",
    "command": "/scripts/work-aks-auth.py",
    "args": ["token"],
    "interactiveMode": "Never",
    "provideClusterInfo": false
  }
}
```

Homelab's one-context, no-exec validation remains unchanged. Work's helper
rechecks the private authentication and kubeconfig digests before running Azure.
It uses the fixed `/usr/bin/az` binary with a clean environment, selects the
reviewed subscription and audience, and verifies the session's exact
tenant/subscription/user before requesting a token. Azure CLI rejects combining
`--tenant` and `--subscription` on token requests. The helper selects the reviewed
subscription and verifies the returned token's tenant and subscription, then
converts it into a Kubernetes ExecCredential. Token
expiry must be in the future. Refresh and explicit enrollment share a lock.
Lock waits and enrollment are limited to 15 minutes; token commands to two
minutes. Azure errors, partial tokens and account output are never printed.
Authentication-required and expired sessions return a generic refusal. Network
and permission failures do not start login.

## Enrollment and session persistence

Startup never requests a token or starts device-code login. The first boot
refuses an imported home or even an empty pre-existing Azure cache. After clean
work enrollment, Azure sessions persist only under this home's `.azure`.
No personal desktop, mixed or homelab cache is cloned or mounted.

Only after a separately approved non-production target and account are
available, an operator may run `/scripts/work-aks-auth.py login` in a private
interactive administrative terminal in the `t3` container, which mounts the
reviewed inputs. The inbound SSH sidecar deliberately does not mount those inputs. Enrollment output goes directly to
`/dev/tty`, not process stdout/stderr or pod logs. Non-interactive enrollment
refuses to run. The helper verifies the reviewed account after login. Retry
the token request after enrollment. No live enrollment is part of the offline
checks, and none was authorized for this implementation.

There is no direct vault login, vault CLI, inherited general SSH agent or
legacy authentication fallback. Required organizational instructions, Claude
hooks, cc-safety-net and output redaction stay active. Policy checks use a
credential-free environment and suppress captured output. The Azure cache,
input Secrets and SSH identity paths are in the runtime secret-protection
deny list.

The required policy files are `.cc-safety-net/policy.json` and
`.claude/settings.json`, with additional hooks under `.config/t3-policy/`.
Legacy `.pi/agent/extensions/pi-permission-system/config.json` input remains
digest-checked and valid-base64-checked but is not installed. Existing files
remain inert for rollback. Every approved acceptance check still runs. A check
that requires the removed extension must fail, then be revised and re-reviewed
with a new policies input digest before rollout. Do not silently drop checks.

`pi-permission-system` is not installed or registered, and startup no longer
writes its policy. Its environment-dump and destructive Kubernetes/Helm/Proxmox
command denies are no longer enforced. cc-safety-net does not replace them.

The shared persistence boundaries also apply to work. Safe T3 preferences,
work project trust and unmanaged Codex settings survive restart. Managed
providers, endpoints, launch arguments and MCP servers are rebuilt from the
reviewed inputs. Codex `/etc/codex/requirements.toml` enforces the provider and
exact hub inventory for trusted projects and newly created worktrees, without
rewriting benign checkout config. Config replacements are atomic.

## GitOps rotation and rollout

The app-of-apps keeps self-healing its source. The work child Application stays
manual and points at `2-k3s/13.t3code-work`. Do not change the parent source,
hand-apply a different composition or attach Argo ownership to external Secrets.

1. P1: After private input/grant and routing reviews, provision the stable
   Secrets. Advance the tracked pod annotation
   `t3code.epaflix.com/inputs-grants-revision` from `unprovisioned` to a public
   counter such as `r1`, then manually sync. The counter is not a private digest
   or proof of approval. Missing Secrets block the pod; invalid inputs refuse
   startup.
2. P2: For each rotation, check active sessions and arrange maintenance. Set
   tracked replicas to `0`, manually sync and wait for termination. Update the
   reviewed Secret files, grants and matching private hashes together. Advance
   the tracked revision, restore replicas to `1`, manually sync and verify.
   This prevents an old grant environment from using newly projected files.
3. P3: Roll back by restoring the old reviewed Secrets and advancing the
   tracked counter again. Mounted Secret updates or hand-patched annotations
   alone are not a restart procedure.

The namespace's PreSync image-prepull and idle-rollout jobs use the work image,
not either other backend's image. Rollout RBAC names only its local StatefulSet
and pod. `OnDelete` and retained PVCs preserve sessions and state; the shared
idle check waits for 30 minutes without T3/terminal activity. Image builds,
smokes and publication cover distinct mixed, homelab and work input-derived
tags under the existing public `ghcr.io/spyrospsarras/t3-runtime` repository.

## Dedicated laptop connection

Use a separate directory, file-backed identity, config and tunnel port. The
helper cannot reuse a directory marked for the other environment and does not
change `~/.ssh/config`, import a keyring or use a general SSH agent.

1. A1: Approve routing and obtain the backend's Ed25519 host public key through
   an authenticated administrative channel. Verify its fingerprint separately,
   not through unauthenticated `ssh-keyscan`.
2. A2: Prepare the dedicated work config offline. The hostname below is synthetic:

   ```sh
   python3 2-k3s/13.t3code/env/tools/prepare-laptop.py \
     --environment work --directory "$HOME/.config/t3-work/ssh" \
     --host ssh.work.example.test --port 22 --user node \
     --host-key-file /private/verified-work-host-key.pub
   ```

   Review and provision only that directory's `id_ed25519.pub` into work
   `authorized_keys`. Never provision its private key to the backend.
3. A3: Open the dedicated loopback tunnel:

   ```sh
   bash 2-k3s/13.t3code/env/tools/connect-laptop.sh \
     "$HOME/.config/t3-work/ssh/ssh_config" 23773 work
   ```

   Homelab's default remains port 13773 and alias `t3-homelab`; work uses alias
   `t3-work`. Both use `ssh -F`, pinned host keys, `IdentityAgent none` and no
   agent forwarding. The server permits forwarding only to T3 on port 3773.
4. A4: Check loopback health/discovery, then use the supported T3 pairing flow.
   Review `t3 pair --help` offline. Later, mint a short-lived pairing token
   through a private approved administrative channel with
   `t3 pair --base-dir /home/t3/.t3 --ttl 5m`. Do not put the token in public
   evidence. Select/name the new environment `work`; retain homelab and mixed.
   No pairing token is minted by these tests.

This login is inbound backend access, not an outbound work SSH grant.
Vault-backed keys and outbound SSH integration remain deferred.

## Offline evidence and remaining acceptance

```sh
python3 2-k3s/13.t3code/env/tools/isolated-runtime.test.py -v
python3 2-k3s/13.t3code/env/tools/work-runtime.test.py -v
python3 2-k3s/13.t3code/env/tools/work-aks-auth.test.py -v
bash 2-k3s/13.t3code/env/tools/runtime-variants.test.sh
bash 2-k3s/13.t3code/env/tools/rollout-hash.test.sh
bash 2-k3s/13.t3code/one/tools/sync-shared.sh --check
kustomize build 2-k3s/13.t3code-work >/dev/null
DOCKER_HOST=unix:///var/run/docker.sock T3_RUNTIME_VARIANT=work \
  bash 2-k3s/13.t3code/env/tools/smoke.sh
```

The shared isolated smoke boots twice on a temporary PVC-like Docker volume.
It checks inventory, retained organizational hooks and cc-safety-net rules,
provider/config persistence, real Codex requirements, health, SSH discovery,
rejected agent/unrelated-port
forwarding and tunnel access. Authentication fixtures replace Azure CLI with
an offline executable and test success, expired/auth-required sessions,
non-auth failures, account mismatch, serialization, timeouts and private
explicit enrollment. They do not contact Azure or make model calls. Pi parity
checks reject permission-system installation, registration and its command
while requiring the remaining extensions to load. The former 242 permission
pipeline assertions no longer run.

- L1: Provide exact private inputs/grants and routing approval, then provision
  and manually sync work. Verify the work jobs and continued mixed/homelab
  health. Neither backend is claimed live-accepted by offline tests.
- L2: With a separately approved non-production work target, enroll privately,
  exercise the required policy checks and make one harmless read-only API
  request. No production access is required.
- L3: Pair/select work from the laptop and finish a harmless agent thread and
  provider-delegated task. Confirm environment-specific thread access, record
  identity/history privately, restart and verify persistence and discovery.

Infrastructure/admin and cross-cluster isolation are outside #1777. These
changes separate application configuration and credential access; they do not
provide a firewall against an infrastructure administrator.
