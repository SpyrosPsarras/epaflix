# Isolated runtime input contract

The homelab and work overlays use `env/files/isolated-config.py` to validate
private inputs before installing agent configuration. Each home has its own
environment marker, state and checkout. First boot requires a clean home.
Cloud caches, legacy vault or SSH-agent environment variables, mismatched
input digests and credential-bearing endpoint URLs fail validation.

## Private input files

`runtime.json` selects the environment, its exact in-cluster hub URL, approved
model endpoint, GitHub HTTPS project and Git authentication mode. Its `sha256`
map binds every projected input file. `grantSha256` binds every selected grant.
The homelab provisioner computes these fields rather than requiring manual
fingerprint copying. The namespace's ordinary client hub token must have its
own environment prefix. `public` Git mode rejects a supplied GitHub token.

`bundle.json` contains `sourceHome` and a `files` map. Each entry is
`{"data":"<base64>","executable":false}`. The installer rewrites source-home
references in Markdown. Required entries are `instructions.md` and at least
one `skills/.../SKILL.md`. Other home files, histories, T3 state and login
credentials are not bundle entries.

`services.json` follows the [hub inventory contract](../../23.mcp-hub/instances/README.md).
Startup replaces managed MCP lists with that environment's allowlist and
clears unrelated project MCP entries and cached auto-enable choices. A hub
configuration is an application boundary, not a network firewall.

## Image-shipped runtime facts

Both isolated images ship `env/files/isolated-guidance.md` at
`/opt/t3/isolated-guidance.md`. Startup appends it to the installed private
instructions shared by `~/.claude/AGENTS.md` and `~/.codex/AGENTS.md`.
Pi copies those instructions to `~/.pi/agent/AGENTS.md` and adds its SSH guide.
Codex's override includes the same instructions plus the variant's SSH guide.
The installed cache includes the guidance in its content hash, so image updates
replace the block rather than repeat it. Private bundles stay unchanged;
updating these runtime facts needs no private-input rotation.

The block covers the stock vault client's syntax, `VAULT_` target-variable
restriction, exact item-name matching, discarded child output, unavailable agent
`bw` and Vaultwarden MCP tools, and model routing. Non-secret results must be
written by the child to a file in a private temp directory and read afterwards.
Personal instructions and skills remain private bundle inputs. SSH guidance
stays in each variant's own guide, with access determined by its generated SSH
config rather than image-baked approval state. Homelab keeps its existing guide;
work's placeholder points to its work guide and host approval procedure.

## Personal instructions and skills

The mixed backend's `env/files/private-config.py` and its synchronized `one/`
copy read `agent/AGENTS.md` and `.agents/skills/` below `--home`. The owner
runs `python3 env/files/private-config.py pack one/private-agent-config.enc.yaml`
from the `13.t3code` directory on homePC after updating that source. Only SOPS
ciphertext belongs in Git. `sourceHome` records the source home; installation
rewrites its absolute Markdown references for the destination home.

Isolated backends accept the same instruction and skill file format. The
homelab provisioner supplies tracked instructions and ponytail for first
provisioning; the work provisioner accepts a complete private input directory.
Neither needs a new bundle format for additional personal skills. Use the
shared `private-config.py` `bundle(home)` function with a private staged home
containing `agent/AGENTS.md` and `.agents/skills/`, not an imported backend home.
Preserve executable flags and set `sourceHome` to the home referenced by the
staged Markdown. Keep all private staging and backups outside the repository.

Keep environment instructions first, unchanged, with personal preferences
appended. Organizational and environment rules override personal preferences
and skills. Homelab instructions retain the personal-only GitHub and data
boundaries and inventory-only SSH agent. Work instructions retain all
organizational rules and must not add outbound SSH access. Exclude remote-thread
handoff skills and replace legacy vault integrations with this environment's
scoped `vault run` client. Read exact-name and environment-variable rules in
`env/files/vault-client.py`, and write rules in [vault-writes.md](vault-writes.md).

For an existing backend, rotate only `bundle.json` and its `runtime.json`
`sha256` entry. Preserve every other input, approval and grant digest. Run the
real `isolated-config.py` validation against the complete proposed inputs
with that backend's grants, capturing private diagnostics. During the stopped
window below, take a private mode-0600 Secret backup, recheck parent and child
automation and pod absence, and use a resourceVersion-tested JSON patch.
Advance the tracked input revision for each changed backend. A validation-only
run must never patch resources, pause Applications or scale a workload.

