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

## Provider preferences and Codex requirements

Startup retains Pi and Codex display names and Pi's enabled preference.
Custom model IDs use `cliproxy/` for Pi or `codex/` for Codex. Only Pi selections
are retained; title-generation selections use `cliproxy-textgen/`. Startup resets
driver identities, executable paths and launch arguments. Extra provider
instances, legacy provider maps and unsupported selections are removed.
Endpoint and credential configuration comes from private inputs, not application
preferences.

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
