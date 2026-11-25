# Work T3 backend

This is a separate backend, not a deployment claim. Issue #1837 tracks its
first deployment and live acceptance. Mixed and homelab keep their Applications
and homes. Employer approval is confirmed. Azure enrollment, the dedicated
organization GitHub PAT and Tailscale enrollment remain owner steps.

The work namespace `t3code-work` has its own PVC, clean home, T3 identity,
provider grants, histories, SSH host key and `projects/work` checkout. The
sibling overlay uses the small `13.t3code` component for startup, safety,
settings, SSH discovery and update jobs.

The work image includes Azure CLI, `kubelogin` and the shared vault client.
Homelab still has no Azure CLI, `kubelogin`, Azure cache or work grants. No image contains
private instructions, service inventories or credentials.

T3 binds only `127.0.0.1:3773` and advertises no direct LAN endpoint. App
access uses Tailscale or SSH only, not the pod IP. The `t3code` Service retains
the StatefulSet identity with SSH only on 2222; it no longer exposes HTTP.
See [client access](../13.t3code/env/client-access.md) for Tailscale and SSH checks.

Files for the owner's desktops go in `~/handoffs/`, where the desktops pull them over SSH; the pod never pushes them ([handoffs and builds](../29.t3-build/README.md#handoffs)).

## Runtime vault reads

