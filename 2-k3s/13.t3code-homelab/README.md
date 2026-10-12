# Homelab T3 backend

This backend is independent of the existing mixed backend. It has its own
namespace, PVC, application identity, provider settings, histories, credentials
and project checkout. Nothing imports the mixed home. The public Application
requires manual sync and cannot start without approved private inputs.

The sibling layout lets this overlay consume the `13.t3code` Kustomize
component without copying shared scripts or relaxing Kustomize's file loader.
The component shares the maintained safety installer, package setup, hub
client generator, SSH launcher workaround and update jobs. Its entrypoint,
Pi launcher and SSH server use the isolated-runtime paths. This is the small
pattern for the later work backend; no work backend is implemented here.

## Private inputs

Provision stable-name Secrets out of band through the approved secret
provisioner, in namespace `t3code-homelab`. Argo renders only the tracked public
overlay and references those names. Never copy the mixed PVC, T3 database,
provider login, personal keyring, cloud cache or vault login. The policies and
instructions must be a reviewed homelab slice that retains required organizational rules and hooks.
The synthetic test bundle is not that policy approval.

1. I1: `t3-runtime-inputs` contains `runtime.json`, `bundle.json`,
   `policies.json`, `services.json`, `kubeconfig` and `authorized_keys`.
2. I2: `t3-runtime-grants` contains `model-token` and a separately minted
   `homelab.`-prefixed `hub-token`. Add `github-token` only if `gitAuth` is
   `github-token`. `public` rejects a supplied Git token. These are independently
   reviewed homelab grants, not the existing mixed grants.
3. I3: Approve SSH routing BEFORE the first manual Application sync. The
   LoadBalancer Service allocates an address at sync, even if pod input
   validation fails. `runtime.json` cannot authorize or prevent that allocation.
   Review the configured pool and reachable network first. Use the allocated
   address only after that approval. Any required worker affinity belongs in the
   tracked overlay before sync, not in private pod inputs. The component copies
   StatefulSet affinity to both update jobs.

`runtime.json` uses this shape. The endpoint examples and digest placeholders
are deliberately not deployable:

```json
{
  "environment": "homelab",
  "hubUrl": "http://mcp-hub.mcp-hub-homelab.svc.cluster.local:8000",
  "modelUrl": "https://models.homelab.example.test",
  "projectUrl": "https://github.com/homelab/approved-project.git",
  "gitAuth": "public",
  "approvals": {
    "git": "private-review-reference",
    "models": "private-review-reference",
    "policies": "private-review-reference",
    "kubeconfig": "private-review-reference",
    "hub": "private-review-reference",
    "laptop": "private-review-reference",
    "routing": "private-review-reference"
  },
  "sha256": {
    "bundle.json": "<sha256-of-exact-file-bytes>",
    "policies.json": "<sha256-of-exact-file-bytes>",
    "services.json": "<sha256-of-exact-file-bytes>",
    "kubeconfig": "<sha256-of-exact-file-bytes>",
    "authorized_keys": "<sha256-of-exact-file-bytes>"
  },
  "grantSha256": {
    "model-token": "<sha256-of-approved-token-without-trailing-newline>",
    "hub-token": "<sha256-of-approved-token-without-trailing-newline>"
  }
}
```

For token-authenticated Git, add the approved `github-token` digest. Keep
fingerprints private too. Startup verifies every file and credential grant
against the reviewed input. A missing input, wrong environment, changed
unreviewed grant, legacy SSH-agent/vault environment, or first-boot home
containing existing files stops startup. Endpoint URLs cannot embed credentials.
Only this namespace's homelab hub URL is accepted; there is no legacy fallback.

`services.json` is the approved homelab hub client inventory described in
[`../23.mcp-hub/instances/README.md`](../23.mcp-hub/instances/README.md).
Unknown, empty, duplicate, work or vault inventories fail closed. Startup
rebuilds the Pi, Claude and Codex MCP lists from that inventory, dropping
unrelated servers as well as retired hub servers. Approved Claude settings
cannot auto-enable project servers. Startup clears project MCP maps and their
cached enable lists in both Claude state files. It also clears MCP entries and
auto-enable settings from retained home-local and homelab-project settings,
retaining unrelated state and hooks. It clears only the server map in the
project's `.mcp.json`. Already-compliant project files keep their original bytes;
startup writes them only when a managed value changes. Required hooks cannot
be disabled by those retained settings. The hub remains the application
authentication boundary; config generation is not a firewall.

The kubeconfig must contain exactly one context, cluster and user. It needs
HTTPS, an inline CA and an inline token or client certificate/key. Exec
plugins, cloud auth providers, external credential files and insecure TLS
are rejected. Agents use only this read-only kubeconfig through `KUBECONFIG`.

`bundle.json` has the existing private-config install format. `sourceHome`
is the source path to rewrite in Markdown; `files` maps `instructions.md`
and reviewed `skills/...` paths to `{ "data": "<base64>", "executable": false }`.
Provide the required instructions and skills explicitly, not a mixed-home
export. T3's state and credentials are not bundle entries.