## Policy input

`policies.json` has `environment`, `files` and nonempty `checks` fields.
Files use the bundle's base64 entry shape. Required targets are
`.cc-safety-net/policy.json` and `.claude/settings.json`. Additional hook files
may live under `.config/t3-policy/`. Secret-protection overrides cannot disable
checks. Claude hooks must remain enabled and cannot add or auto-enable project
MCP servers.

The homelab safety check includes its installed CLI path:

```json
{
  "argv": ["python3", "${HOME}/.config/t3-policy/check.py",
           ".local/share/pi/cc-safety-net/2.6.4/dist/bin/cc-safety-net.js"],
  "exit": 0
}
```

Checks must exercise the required policy with harmless allow/deny payloads.
Startup runs them with a credential-free environment and a 30-second limit;
child diagnostics are captured rather than printed. A failed check stops startup.
Legacy pi-permission-system inputs remain accepted but are not installed.
Their acceptance checks are still run and cannot be silently removed.
See [the shared safety layers](../one/pi-safety.md) for the rules the installed
extensions do and do not enforce.

## Homelab kubeconfig

The homelab kubeconfig must contain exactly one context, cluster and user,
with consistent references, an HTTPS API URL, an inline trusted CA and an
inline token or client certificate/key. Exec plugins, cloud auth providers,
external credential files and insecure TLS are rejected. `KUBECONFIG` selects
the read-only Secret projection. Actual resource permissions come from the
separately tracked homelab RBAC, not the file's mount mode.

## Manager kubeconfig

The manager uses only the tracked `t3code/manager-agent` ServiceAccount and its
separately synced manager access Roles. It does not create RBAC or change the
homelab or work grants. Its kubeconfig has one `manager` context, the
`manager-agent` user, an inline token and CA, and the in-cluster Kubernetes
HTTPS service address. It has no exec plugin, external credential path or
insecure TLS setting. The file format does not grant permissions; the tracked
manager RBAC does.

OWNER: after syncing the tracked manager access Application and before
restarting the manager, run the reviewed helper from the desktop, from the
repository root:

```sh
python3 2-k3s/13.t3code/one/tools/manager-kubeconfig.py --context epaflix
```

`epaflix` selects the owner's desktop admin context, not the manager's new
context. The helper requires an explicit context and never falls back to the
current context. It creates `manager-agent-kubeconfig` as a ServiceAccount token
Secret, waits at most 30 attempts with one-second intervals for its token and
CA, and creates Opaque Secret `manager-kubeconfig` with only the `kubeconfig`
key. All Secret values stay in memory and captured subprocess input/output.
Successful output is only `Manager kubeconfig prepared.`

An identical existing grant is left unchanged. A different grant, missing
account, missing token-controller data or Kubernetes failure stops provisioning
with a generic error. Check resource presence and any existing grant privately;
never print or paste Secret values into agent sessions or public evidence.
There is no automatic rotation or replacement. Leave the manager stopped if
provisioning fails. Resolve any mismatched grant through an owner-reviewed
recovery before retrying; do not restore a desktop admin kubeconfig to the
manager. This helper belongs to the conversion PR, not a separate live rollout.
The isolated-runtime input rotation procedure below remains unchanged.

## Manager conversion and owner cutover

The manager keeps its existing home, checkout, T3 state, inbound SSH and model
access. Startup requires `manager-guidance.md`, installs it through the private
bundle installer, and atomically replaces Codex's override with the same combined
instructions. Pi uses those instructions without the homelab SSH guide. Manager
rules override personal preferences. Known agent and project MCP maps and cached
auto-enable choices are cleared on every boot, including T3 worktrees. Invalid,
escaping or unsupported alternate configuration stops startup with no config
contents in diagnostics. Resolve those files privately before retrying.

`KUBECONFIG=/run/t3-manager/kubeconfig` selects the read-only `kubeconfig` key of
Opaque Secret `manager-kubeconfig` in both T3 and SSH shells. Automatic token
mounting stays off. The model grant is `cliproxy-secrets/omp-api-key`. There are
no manager hub registrations, GitHub credentials, vault login or outbound SSH
identity setup. Removing mounts does not remove old files from the persistent
home. The owner must establish credential absence before starting the manager.