The work overlay uses the same [`vault run` client and sidecar](../13.t3code-homelab/README.md#runtime-vault-reads)
as homelab, including its timeouts, warm-read path, attachment cleanup and
suppression of child stdin, stdout and stderr. The unpatched `@bitwarden/cli`
is pinned to 2026.8.0. Commands consume work passwords, custom fields and
attachments without printing their values:

```sh
vault run --item "$ITEM_ID" --password-env VAULT_PASSWORD \
  --field api-key=VAULT_API_KEY --attachment credentials.json \
  --attachment-env VAULT_FILE -- python3 /private/consume.py
```

Only the UID-1001 `vault` sidecar receives the five `BW_*` keys from the work
namespace's existing `vaultwarden-agent` Secret. It must contain the work agent
login and work organization/collection scope, never the owner's login or the
homelab grant. Argo does not manage this Secret. The retained 1 GiB
`t3code-vault` PVC is separate from the work home and homelab's same-named PVC
in its own namespace. The storage init restores UID-1001 ownership and mode
0700 before each start. T3 and sshd stay UID 1000 without the login environment
or client-data mount. T3 reads only the sidecar's published scope metadata.

Startup rejects every `BW_*` and `BITWARDENCLI_*` variable, including empty
values. `T3_VAULT_CLIENT=1` enables the runner without relaxing that guard.
Organizational instructions, hooks, policy checks and hub inventory stay intact.
The vault sidecar is independent of T3 startup and never starts Azure login.

The API binds only `127.0.0.1:8087`, with no Service or container port. It has
no authentication within the pod. `vault run` checks both organization and
collection and suppresses output, but cannot prevent deliberate exfiltration
through direct API access. Cross-organization isolation also depends on separate
agent accounts.

[Scoped vault writes](../13.t3code/env/vault-writes.md) covers write commands,
trashed-item reads and post-merge checks for both runtimes.

### Post-merge vault acceptance for #1843

Do not run these steps from an unmerged implementation or move the owner's
real items for testing.

1. V1: Wait for the work runtime image referenced by the merged overlay to be
   published. Check for active work and arrange maintenance. Wait for
   `t3code-work` to sync automatically, then let the idle-rollout job replace
   the `OnDelete` pod, or approve one pod replacement during maintenance. Wait
   for all three containers to be ready. Do not sync or restart homelab or
   shared services.
2. V2: Check only the work Secret's key names, not its data. Confirm the new
   work vault PVC is Bound, the `vault` container runs as UID 1001, and only
   that container references `vaultwarden-agent`. Check scope readiness without
   printing scope IDs, login environments, client cache, kubeconfig or logs
   that could contain credentials. Verify work T3 health and retained policy
   checks before testing reads.
3. V3: Through the homelab agent's existing local API, create one synthetic
   item in its published organization and collection. Give it a synthetic
   password, custom field and attachment. Keep its returned item ID only in
   the private administrative process. Sync that agent and prove a positive
   `vault run` read in homelab, checking values in the child with assertions
   and recording only status and elapsed time.
4. V4: Sync the work agent, then request that exact homelab probe ID through
   `vault run` in the work `t3` container. Use `--password-env VAULT_PASSWORD`
   and a child that would create a temporary sentinel. Require runner exit
   code 1 and no sentinel. A missing-item refusal is expected when the work
   account cannot see the item. The homelab positive control proves this was
   an existing item, not a random nonexistent ID.
5. V5: Create a separate synthetic item through the work agent in its own
   scope. Sync and check password, custom field and attachment consumption
   through work `vault run`. Assert the expected values only inside the child.
   Make the child print them to both output streams and verify the runner
   still emits only status. Record cold and warm elapsed times without values;
   reads reuse the resident client rather than logging in per command. Check
   attachment cleanup after success, failure and timeout. Recheck work cluster
   read-only access and organizational hooks without reenrolling Azure.
6. V6: In a `finally` cleanup, permanently delete both synthetic items through
   the agents that created them, not just move them to trash. Sync both agents
   and confirm the probe IDs no longer resolve. Record only pass/fail and
   timings. Leave both organizations free of test items and confirm homelab
   and work remain healthy.

## Keeper

The work pod can also read and write the owner's Keeper vault
(`keepersecurity.eu`), alongside Vaultwarden. The tenant has no Keeper
Secrets Manager, so the `keeper` sidecar holds the **owner's own** Commander
session through persistent login ("stay logged in"). By the owner's choice
the agent reaches every record that login can see: the owner's own folders
and everything shared with the owner, now or later. Only typed records are
served: legacy (v2) and Secrets Manager application (v5) records never are,
and file records only as attachments of a typed record. The owner keeps no production credentials in it.

- Image: Commander `KEEPERCOMMANDER_VERSION` (`../13.t3code/versions.env`) in
  `/opt/keeper`, plus `../13.t3code/env/files/keeper-client.py` installed as
  `/usr/local/bin/keeper`. Work variant only.
- Sidecar: UID 1002, with Commander's `config.json` on its own retained 1 GiB
  PVC `t3code-keeper` (`KEEPER_CONFIG_STORAGE=file`, never an OS keyring). No
  other container mounts that PVC. The sidecar has no Secret, probes or
  container port. It binds its port at start; while it is not logged in it
  refuses requests (between login attempts, at once) and never holds the pod
  NotReady.
- API: plain HTTP on `127.0.0.1:8088`, unauthenticated inside the pod, like
  `bw serve`. A process in the pod that calls `/record` directly can read any
  value in the vault; `keeper run` only stops accidental leaks.

### One-time login

From an admin shell:

```sh
kubectl --context epaflix -n t3code-work exec -it work-0 -c keeper -- keeper login
```

In the Commander shell, run `login <email>` and complete MFA. Then run
`this-device register`, `this-device persistent-login on`,
`this-device 2fa_expiration forever` and `this-device timeout 30d` (or your
enterprise maximum). Leave with `quit`, not `logout`: `logout` ends the
persistent login. The sidecar should pick up the session within a minute and
log `keeper: ready` (not yet observed on the live tenant). If the account uses
SSO and persistent login cannot resume without a browser step, the sidecar
logs `keeper: persistent login failed (KeyboardInterrupt)` and keeps waiting.

Persistent login applies to the whole account, so the web, desktop and mobile
apps also stay logged in ([Commander docs][keeper-persistent]). Commander
stores a new clone code in `config.json` after every login
(`post_login_processing` in Commander's `loginv3.py`). Never copy
`config.json` to another machine: the same docs say using one config on two
devices revokes both. `keeper login` and the sidecar's own re-logins share a
file lock, so they never overlap.

Every request syncs at most once per 10 seconds, and an idle sidecar syncs
every 5 minutes. That should keep the inactivity timer from running out, but
it has not been observed on the live tenant yet. If Keeper still ends the
device session (policy, timeout or revocation), the sidecar logs
`keeper: not logged in; run keeper login ...`; repeat the login above. If
Keeper no longer recognises the device at all, Commander registers a new one
and overwrites `config.json`, so the login also has to be repeated.

SIGTERM sets a flag: the sidecar finishes the request or login in progress,
then exits. The sidecar turns off Commander's throttle back-off
(`fail_on_throttle`), but a slow read can still block for minutes, so 45 s
after the signal the sidecar exits regardless, before the pod's 60 s grace
period ends. A cut-off that lands exactly while a login writes
`config.json` could leave it unreadable; if the sidecar then reports
`not logged in`, repeat the login above.

[keeper-persistent]: https://docs.keeper.io/en/keeperpam/commander-cli/commander-installation-setup/logging-in#persistent-login-sessions-stay-logged-in

### Agent commands

```sh
keeper list [filter]                        # metadata only: uid, title, type, field names, attachment names
keeper run (--uid UID | --title TITLE) [--password-env KEEPER_X] [--login-env KEEPER_X] \
  [--field NAME=KEEPER_X] [--attachment NAME --attachment-env KEEPER_X] -- <command>
keeper create --input-file /tmp/private.json   # or --input-env KEEPER_DOC
keeper update --uid UID --input-file /tmp/private.json
keeper attach --uid UID --file /tmp/private/cred.bin
keeper trash --uid UID
```

- `run` works like `vault run`. Values reach only the child process. The
  child gets no stdin, its output is discarded, and inherited `KEEPER_*` and
  `KSM_*` variables are removed. `--field` matches a field's label, or its
  type if it has no label.
- Documents use Keeper's record JSON:
  `{"type": "login", "title": "...", "notes": "...", "fields": [{"type": "password", "value": ["..."]}], "custom": [{"type": "text", "label": "api", "value": ["..."]}]}`.
  `create` needs `type` and `title`. `update` replaces each top-level key you
  supply and keeps attachments and record links. Documents cannot contain
  folder keys or link fields (`fileRef`, `addressRef`, `cardRef`,
  `recordRef`, `script`); `attach` manages files. New records go to the root
  of My Vault.
- `attach` replaces an attachment with the same file name: the new file is
  linked and the old link removed in a single record update.
- `trash` runs Commander's own `rm` on the record, which unlinks it from every
  folder that holds it. That should send it to Deleted Items, but this has not
  been tested against the live tenant. For a record in a shared folder,
  Commander unlinks it from that shared folder itself
  (`commands/record.py`, `RecordRemoveCommand`), which likely removes it for
  every member of the folder; this is untested too. If the enterprise
  enforces master-password re-entry for record changes, `trash` fails before
  any change is sent, but reports exit `2` because the sidecar cannot tell.
- While the sidecar is not logged in, every command exits `1` with
  `keeper: the keeper sidecar is not logged in; ask the owner to run ...`.
- Refusals print only the exception type and the client's own call chain.
  Exit code `2` means a write was sent but not confirmed: check the record in
  the web vault before you retry.

Offline checks: `python3 2-k3s/13.t3code/env/tools/keeper-client.test.py -v`.
They run against fake Commander objects. Nothing here has been run against a
live Keeper tenant yet.

## Network isolation

See [network policies and post-merge checks](../13.t3code/env/network-isolation.md).

## Required private provisioning

Review exact inputs and grants for work use. A context name, hostname or
account display name does not prove organizational ownership. The approval
references and digests bind operator-reviewed bytes and grants; they are not
an automatic account classifier or a signature from an external authority.
Do not infer inputs from legacy files or export a desktop or mixed home.

Provision external, stable-name Secrets in `t3code-work` through the approved
private provisioner. Argo owns only the public overlay, never these Secrets.
Keep private values, approval references, hashes and login output out of
public Git, issue comments and logs.

1. I1: `t3-runtime-inputs` requires `runtime.json`, `bundle.json`,
   `policies.json`, `services.json` and `authorized_keys`. All are required;
   there is no default work inventory, policy or endpoint. Kubeconfigs are not
   inputs; see [Kubernetes clusters](#kubernetes-clusters).
2. I2: `t3-runtime-grants` requires a reviewed `model-token` and an independent
   `work.`-prefixed `hub-token`. Add a reviewed `github-token` only with
   `gitAuth: github-token`. Public Git mode rejects a supplied Git grant.
3. I3: Review SSH routing, the LoadBalancer pool and worker placement BEFORE
   merging the first runtime revision. Automatic sync exposes the pinned SSH
   address `192.168.10.114` at `t3code-work.epaflix.lan` even if startup
   refuses inputs. Put required affinity in the tracked overlay first. Both
   jobs inherit that affinity.

Use the private input formats documented in
[`../13.t3code-homelab/README.md`](../13.t3code-homelab/README.md), with these
explicit work differences:

- `environment` is `work` in runtime, policy and service files.
- `hubUrl` is exactly
  `http://mcp-hub.mcp-hub-work.svc.cluster.local:8000`. No mixed or homelab hub
  is registered. Only the work-prefixed hub grant is accepted.
- `projectUrl`, `modelUrl`, instruction/skill bundle and grant hashes come
  from their work reviews. No model or Git grant is copied from another home.
- `skillsUrl` is optional. Startup pulls from the existing clone's origin in
  `~/work/ai-skills`. If that clone is missing and startup finds exactly one
  clone at `~/*/ai-skills` that is not reached through a symlink, it moves that
  clone into place and pulls from it. If there is still no clone, startup
  uses `skillsUrl` to clone it. The URL must be a GitHub HTTPS `.git` URL and be
  covered by the `git` review. With no clone and no URL, startup logs
  `work skills: runtime.json has no skillsUrl; ai-skills clone and update skipped`
  and continues.
- `runtime.json.approvals` requires exactly `git`, `models`, `policies`,
  `hub`, `laptop` and `routing`.
- `runtime.json.sha256` covers the four input files besides `runtime.json`.
  `grantSha256` covers the supplied model, hub and optional Git grants.
- The approved initial `services.json` is
  `{"environment":"work","services":["searxng"]}`. Other services are
  owner-added later. Vault, unknown, empty, duplicate or foreign-environment
  inventories fail closed.

## Kubernetes clusters

The work pod may use any cluster except epaflix, the homelab cluster. There is
no allowlist, pinning or import step. Kubeconfigs live in the persistent home
under `~/.kube/`; `KUBECONFIG` is unset, so kubectl reads `~/.kube/config` by
default. Add a cluster from the pod in either way:

```sh
az aks get-credentials -g <group> -n <cluster>
kubelogin convert-kubeconfig -l azurecli
```

or write a kubeconfig given as text into `~/.kube/`. The Azure session lives in
the home's `~/.azure`. Log in with
`bash 2-k3s/13.t3code-work/tools/owner-login.sh azure [--tenant <id>]` from an
owner terminal, or let the agent run `az login --use-device-code` and complete
the code in a browser.

The pod has no epaflix credential, and its runtime has no egress to the
epaflix API; see [network isolation](../13.t3code/env/network-isolation.md).

### Moving from pinned contexts

Pods from before 2026-11-17 used reviewed `kubeconfig` and `work-auth.json`
inputs and `/scripts/work-aks-auth.py`. To move:

1. K1: Follow the input rotation in P2. Remove the `kubeconfig` and
   `work-auth.json` keys from `t3-runtime-inputs`, drop them from
   `runtime.json.sha256` and drop the `kubeconfig` and `workAuth` approvals.
   Remove the old Kubernetes environment sentence, the one about approved or
   dev clusters and production contexts, from the instructions in
   `bundle.json` and update its digest.
2. K2: Delete the leftover build grant:
   `kubectl --context epaflix -n t3code-work delete secret t3-build-kubeconfig`.
3. K3: After restart, the default Azure session in `~/.azure` still works.
   Sessions under `~/.azure/tenants/` and the generated `~/.kube/work/` files
   are no longer used; delete them. Log in again with `az login --tenant <id>`
   for other tenants, then add each cluster as above.

## Policies and persistence

There is no vault login environment in T3, inherited general SSH agent or
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
rewriting benign checkout config. Config replacements are atomic. The direct
Codex instance is disabled; use Pi with `cliproxy/<model>` IDs. See
[the image-shipped runtime facts](../13.t3code/env/isolated-runtime.md#image-shipped-runtime-facts)
for the instruction block appended without private-input rotation.

## GitOps rotation and rollout

The app-of-apps keeps self-healing its Application definitions. The work child
Application automatically syncs merged revisions with pruning, without
self-healing, and points at `2-k3s/13.t3code-work`. Do not change the parent
source, hand-apply a different composition or attach Argo ownership to external
Secrets.

1. P1: After private input/grant and routing reviews, provision the stable
   Secrets. Advance the tracked pod annotation
   `t3code.epaflix.com/inputs-grants-revision` from `unprovisioned` to a public
   counter such as `r1`, then review and merge for automatic sync. The counter
   is not a private digest or proof of approval. Missing Secrets block the pod; invalid inputs refuse
   startup.
2. P2: For each rotation, follow the shared
   [input rotation procedure](../13.t3code/env/isolated-runtime.md#input-rotation),
   except for [skills-only changes](../13.t3code/env/isolated-runtime.md#skills-only-rotation).
   Otherwise, pause `app-of-apps` first, then `t3code-work`, before scaling the
   StatefulSet to zero. Update reviewed inputs, grants and matching hashes together, then
   advance the tracked revision. Resume from the merged revision only after
   private inputs are consistent. The parent would undo a child-only pause.
   This prevents an old grant environment from using newly projected files.
3. P3: Roll back by restoring the old reviewed Secrets and advancing the
   tracked counter again. Mounted Secret updates or hand-patched annotations
   alone are not a restart procedure.

The work hub inventory is `searxng` and `notion`. The owner adds Notion with
fresh work-account consent through
`2-k3s/23.mcp-hub/tools/bootstrap-notion.py --instance work --context <approved-context>`,
then stages the
matching hub inventory and runtime allowlist. The work hub's
`mcp-hub.epaflix.com/inputs-revision` and this StatefulSet's
`t3code.epaflix.com/inputs-grants-revision` counters roll the change out.

The namespace's PreSync image-prepull and idle-rollout jobs use the work image,
not either other backend's image. Rollout RBAC names only its local StatefulSet
and pod. `OnDelete` and retained PVCs preserve sessions and state; the shared
idle check waits for 30 minutes without T3/terminal activity. Image builds,
smokes and publication cover distinct mixed, homelab and work input-derived
tags under the existing public `ghcr.io/spyrospsarras/t3-runtime` repository.

## First deployment for #1837

Run provisioning from a clean checkout in a private administrative terminal.
The tools use only `kubectl --context epaflix`, never a work Azure cache or an
existing GitHub login. They report the failed check without exposing private
command/API diagnostics and never sync Applications. Do not run with Python
tracing or shell recording enabled.

1. B1: Stage independent grants from an in-cluster administrative environment:

   ```sh
   python3 2-k3s/13.t3code-work/tools/provision.py grants
   ```

   This creates `t3code-work` and `mcp-hub-work` if absent, then external
   Secrets `t3-runtime-grants`, `mcp-hub-instance` and `mcp-hub-searxng` in
   their respective namespaces. The model key has the reserved `t3-work-`
   prefix. The proxy management API appends it without replacing another
   client's keys. Registration refuses another key with the same masked
   four-character prefix. Re-running reuses grants and verifies model
   discovery. It checks only the `t3code-work` client digest and the searxng
   upstream, refusing changes to either owned entry. Other clients, upstreams
   and service inventory remain unchanged and do not block a rerun.
2. B2: The owner creates the org-scoped GitHub grant before first boot:

   ```sh
   bash 2-k3s/13.t3code-work/tools/owner-login.sh github
   ```

   On the GitHub PAT page, create a dedicated fine-grained PAT with the
   organization as resource owner, only the needed org repositories including
   at least one private repository, Metadata read + Contents read, and a short
   expiry. Approve the request in the organization settings if GitHub requires it.

   The script asks only for the token, hidden. An existing grant is never
   overwritten.
3. B3: Prepare the five private files listed in I1 in a dedicated private
   directory. Keep required organizational instructions, skills, hooks and
   actual allow/deny checks. Do not export a mixed home or use test fixtures.
   Use `gitAuth: github-token`, set
   `modelUrl` to `http://cliproxy.remote-pi.svc.cluster.local:8317`, and
   include the approved public login keys from both homePC and laptop.

   ```sh
   python3 2-k3s/13.t3code-work/tools/provision.py inputs \
     --directory /private/reviewed-work-inputs
   ```

   This binds only `runtime.json.grantSha256` to this backend's staged grants,
   validates the complete bundle with the existing runtime validator, then
   creates external `t3-runtime-inputs`. It preserves input hashes, approval
   references, policy files and policy checks. It cannot replace existing
   inputs or stage them while a work pod exists. Required private inputs are
   not supplied by this repo; missing input or policy reviews block startup.
4. B4: Review and merge the work manifests and proxy transcript change. Once
   B2/B3 and routing review pass, advance the tracked work StatefulSet revision
   from `unprovisioned` to `r1` through a reviewed change. Do not hand-patch it.
   From an authenticated Argo administrative terminal, after the merge:

   ```sh
   argocd app sync remote-pi
   kubectl --context epaflix -n remote-pi rollout status \
     deployment/cliproxy --timeout=300s
   argocd app wait mcp-hub-work --sync --health --timeout 300
   kubectl --context epaflix -n mcp-hub-work rollout status \
     deployment/searxng-mcp --timeout=300s
   kubectl --context epaflix -n mcp-hub-work rollout status \
     deployment/mcp-hub --timeout=300s
   argocd app wait t3code-work --sync --health --timeout 1800
   kubectl --context epaflix -n t3code-work wait --for=condition=Ready \
     pod/work-0 --timeout=1800s
   ```

   The proxy transcript field `client_backend: work` maps the reserved key
   prefix after CLIProxyAPI masks the credential as four prefix characters,
   `...`, then four suffix characters. This is request attribution, not an
   authorization decision.
   Verify a harmless model call with the private work grant, then query Loki
   with this filter:

   ```logql
   {namespace="remote-pi",container="transcripts"}
     |= `"client_backend": "work"`
   ```

   Do not query by raw key or publish raw payloads.
5. B5: Only after work startup passes, the owner logs in to Azure in the new home:

   ```sh
   bash 2-k3s/13.t3code-work/tools/owner-login.sh azure
   ```

   Azure session files remain in the work PVC, not a Kubernetes Secret or the
   mixed home. Add clusters as in [Kubernetes clusters](#kubernetes-clusters).
   Connect homePC and laptop: [pair the app over Tailscale](../13.t3code/env/client-access.md#tailscale).
   Complete one harmless thread on each, verify instructions/hooks, discovery,
   read-only cluster access and restart persistence before accepting #1837.

`mcp-hub-work` exposes only `/searxng` initially. Its separate `searxng-mcp`
Deployment reuses the maintained public search module, with its own bearer
credential and no Kubernetes ServiceAccount token. Ordinary work, homelab and
legacy client grants cannot call that upstream directly. Only the hub holds
the upstream credential. It sends public search queries to the existing
SearXNG service, never to a work data source. Treat search results as untrusted.

For hub Secret changes, follow
[hub provisioning A3](../23.mcp-hub/instances/README.md).
Runtime grant/input rotations still require the stop/change/revision/start
procedure above; these first-setup tools refuse rotation.

## App access

Apps reach this pod over Tailscale at
`http://t3code-work.tailea078a.ts.net:3773`; T3 Connect is unlinked. Follow the
shared [Tailscale and Zed SSH procedure](../13.t3code/env/client-access.md).

## Dedicated laptop diagnostic connection

The SSH tunnel below is an acceptance and diagnostic check, not the normal
client connection.

Use a separate directory, file-backed identity, config and tunnel port. The
helper cannot reuse a directory marked for the other environment and does not
change `~/.ssh/config`, import a keyring or use a general SSH agent.

1. A1: Approve routing and obtain the backend's Ed25519 host public key through
   an authenticated administrative channel. Verify its fingerprint separately,
   not through unauthenticated `ssh-keyscan`.
2. A2: Prepare the dedicated work config offline. The hostname below is
   synthetic:

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
4. A4: Check `http://127.0.0.1:23773/health` and
   `http://127.0.0.1:23773/.well-known/t3/environment`. For app access,
   [pair the app over Tailscale](../13.t3code/env/client-access.md#tailscale); retain homelab and mixed.

This login is inbound backend access, not an outbound work SSH grant.

## Outbound work SSH

The work vault sidecar uses the shared stock vault client's SSH agent and
UID-1001 socket proxy from #1842. It loads only SSH key items in its configured
work organization and collection. Approved aliases select
`IdentityAgent /run/t3-vault/ssh-agent.sock`, which permits listing and signing,
not key mutation. T3 processes and SSH login sessions do not receive
`SSH_AUTH_SOCK`. No homelab config or key material is mounted. Private keys
pass to `ssh-add` over stdin with output suppressed, not through files or logs.
An empty collection starts an empty agent without a fallback. With no approved
hosts, the agent stays empty and does not fetch SSH key items, even if the work
collection contains them. The sidecar reads `hosts.json` when it starts, so approving the first host takes effect at the next sidecar start.
Invalid SSH key items refuse startup when SSH is enabled. Every 10 seconds the sidecar syncs the vault and reloads the agent when the scoped SSH key set changes.
A failed sync or a bad key keeps the running agent. Agent connections opened before a reload fail once and must reconnect.

`github.com` is the one hostname target. The generated config sends it
to the same agent with `User git`, port 22 and GitHub's published host keys
pinned in `github_known_hosts`; a `HostName` override falls to the deny block.
The public IPv4 egress rule already reaches GitHub on TCP 22, so no policy
change is needed. GitHub must know a key from the work collection. Because the
agent loads keys only while approved hosts exist, GitHub SSH also needs at least
one approved host. Check with `ssh -T git@github.com`: exit code 1 is success.

The owner must supply these private inputs before enabling outbound work SSH.
They are not required to start or use the work pod:

- S1: The exact approved non-production work hosts, login users and SSH ports,
  plus their verified public host keys. Use literal fixed IP addresses, not
  DNS names or subnets, so SSH and egress use the same targets. Obtain host keys
  over an authenticated administrative channel, not `ssh-keyscan`.
- S2: Which SSH key items belong in the configured work organization and
  collection. Every type-5 SSH key item in that scope loads. Move only approved
  work SSH keys there and authorize their public keys on the approved hosts.
  Homelab items must remain in the homelab organization. Existing `vaultwarden-agent`
  scope IDs select the organization and collection; do not copy credentials
  from the homelab Secret or put item names/IDs in this public repository.
- S3: Approval to add the exact target/port egress rules below and to perform
  one harmless `true` check. The tracked work NetworkPolicy still denies
  general LAN access. The generated private policy adds only the approved
  destinations, not a LAN CIDR or a general SSH rule.

Keep the host list outside Git. A private JSON array has this shape, using
synthetic, non-deployable values here:

```json
[
  {
    "alias": "work-dev",
    "address": "192.0.2.10",
    "user": "node",
    "port": 22,
    "hostKey": "ssh-ed25519 <verified-base64-public-host-key>"
  }
]
```

The parser accepts up to 32 unique literal aliases, fixed IPv4/IPv6 addresses and
Ed25519, RSA or NIST P-256 host keys. It rejects wildcard aliases, unknown
fields and SSH directives. Loopback, multicast, unspecified and link-local
addresses are not work targets. The owner, not the parser, confirms that each
target is non-production and work-only.

Run these commands in a private administrative terminal after review and merge.
Do not capture the JSON, generated SSH config or policy in model-visible
output. No Argo sync or pod restart is performed by the implementation.

```sh
umask 077
kubectl --context epaflix -n t3code-work create secret generic t3-work-ssh \
  --from-file=hosts.json=/private/work-ssh.json
python3 2-k3s/13.t3code-work/files/work-ssh.py policy \
  /private/work-ssh.json /private/work-ssh-policy.json
kubectl --context epaflix apply -f /private/work-ssh-policy.json
```

`t3-work-ssh` is a separate private Secret rather than an addition to the
five-file, digest-checked `t3-runtime-inputs` contract. Its projection is
optional. A missing Secret or missing, zero-byte, whitespace-only or `[]`
`hosts.json` keeps startup usable, replaces any previous work host config with
an empty one, and starts an empty agent. The config installer logs exactly
`work SSH: no approved hosts; SSH disabled`. No work SSH egress policy is added.
A present nonempty invalid input still refuses startup.

The `t3` and vault containers mount the same read-only input and public parser.
Both use that parser, so invalid input prevents the agent from loading keys.
Startup replaces both the runtime and login user's SSH config and pins the
supplied host keys only for approved aliases at their approved IP addresses.
Overriding an alias's hostname to another address also disables the agent and
pinned trust. Unknown aliases use
`IdentityAgent none`, strict host key checking, no known-host trust file and
`ProxyCommand false`.
Passwords, local identity files, agent forwarding and unpinned host keys are
disabled. The inbound
SSH sidecar uses the generated config in the shared home without mounting the
private host input or vault data.

For rotation, stop the work pod under the existing approved stop/change/start
procedure. Replace `t3-work-ssh` privately, regenerate and apply the same named
policy from the new host list, update vault SSH items as approved, then start
work. For an empty host list, applying the generated policy removes its old
SSH allow rules. Removing the Secret does not remove a previously applied
private policy, so revoke those rules as part of the same change. Do not leave
the old policy under another name. Both config and known
hosts are replaced, not appended, and the new agent cannot sign with removed
keys. A stopped input rotation resumes through the shared procedure. For an
image-only `OnDelete` update, automatic sync leaves replacement to the
idle-rollout job or an owner-approved earlier pod replacement.

After rollout, run `ssh <approved-alias> true` in work and record only success
or failure privately. Confirm an unapproved alias is refused, the agent has
only the expected work public-key fingerprints, and no homelab aliases or keys
are present. Repeat after replacing a synthetic work key and waiting for the
sidecar's `vault: SSH agent reloaded` log line to prove the old key is gone,
then permanently delete any synthetic vault item.
No real work target or vault key has been exercised by offline tests.

## Offline evidence and remaining acceptance

```sh
python3 2-k3s/13.t3code-work/tools/provision.test.py -v
python3 2-k3s/13.t3code/env/tools/work-ssh.test.py -v
python3 2-k3s/13.t3code/env/tools/vault-client.test.py -v
python3 2-k3s/13.t3code/env/tools/vault-startup.test.py -v
python3 2-k3s/13.t3code/env/tools/startup.test.py -v
python3 2-k3s/13.t3code/env/tools/image-commands.test.py -v
python3 2-k3s/17.remote-pi/cliproxy/tools/client-backend.test.py -v
bash -n 2-k3s/13.t3code-work/tools/owner-login.sh
shellcheck 2-k3s/13.t3code-work/tools/owner-login.sh
python3 2-k3s/13.t3code/env/tools/isolated-runtime.test.py -v
python3 2-k3s/13.t3code/env/tools/work-runtime.test.py -v
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
rejected agent/unrelated-port forwarding and tunnel access. It does not
contact Azure or make model calls. Pi parity
checks reject permission-system installation, registration and its command
while requiring the remaining extensions to load. The former 242 permission
pipeline assertions no longer run.

- L1: Provide exact private inputs/grants and routing approval, then provision
  before merging the runtime revision and wait for automatic sync. Verify the
  work jobs and continued mixed/homelab health. Neither backend is claimed
  live-accepted by offline tests.
- L2: Log in to Azure, add one work cluster, exercise the required policy
  checks and make one harmless read-only API request. Confirm the epaflix API
  is unreachable from the `t3` container.
- L3: Select work over Tailscale from the laptop and finish a harmless
  agent thread and provider-delegated task. Confirm environment-specific
  thread access, record identity/history privately, restart and verify
  persistence and discovery.

Infrastructure/admin and cross-cluster isolation are outside #1777. These
changes separate application configuration and credential access; they do not
provide a firewall against an infrastructure administrator.