`policies.json` has `environment`, `files` and `checks`. Files use the same
base64 entry format. Required targets are:

- `.cc-safety-net/policy.json`
- `.pi/agent/extensions/pi-permission-system/config.json`
- `.claude/settings.json`

Additional hook files may live under `.config/t3-policy/`. Required Pi
command denies must be retained. Secret-protection overrides cannot disable
checks. Claude hooks must be present and enabled. Startup retains the approved
policy files, adds the runtime's credential paths to the secret deny list,
and runs each approved harmless acceptance check with no credential-bearing
environment. A check is `{ "argv": ["python3", "${HOME}/.config/t3-policy/check.py"],
"exit": 0 }`. Checks must exercise the actual required policy or hook, with
both harmless allows and expected denies, not merely return success.
Every command has a 30-second limit; captured output is not printed.
The fixture suite demonstrates a retained private rule and a hook that denies
a synthetic Git push without making one.

The reviewed cc-safety-net extension, permission-system package and output
redaction remain installed. Pi package registration must pass before T3
starts. Startup also requires usable model discovery at the approved endpoint
on first boot and rejects a retained provider configuration for a different
endpoint. These are application safety layers, not protection against an
infrastructure administrator or a user editing their own runtime files.

## Persistent configuration boundaries

T3 retains the approved `pi` and `codex` instances' display names, enabled
preferences and custom model IDs under `cliproxy/` and `codex/`, respectively.
Their default/text-generation selections and options survive if they use those
instance/model prefixes. Startup resets driver identities, executable paths
and launch arguments from the maintained configuration. Extra provider
instances, legacy `providers`, unapproved custom model IDs and selections are
removed. `claudeAgent` remains disabled because its bare model names cannot use
the approved proxy routing. Credentials and endpoints are always managed by
the reviewed inputs, not app preferences.

Codex retains unmanaged TOML settings, including model/reasoning preferences,
features, UI settings and trust entries for the homelab project and its
subdirectories. Startup replaces `model_provider`, the entire `model_providers`
map and the MCP map. It removes `profile` and `profiles` to prevent alternate
provider/MCP overrides, and removes trust entries outside the homelab project.
The rewritten TOML preserves values, not comments or formatting. Startup does
not select or import separate profile files. It atomically replaces Codex TOML
through a private temporary file, so failed writes keep the last good file.

Startup also writes the pinned Codex CLI's supported
`/etc/codex/requirements.toml`. It requires the approved provider and its
endpoint definition and allowlists MCP servers by exact name and hub URL.
Codex applies these requirements when loading configuration, including trusted
project directories, subdirectories and T3 worktrees created after startup.
Project MCP additions and approved-name entries redirected to another URL are
disabled by Codex rather than contacting an unapproved server. Benign project
settings still apply, and project `.codex/config.toml` files are not rewritten.
The offline smoke test checks both `mcp list` and the app-server config API with
the real pinned CLI. This uses requirements, not undocumented flags or wildcard
trust entries. Deliberately rewriting runtime files or selecting external
configuration remains outside this boundary.

## Provisioning and rotation

The app-of-apps owns the Application source and self-heals it. Leave that source
pointing at `2-k3s/13.t3code-homelab`; do not hand-apply another composition or
edit the child Application. This overlay intentionally does not own Secrets,
so its reconciliation cannot replace provisioned data or change Secret names.
The backend still requires manual sync. Parent self-heal remains enabled.

1. P1: Obtain the private input, grant and routing reviews. Create the namespace
   through the approved provisioner if it does not yet exist, then provision
   `t3-runtime-inputs` and `t3-runtime-grants` with the keys in I1/I2. Use those
   exact stable names, not content-hashed names. Keep plaintext, private digests,
   endpoint details and approval references outside public Git and command logs.
   Do not attach Argo ownership/tracking annotations to these external Secrets.
2. P2: Advance the tracked pod-template annotation
   `t3code.epaflix.com/inputs-grants-revision` in `statefulset.yaml` from
   `unprovisioned` to a non-sensitive counter such as `r1`. This counter records
   an input/grant update, not proof of approval and not a digest of private data.
   After reviews and provisioning, manually sync the public Application. Missing
   Secrets block the pod; missing, mismatched or unreviewed inputs fail startup.
3. P3: For EVERY later input or grant rotation, first check for active sessions
   and arrange maintenance. Set tracked `spec.replicas` to `0`, manually sync
   and wait for the pod to terminate before changing external Secrets. Update
   the reviewed files, grants and matching private digests together. Advance
   the same tracked revision counter, restore tracked replicas to `1`, then
   manually sync and verify startup, health and the approved policy checks.
   This avoids old environment grants running with newly projected input files.
   Restoring the old Secrets and advancing the counter again is the rollback
   procedure. Never rely on a hand-patched annotation, an edited child source
   or mounted Secret updates alone to restart the process.

The shared PreSync Job waits for the homelab image on the chosen worker.
The namespace's idle-rollout job has RBAC only for its own StatefulSet and
pod. `OnDelete` waits until T3 and terminal input have been idle for 30 minutes;
a crash-loop can roll sooner. Each container points the idle check at this
home's T3 database. PVC pruning/deletion/scaling retains the home. A manual
pod deletion, after checking for active sessions, applies a pending revision.