The approved requests are `500m` CPU and `1536Mi` memory. The observed `0.44` CPU
and `0.9Gi` memory sample is not a peak measurement. Keep existing limits and the
`40Gi` PVC, then observe normal use after cutover. PVC shrinking is not in place.

### Owner preparation

Before merge, privately edit `one/cliproxy-secrets.enc.yaml` with SOPS. Remove
`github-token` and `aks-tenant-id`, keeping only `omp-api-key`. Never display
plaintext. After a model-key rotation, the owner restarts the manager from a
desktop by running `kubectl --context epaflix -n t3code delete pod t3code-0`.
The encrypted-key inventory test remains failing until this owner step is done.
The conversion cannot be accepted or cut over while it fails.

Record PR and commit references, the pre-cutover revision, replica counts,
automation policies and idle-rollout suspension state privately outside this
conversation. Perform every following step from an independently authenticated
desktop. The manager cannot push, merge, sync or recover its own namespace.

1. M1: Confirm #1946 is accepted, manager access is merged and synced, and the
   conversion PR passes independent review and CI. Confirm desktop Git merge,
   Argo sync and `kubectl --context epaflix auth can-i patch statefulsets.apps
   -n t3code` access. Expect `yes`. Arrange maintenance and end manager sessions.
   Stop if any independent recovery access is missing.
2. M2: Verify the full allow/deny matrix from `one/tools/manager-rbac.test.py`
   through desktop impersonation with ServiceAccount
   `system:serviceaccount:t3code:manager-agent` and groups `system:authenticated`,
   `system:serviceaccounts` and `system:serviceaccounts:t3code`. Check named
   Application get, denied Application list/watch/patch/other-name get, denied
   work exec/logs/port-forward/Secrets, own namespace and cluster resources.
   These are authorization requests, not real work-output requests.
   Use `--subresource`, never the `pods/exec` form or other slash forms here.
   `auth can-i` parses `pods/exec` as a pod named `exec`, not the exec subresource.
   Run these exact subresource checks for both runtime and hub namespaces:

   ```sh
   for namespace in t3code-homelab mcp-hub-homelab t3code-work mcp-hub-work; do
     printf '%s\n' "$namespace"
     kubectl --context epaflix --as=system:serviceaccount:t3code:manager-agent --as-group=system:authenticated --as-group=system:serviceaccounts --as-group=system:serviceaccounts:t3code auth can-i create pods --subresource=exec -n "$namespace"
     kubectl --context epaflix --as=system:serviceaccount:t3code:manager-agent --as-group=system:authenticated --as-group=system:serviceaccounts --as-group=system:serviceaccounts:t3code auth can-i get pods --subresource=log -n "$namespace"
     kubectl --context epaflix --as=system:serviceaccount:t3code:manager-agent --as-group=system:authenticated --as-group=system:serviceaccounts --as-group=system:serviceaccounts:t3code auth can-i create pods --subresource=portforward -n "$namespace"
     kubectl --context epaflix --as=system:serviceaccount:t3code:manager-agent --as-group=system:authenticated --as-group=system:serviceaccounts --as-group=system:serviceaccounts:t3code auth can-i patch statefulsets.apps --subresource=scale -n "$namespace"
     kubectl --context epaflix --as=system:serviceaccount:t3code:manager-agent --as-group=system:authenticated --as-group=system:serviceaccounts --as-group=system:serviceaccounts:t3code auth can-i patch deployments.apps --subresource=scale -n "$namespace"
   done
   ```

   Expect exec/log `yes` in both homelab namespaces and `no` in both work
   namespaces. Expect portforward and both scale checks `no` everywhere.
   A denied check exits nonzero; do not run this loop with `set -e`.
3. M3: Run the reviewed kubeconfig helper with `--context epaflix`. Privately
   test the real grant from a mode-0600 file outside the repository, without
   impersonation. Expect context `manager` and the same full matrix results.
   If needed, select the same cluster's desktop-reachable API with `--server`,
   retaining the manager user and trusted CA. Never copy desktop user credentials
   into the test config. With `manager_config` pointing to that private file,
   repeat M2's subresource checks without impersonation:

   ```sh
   for namespace in t3code-homelab mcp-hub-homelab t3code-work mcp-hub-work; do
     printf '%s\n' "$namespace"
     kubectl --kubeconfig "$manager_config" --context manager auth can-i create pods --subresource=exec -n "$namespace"
     kubectl --kubeconfig "$manager_config" --context manager auth can-i get pods --subresource=log -n "$namespace"
     kubectl --kubeconfig "$manager_config" --context manager auth can-i create pods --subresource=portforward -n "$namespace"
     kubectl --kubeconfig "$manager_config" --context manager auth can-i patch statefulsets.apps --subresource=scale -n "$namespace"
     kubectl --kubeconfig "$manager_config" --context manager auth can-i patch deployments.apps --subresource=scale -n "$namespace"
   done
   ```

   Expect the same homelab exec/log `yes`, work exec/log `no`, and portforward
   and scale `no` everywhere. Do not use `pods/exec` or other slash forms.
   Remove the private file afterwards. Stop on a mismatch.
4. M4: Pause `app-of-apps` automation first, then `t3code`, following the
   [parent-first pause procedure](#input-rotation). Suspend `t3code-idle-rollout`
   in `t3code`. Verify both policies stay disabled, no sync is running and no
   rollout Job is active. Wait for any existing Job to finish. Leave the four
   managed Applications unchanged.
5. M5: Merge the reviewed conversion from the desktop and record
   `MERGED_COMMIT_SHA`. Confirm runtime reconciliation remains paused.
6. M6: Privately inventory and remove home admin kubeconfigs and duplicates,
   cloud caches, outbound Git/SSH keys, GitHub CLI/login and credential-helper
   stores, stale `~/.gitconfig` `credential.helper` and `user` entries,
   `~/.pi/agent/mcp-auth.json` MCP OAuth tokens, and shell/profile token exports
   in both the PVC and passwd homes.
   Do not display their contents. Keep inbound public login keys, server host
   keys, the non-credential instruction bundle and model setup. The explicit
   known-file removal, performed only by the owner, is:

   ```sh
   kubectl --context epaflix -n t3code exec t3code-0 -c t3 -- sh -c 'rm -f "$HOME/.kube/config"'
   kubectl --context epaflix -n t3code scale statefulset t3code --replicas=0
   kubectl --context epaflix -n t3code wait --for=delete pod/t3code-0 --timeout=300s
   ```

   Stop if private inventory cannot establish credential absence. Selecting a
   different `KUBECONFIG` does not make a surviving admin file safe.
7. M7: Wait for merged CI and required image publication, then run:

   ```sh
   argocd app sync app-of-apps --revision "$MERGED_COMMIT_SHA"
   argocd app wait t3code --sync --timeout 1200
   kubectl --context epaflix -n t3code scale statefulset t3code --replicas=1
   kubectl --context epaflix -n t3code wait --for=condition=Ready pod/t3code-0 --timeout=1200s
   ```

   Parent sync restores the tracked parent/child policies; child auto-sync
   applies the conversion and prunes obsolete Secrets. Explicit scale-up is
   required because the Application ignores `spec.replicas`. Verify the reviewed
   controller revision. Confirm the legacy hub removes only the manager client
   and retains its other clients, without displaying inventory values.
8. M8: Privately confirm the mounted context, repeat the full real-credential
   authorization matrix, and check only names, modes and env names for grant
   absence. Verify a harmless model prompt, empty external MCP inventory,
   manager instructions, inbound SSH/T3 reconnect and the separate idle-rollout
   account. Check the named Application's automation before any recovery and
   again immediately before deletion. Missing, disabled or unreadable automation
   requires an owner decision. Do not pause work Applications or delete real
   work pods for a smoke test. Instructions are not technical enforcement.
9. M9: Restore the recorded idle-rollout state. Confirm parent and `t3code`
   self-heal, then observe `kubectl --context epaflix -n t3code top pod t3code-0
   --containers` during normal use. Keep sanitized acceptance evidence only.

### Owner rollback

Use desktop context `epaflix`, never credentials found through homelab exec.
Pause the parent and manager Applications, suspend idle rollout and stop the
manager before repairing inputs. Keep the home admin credential removed.

For bad access rules, use a reviewed access PR and manually sync
`t3code-manager-access`; include `--prune` when removing a Role. For a bad
generated kubeconfig grant, privately repair the mismatch and rerun the helper,
then repeat real-credential authorization checks. Do not add a temporary broad
Role or ClusterRoleBinding.

For a runtime regression, use a reviewed revert or forward fix that preserves
credential removal. Do not restore the mixed manifest or its old mounts. Merge
from the desktop and use the controlled sync/start sequence above. Restore
parent/child automation and idle state only after acceptance. Desktop recovery
must work even when homelab is unavailable.

## Provider preferences and Codex requirements

Startup retains Pi and Codex display names and Pi's enabled preference.
Custom model IDs use `cliproxy/` for Pi or `codex/` for Codex. Only Pi selections
are retained; title-generation selections use `cliproxy-textgen/`. Startup resets
driver identities, executable paths and launch arguments. Extra provider
instances, legacy provider maps and unsupported selections are removed.
Endpoint and credential configuration comes from private inputs, not application
preferences.

Pi caps `cliproxy/claude/claude-haiku-5-5` at 100K tokens to avoid the
[price step above 100,000 prompt tokens](https://platform.claude.com/docs/en/about-claude/pricing#long-context-pricing).
With the default 16,384-token reserve, Pi auto-compacts near 84K tokens.

The Codex and Claude direct instances stay disabled in isolated runtimes.
The work pod's Codex model API and T3 catalog expose bare IDs. Minimal
non-streaming requests to its configured Responses endpoint returned HTTP 400
for `gpt-6.1-sol` and HTTP 200 for `codex/gpt-6.1-sol`. Adding custom model IDs
does not replace discovered bare IDs, and T3's Codex settings have no route-prefix
mapping. Startup therefore disables that instance and resets its selections to
Pi. Use T3 `delegate_task` with instance `pi` and `cliproxy/<route>/<model>`.
Mixed-runtime provider settings are unchanged.

Codex retains unmanaged TOML values and trust entries confined to this
environment's project and subdirectories. Startup replaces its provider and
MCP maps, removes alternate profiles, and atomically writes the result.
`/etc/codex/requirements.toml` enforces the approved provider definition and
exact allowed hub server URLs, including in trusted project directories and
T3 worktrees. Preferences cannot bypass those managed requirements.

## Input rotation

The homelab and work Applications automatically sync merged revisions with
pruning, but without self-healing. Their StatefulSets use `OnDelete`; the
namespace's `t3code-idle-rollout` CronJob replaces a pending runtime pod after
30 idle minutes. Automatic sync can still restore a scaled-down StatefulSet
on a new Git revision, so pause it for the entire rotation.

`app-of-apps` renders `2-k3s/11.argocd/apps`, including itself and both runtime
Applications, with self-healing enabled. It would undo a child-only pause.
The `argocd` Application renders `2-k3s/11.argocd`, whose kustomization does
not include `apps/`, so pause `app-of-apps` first. This temporarily stops
reconciliation of all Application definitions, not the other children's own
automatic sync. Both hubs continue their own automated sync and self-healing.
Coordinate this window with other operators.

1. A1: Check active sessions and arrange maintenance. Select `t3code-homelab`
   or `t3code-work`, then disable the parent's and selected child's automation:

   ```sh
   app=t3code-homelab
   namespace=$app
   statefulset=${app#t3code-}
   kubectl --context epaflix -n argocd patch application app-of-apps --type json \
     -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
   kubectl --context epaflix -n argocd patch application "$app" --type json \
     -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
   ```

   Confirm both policies remain disabled and any in-flight sync has completed
   before scaling. Do not manually sync either Application during rotation.
2. A2: Scale down and wait for the pod to disappear before changing Secrets:

   ```sh
   kubectl --context epaflix -n "$namespace" scale statefulset "$statefulset" --replicas=0
   kubectl --context epaflix -n "$namespace" wait --for=delete "pod/$statefulset-0" --timeout=300s
   ```

3. A3: Update inputs, grants and their digests together. Advance the tracked
   input revision, review and merge, keeping the tracked replica count at one.
   Keep both Applications paused until all private inputs are consistent and
   the merged runtime image is published. Otherwise an old grant environment
   can run against newly projected input files.
4. A4: Sync `app-of-apps` to the merged revision. Its manifests restore its own
   self-healing policy and the child's automated pruning policy. The child's
   automatic sync applies the merged runtime revision and restores one replica:

   ```sh
   revision=MERGED_COMMIT_SHA
   argocd app sync app-of-apps --revision "$revision"
   argocd app wait "$app" --sync --health --timeout 1200
   kubectl --context epaflix -n "$namespace" wait --for=condition=Ready "pod/$statefulset-0" --timeout=300s
   ```

   Verify both automation policies are restored, runtime self-healing remains
   off, and the expected input revision is running. If rotation fails, leave
   both Applications paused and the pod stopped until inputs are consistent.