## Laptop preparation

The offline helper creates a dedicated, file-backed laptop login identity
and a separate SSH config. It reads no existing keyring or SSH agent. Only
its public key goes into the backend's approved `authorized_keys`. Private
keys stay on the laptop. Vault-backed key integration remains disabled.

1. A1: Approve the homelab SSH routing and obtain the backend's Ed25519 host
   public key through an authenticated administrative channel. Verify its
   fingerprint out of band. Do not trust an unauthenticated `ssh-keyscan`.
2. A2: Prepare the laptop, using a new dedicated directory and the approved
   endpoint. This synthetic hostname is only an example:

   ```sh
   python3 2-k3s/13.t3code/env/tools/prepare-laptop.py \
     --directory "$HOME/.config/t3-homelab/ssh" \
     --host ssh.homelab.example.test --user node \
     --host-key-file /private/verified-homelab-host-key.pub
   ```

3. A3: Review and provision that directory's `id_ed25519.pub` as a login key.
   The key is option-free Ed25519. Server settings require public-key login,
   disable agent forwarding/passwords, and permit forwarding only to T3 on
   loopback port 3773. This login does not grant agents outbound homelab SSH.
4. A4: Open a loopback-only tunnel using the separate config:

   ```sh
   bash 2-k3s/13.t3code/env/tools/connect-laptop.sh \
     "$HOME/.config/t3-homelab/ssh/ssh_config" 13773
   ```

   Keep it running. It pins the host key, sets `IdentityAgent none`, disables
   general agent forwarding and uses only the dedicated key. It mounts or
   copies no laptop home/cache. Check `http://127.0.0.1:13773/health` and
   `http://127.0.0.1:13773/.well-known/t3/environment`.
5. A5: Pair the laptop's T3 application with that remote URL using T3's
   supported headless pairing flow. Mint the short-lived pairing token from
   this backend with `t3 pair --base-dir /home/t3/.t3 --ttl 5m`, through the
   approved administrative channel. Review the scopes printed by
   `t3 pair --help`; repeated `--scope` flags replace the default scope set. Enter it privately in the application, never in
   issue evidence. Name the environment `homelab`; retain the mixed entry.
   Selecting this URL connects to this backend's own state. The SSH launcher
   workaround and discovery record also remain available for an approved
   T3 SSH-target connection, but this helper deliberately uses `ssh -F` so
   the laptop's broader SSH configuration cannot override it.

## Offline checks

From the repository root, without any real Secrets or live targets:

```sh
python3 2-k3s/13.t3code/env/tools/isolated-runtime.test.py -v
bash 2-k3s/13.t3code/env/tools/runtime-variants.test.sh
bash 2-k3s/13.t3code/env/tools/rollout-hash.test.sh
bash 2-k3s/13.t3code/one/tools/sync-shared.sh --check
kustomize build 2-k3s/13.t3code-homelab >/dev/null
DOCKER_HOST=unix:///var/run/docker.sock bash 2-k3s/13.t3code/env/tools/smoke.sh
DOCKER_HOST=unix:///var/run/docker.sock T3_RUNTIME_VARIANT=homelab \
  bash 2-k3s/13.t3code/env/tools/smoke.sh
```

The Docker smoke uses synthetic grants, a loopback model-list server and a
synthetic local checkout. It builds the reviewed safety extension from public
sources. It does not contact a hub, cloud, vault or live homelab target.
The homelab test boots twice on one temporary volume, checks CLI inventory,
including absence of `az`, `bw` and `kubelogin`, and checks `t3 pair` exposes
`--base-dir`, `--scope` and `--ttl` without minting any tokens. Synthetic renders
cover both overlays, the stable Secret references and the tracked rollout
revision. The smoke checks policy enforcement, SSH login/discovery, rejected
agent forwarding and unrelated-port forwarding, and tunnel health. It verifies the same T3
application identity, SSH host key and fixture history in T3's SQLite file
survive restart. The volume and containers are removed afterward. It makes
no model calls and does not claim to have completed an agent thread.

## Live acceptance still required

Parent-led acceptance needs the private inputs above, approved routing and a
reachable laptop. The known laptop SSH timeout is not a credential diagnosis
and does not block offline tests.

- L1: Confirm both backends are available. Pair and select `homelab` on the
  laptop, complete a harmless agent thread and a provider-delegated task,
  then verify their thread access is confined to the homelab environment.
- L2: Verify the approved policy hooks with harmless allow/deny fixtures and
  one read-only request to an explicitly approved homelab target. Check that
  unavailable vault/outbound SSH integrations remain disabled.
- L3: Record the backend identity and test thread, restart the homelab pod,
  then confirm health, SSH discovery, project and history persistence. Check
  the homelab image-prepull and idle-rollout jobs and continued mixed health.

Issue #1776 is not live-accepted until those checks pass. Vault integration
and vault-backed SSH keys remain in their dependent tickets. Infrastructure
firewall, VM hardening and administrator/cross-cluster isolation are deferred.
