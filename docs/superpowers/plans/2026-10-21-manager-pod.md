# Manager pod Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve #1947 by converting `t3code-0` into a permanently running manager with model access, no external MCP services or direct administrative credentials, and exactly D7 recovery rights.

**Architecture:** Keep the existing `t3code` StatefulSet, home PVC, inbound SSH and `OnDelete` updates. A separate, manually synced access Application owns the manager's cross-namespace Roles; an owner-provisioned ServiceAccount kubeconfig replaces the home admin credential. Stage access first, then stop and convert the manager from an independent desktop, because this conversation loses its own administrative and Git credentials at cutover.

**Tech Stack:** Kubernetes RBAC, Argo CD, Kustomize, SOPS/KSOPS, Bash, Python stdlib, existing PyYAML/unittest tests.

**Spec:** `docs/superpowers/specs/2026-10-19-t3-pod-split-design.md`, especially D1, D3, D7, D9, D10, D11 and X1-X6. Issue: `gh issue view 1947`. Baseline: `origin/main` at `9f8c2bfc`; planning branch: `docs/1947-manager-plan`.

## Global Constraints

- D1: "It is not a third daily-use environment."
- D1: "code or manifest fixes go to the homelab pod by pull request."
- D3: "Remove all hub registrations and hub credentials from the manager."
- D7: "Grant no list or watch." This applies to Argo Applications, whose only grant is named `get`.
- D9: "Keep CLIProxy model access so the manager's agent can run."
- D10: "The manager does not cover its own namespace `t3code`."
- D11: "The manager does not act on a pod whose Argo CD Application has automated sync turned off."
- X2: "A legacy token or refresh token enters a new namespace." This is a failure, not a migration technique.
- X3: "Homelab gains rights over `t3code-work`." This is a failure; leave homelab's existing operating boundary unchanged.
- X4: "A public file names an employer or account identity." Use generic homelab/work names; publish no endpoints, account IDs, secret values or private evidence.
- X5: "The accepted D7 homelab exec path is the only exception." Remove every direct admin/work credential from the manager's home and mounts.
- X6: "Using credentials found through the D7 homelab exec path." This is forbidden even though homelab exec is an accepted indirect-admin risk.
- N1: "Import no thread history into any pod." Preserve existing state without copying it between environments.
- No new dependencies, no planned code comments, no work exec/log reads, no Secret-value inspection by the implementing agent. Owner-only provisioning may handle credentials privately without displaying them.
- #1946 must be accepted before the conversion. #1848 removes the legacy hub later; this issue removes only the manager's use and registration.

## Review Focus

- RF1: Namespace transformation or a group/cluster binding broadens a correct-looking Role. Rendered effective-permission tests must deny work output, other namespaces and cluster resources. Task 1.
- RF2: A persisted custom, nested-project or alternate-agent MCP entry survives removal of hub env. Migration tests must clear it or stop startup without exposing its contents. Task 3.
- RF3: Missing token-controller data, a mismatched existing kubeconfig or a provisioning error leaves the manager unusable. Fake-cluster tests must fail quietly and never overwrite a different grant. Task 2.
- RF4: Old instruction symlinks, bundle reinstall or missing guidance restore homelab operating instructions. Repeated-boot tests must install manager instructions once and fail closed if guidance is missing. Task 3.
- RF5: Removing a Secret generator leaves another container, shell or encrypted key carrying direct credentials. Rendered pod, encrypted-key-inventory and startup tests must prove their absence while preserving model access and inbound SSH. Task 3.

---

## File map and ownership

Paths below are the implementation changes, not changes to make while writing this plan.

| Files | Responsibility and Argo owner |
| --- | --- |
| Create `2-k3s/13.t3code/manager-access/{kustomization,serviceaccount,recovery,applications}.yaml` | Explicit namespaces, no Kustomize namespace transformer. New `t3code-manager-access` Application owns one ServiceAccount and five Role/RoleBinding pairs. |
| Create `2-k3s/11.argocd/apps/app-t3code-manager-access.yaml`; modify `2-k3s/11.argocd/apps/kustomization.yaml` | `app-of-apps` owns the manual access Application definition. Never place manager RBAC in a work-owned overlay. |
| Create `2-k3s/13.t3code/one/tools/manager-rbac.test.py`, `manager-kubeconfig.py`, `manager-kubeconfig.test.py`, `manager-runtime.test.py` | Offline RBAC evaluation, owner-only grant provisioning, synthetic provisioning/runtime tests. |
| Modify `2-k3s/13.t3code/one/{statefulset,kustomization,ksops-generator}.yaml`; delete `one/{github-ssh,mcp-hub-client,vaultwarden}.enc.yaml` | Existing `t3code` Application owns the manager runtime and prunes obsolete Secrets. Keep its automated self-heal and existing `ignoreDifferences`. |
| OWNER: edit `2-k3s/13.t3code/one/cliproxy-secrets.enc.yaml` | Keep only the model key using SOPS. The agent never decrypts it. |
| Create `2-k3s/13.t3code/one/files/manager-setup.py`, `manager-guidance.md`; modify `one/files/{entrypoint.sh,ssh-config.sh,sshd.sh}` | Manager-only startup migration, instructions and removal of outbound credential setup. Shared `pi-setup.py` and its tests stay unchanged. |
| Modify `2-k3s/13.t3code/env/tools/{private-config.test.py,rollout-hash.test.sh,isolated-runtime.test.py,work-runtime.test.py,smoke.sh}` and `.github/workflows/ci.yml` | SOPS-free fixtures, manager rollover assertions, preserved homelab/work SSH guidance, startup smoke staging and CI. |
| Modify `2-k3s/23.mcp-hub/files/clients.json` | Existing `mcp-hub` Application removes only the `t3code` client digest. Retain every other client and backend. |
| Modify `2-k3s/13.t3code/env/isolated-runtime.md` | Add a manager section with the owner cutover/rollback below; retain isolated-runtime rotation instructions unchanged. |

`one/idle-rollout.yaml` remains unchanged: its separate `t3code-idle-rollout` ServiceAccount has its own-namespace rollout rights. Those rights must never bind `manager-agent`. Keep the existing `app-t3code.yaml` ownership and self-heal. Shared hub/vault/SSH helper sources can remain tracked for existing consumers and tests, but remove them from the manager's generated scripts ConfigMap where unused. Do not change shared isolated-runtime registration behavior or delete the legacy hub.

## Task 1: Add the independently reviewable manager access policy

**Files:**
- Create: `2-k3s/13.t3code/manager-access/kustomization.yaml`, `2-k3s/13.t3code/manager-access/serviceaccount.yaml`, `2-k3s/13.t3code/manager-access/recovery.yaml`, `2-k3s/13.t3code/manager-access/applications.yaml`, `2-k3s/11.argocd/apps/app-t3code-manager-access.yaml`
- Modify: `2-k3s/11.argocd/apps/kustomization.yaml`, `.github/workflows/ci.yml`
- Test: `2-k3s/13.t3code/one/tools/manager-rbac.test.py` (create)

**Interfaces:** Produces `system:serviceaccount:t3code:manager-agent`. `serviceaccount.yaml` defines `manager-agent` in `t3code` with `automountServiceAccountToken: false`. No credentials are committed. The runtime consumes this identity in Task 3.

- [ ] **Step 1: Write failing rendered-policy tests.** Follow `13.t3code-homelab/tools/rbac.test.py`: call `kustomize build`, load YAML, assert exact objects and subject bindings. Add a test-local `allowed(docs: list[dict], verb: str, group: str, resource: str, namespace: str | None, name: str | None = None) -> bool` that resolves Roles through bindings for this identity and its standard ServiceAccount groups. Follow the table-driven offline-policy style in `env/tools/network-policy.test.py`; do not create a cluster or apply fixtures. Use verbs `get/list/watch/create/update/patch/delete/deletecollection` across each resource/namespace cell; every cell outside the exact grants must be false, for example `assert not allowed(docs, 'create', '', 'pods/exec', 't3code-work')`.

  | Test name | Required assertions |
  | --- | --- |
  | `test_exact_recovery_matrix` | For all four target namespaces, `allowed(..., verb, '', 'pods', ns)` is true for `get/list/watch/delete`; core group `""` `events` and `events.k8s.io` `events` each allow only `get/list/watch`; group `apps` resources `statefulsets/deployments` allow only `get/list/watch`. Assert exact rule tuples, pinning both events API groups and absence of wildcard groups/resources/verbs. |
  | `test_only_homelab_can_exec_and_read_logs` | Only `t3code-homelab` and `mcp-hub-homelab` allow `create pods/exec` and `get pods/log`. In both work namespaces, every tested verb on `pods/exec`, `pods/log`, `pods/portforward` and `secrets` is false. `get pods/exec` is false everywhere; port-forward and Secrets are false in homelab too. |
  | `test_named_application_get_only` | `get applications.argoproj.io` is true only in `argocd`, with each of the four exact names. Unnamed/other-name get and every list/watch/create/update/patch/delete are false; no Application sync grant. |
  | `test_no_self_other_namespace_or_cluster_resource_grants` | Deny manager operations in `t3code`, `default`, `kube-system`, legacy `mcp-hub` and an arbitrary namespace; deny cluster nodes, namespaces, persistentvolumes, clusterroles and clusterrolebindings. Assert no ClusterRole/ClusterRoleBinding in access output, no RoleBinding to a ClusterRole, and exactly five Role/RoleBinding pairs. Render a temporary copy of `one/` using the existing `env/tools/private-config.test.py::overlay` fixture to prove the idle account's grants do not match the manager or its groups. |
  | `test_access_app_is_manual_and_namespaces_survive_rendering` | Registry includes the new app; source path is `2-k3s/13.t3code/manager-access`, revision `main`, no `automated` policy. Destination namespace is `t3code`, but rendered RBAC retains all five explicit namespaces. No manager binding targets `homelab-agent` or any work pod account. |

  ```python
  assert allowed(docs, 'get', '', 'events', 't3code-work')
  assert allowed(docs, 'watch', 'events.k8s.io', 'events', 't3code-work')
  assert not allowed(docs, 'delete', 'events.k8s.io', 'events', 't3code-work')
  assert not allowed(docs, 'create', '', 'pods/exec', 't3code-work')
  ```

- [ ] **Step 2: Run the new test before implementation.** `python3 2-k3s/13.t3code/one/tools/manager-rbac.test.py -v`. Expected: failure because the access overlay does not exist.
- [ ] **Step 3: Implement the exact manifests.** `recovery.yaml` owns four named `manager-recovery` Roles and bindings, with the namespace-specific matrix above. `applications.yaml` owns `manager-applications` Role/binding in `argocd`, with `resourceNames: [t3code-homelab, t3code-work, mcp-hub-homelab, mcp-hub-work]`, `apiGroups: [argoproj.io]`, `resources: [applications]`, `verbs: [get]`. Every binding has the single `manager-agent` subject in `t3code`. Pattern the manual Application after `app-t3code-homelab-access.yaml`; do not edit homelab access.
- [ ] **Step 4: Run verification.** The test above and `kustomize build 2-k3s/13.t3code/manager-access >/dev/null` must pass. `kustomize build 2-k3s/11.argocd/apps >/dev/null` must exit 0. Keep the synthetic `one/` identity test passing again after Task 3 updates that fixture.
- [ ] **Step 5: Submit access PR first.** Check the implementation branch is rebased onto current `origin/main` before committing. Commit only this task with `feat: add namespaced manager recovery access`, without a co-author. The still-credentialed agent in `t3code-0` pushes and opens this PR; normal independent review applies. OWNER: merge it. OWNER: sync `t3code-manager-access` from a desktop, then verify the identity before any runtime cutover. The manager must not sync its own access app.

  ```bash
  git add 2-k3s/13.t3code/manager-access/kustomization.yaml 2-k3s/13.t3code/manager-access/serviceaccount.yaml 2-k3s/13.t3code/manager-access/recovery.yaml 2-k3s/13.t3code/manager-access/applications.yaml 2-k3s/11.argocd/apps/app-t3code-manager-access.yaml 2-k3s/11.argocd/apps/kustomization.yaml 2-k3s/13.t3code/one/tools/manager-rbac.test.py .github/workflows/ci.yml
  git commit -m "feat: add namespaced manager recovery access"
  ```

## Task 2: Provision a read-only ServiceAccount kubeconfig without printing it

**Files:**
- Create: `2-k3s/13.t3code/one/tools/manager-kubeconfig.py`
- Modify: `.github/workflows/ci.yml`, `2-k3s/13.t3code/env/isolated-runtime.md`
- Test: `2-k3s/13.t3code/one/tools/manager-kubeconfig.test.py` (create)

**Interfaces:** Define `kubeconfig(token: bytes, ca: bytes) -> bytes` and `provision(context: str, run=subprocess.run) -> None`. CLI: `python3 .../manager-kubeconfig.py --context epaflix`. `epaflix` is an explicit owner desktop admin context, not the manager's new context. No implicit current-context fallback.

- [ ] **Step 1: Write failing fake-kubectl tests.** `test_kubeconfig_has_one_manager_context` asserts one context/cluster/user, `current-context == 'manager'`, user `manager-agent`, inline token and CA, and the standard in-cluster Kubernetes HTTPS service address, with no exec/auth-provider/external paths/insecure TLS. `test_provision_uses_tracked_account_and_explicit_context` asserts every command has the supplied `--context`, the namespace is only `t3code`, and no RBAC is created. `test_existing_identical_grant_is_idempotent` asserts no replacement. RF3: `test_missing_account_unpopulated_token_or_mismatched_grant_fails_quietly` asserts bounded failure, nonzero exit, no overwrite, no synthetic token, CA or captured kubectl output on stdout/stderr.

  ```python
  assert config['current-context'] == 'manager'
  assert len(config['contexts']) == len(config['clusters']) == len(config['users']) == 1
  assert config['users'][0]['name'] == 'manager-agent'
  assert 'exec' not in config['users'][0]['user']
  ```
- [ ] **Step 2: Run** `python3 2-k3s/13.t3code/one/tools/manager-kubeconfig.test.py -v`. Expected: fail on missing module.
- [ ] **Step 3: Implement the homelab provisioning pattern without its unrelated grants.** Follow `13.t3code-homelab/tools/inputs.py::agent_kubeconfig`: require the tracked ServiceAccount, create `manager-agent-kubeconfig` as a `kubernetes.io/service-account-token` Secret annotated for `manager-agent`, and wait at most 30 one-second attempts for token/CA. Build the kubeconfig in memory, then create `manager-kubeconfig`, type Opaque, with only key `kubeconfig`. Capture all subprocess output. An existing different grant is an error requiring owner intervention, not an automatic rotation. Successful stdout is only `Manager kubeconfig prepared.`; failures are generic. No temporary plaintext files, shell tracing, output tokens, SOPS decryption or model-management access.
- [ ] **Step 4: Run** the test command again. Expected: all tests pass. This test uses only fake credentials; do not run provisioning as an agent.
- [ ] **Step 5: Commit** with `feat: add owner-only manager kubeconfig provisioning`, after the branch/base check. Include this in the conversion PR, not a separate live rollout. OWNER: run the reviewed helper from the desktop after access sync and before restarting the manager. The owner may handle ServiceAccount Secret values privately; the agent may inspect only test fixtures and Secret metadata.

  ```bash
  git add 2-k3s/13.t3code/one/tools/manager-kubeconfig.py 2-k3s/13.t3code/one/tools/manager-kubeconfig.test.py .github/workflows/ci.yml 2-k3s/13.t3code/env/isolated-runtime.md
  git commit -m "feat: add owner-only manager kubeconfig provisioning"
  ```

## Task 3: Remove direct credentials and external MCP configuration

**Files:**
- Create: `2-k3s/13.t3code/one/files/manager-setup.py`, `2-k3s/13.t3code/one/files/manager-guidance.md`
- Modify: `2-k3s/13.t3code/one/statefulset.yaml`, `2-k3s/13.t3code/one/kustomization.yaml`, `2-k3s/13.t3code/one/ksops-generator.yaml`, `2-k3s/13.t3code/one/cliproxy-secrets.enc.yaml` (OWNER), `2-k3s/13.t3code/one/files/entrypoint.sh`, `2-k3s/13.t3code/one/files/ssh-config.sh`, `2-k3s/13.t3code/one/files/sshd.sh`, `2-k3s/13.t3code/env/tools/private-config.test.py`, `2-k3s/13.t3code/env/tools/smoke.sh`, `.github/workflows/ci.yml`, `2-k3s/23.mcp-hub/files/clients.json`, `2-k3s/13.t3code/env/isolated-runtime.md`
- Delete: `2-k3s/13.t3code/one/github-ssh.enc.yaml`, `2-k3s/13.t3code/one/mcp-hub-client.enc.yaml`, `2-k3s/13.t3code/one/vaultwarden.enc.yaml`
- Test: `2-k3s/13.t3code/one/tools/manager-runtime.test.py` (create), `2-k3s/13.t3code/env/tools/rollout-hash.test.sh`, `2-k3s/13.t3code/env/tools/isolated-runtime.test.py`, `2-k3s/13.t3code/env/tools/work-runtime.test.py` (modify)

**Interfaces:** `manager-setup.py::configure(home: Path, project: Path, guidance: Path, bundle: Path = Path('/private-agent-config/bundle.json')) -> None`, invoked as `python3 /scripts/manager-setup.py "$HOME" "$PROJECT_DIR" /scripts/manager-guidance.md` before any agent starts. It replaces the separate private-bundle install command, calling existing `private-config.py::install(payload, home, instructions=guidance_bytes)` so Claude/Codex keep their managed symlinks and migration backups are not recreated. Keep shared `pi-setup.py::write(home, safety_entry, ssh_path=Path('/scripts/homelab-ssh.md'))` and its CLI unchanged. Only the manager entrypoint replaces its CLI write call with a Python API call supplying `ssh_path=Path('/dev/null')`, since the installed canonical instructions already contain manager guidance. Homelab/work keep the default SSH-guide append. Manager setup atomically copies that same combined content to Codex's override.

- [ ] **Step 1: Add failing tests** to `manager-runtime.test.py`, `env/tools/isolated-runtime.test.py`, `env/tools/work-runtime.test.py` and `env/tools/rollout-hash.test.sh`. Use copied overlays in `TemporaryDirectory`, strip KSOPS generators, and inject synthetic Secrets following `env/tools/private-config.test.py::overlay`. Never decrypt real files.

  | Test name | Required assertions |
  | --- | --- |
  | `test_rendered_manager_has_only_model_and_recovery_grants` | StatefulSet uses `manager-agent`, automount is false, and `KUBECONFIG` selects `/run/t3-manager/kubeconfig`. Secret `manager-kubeconfig` projects only `kubeconfig`, mode `0440`, read-only into both `t3` and `sshd`. No token Secret is mounted. No removed credential env, `envFrom`, volumes or mounts in any container/init container. |
  | `test_model_key_inventory_and_manager_client_removal` | `assert set(secret.get('stringData', secret.get('data', {}))) == {'omp-api-key'}` for encrypted `cliproxy-secrets`; no decryption. KSOPS excludes the three deleted files; no hub token-revision replacement. `ANTHROPIC_BASE_URL` and model `ANTHROPIC_AUTH_TOKEN` reference remain. `clients.json` lacks only `t3code`; preserve the fixture's other clients. |
  | `test_stale_agent_and_project_mcp_maps_are_empty_after_two_boots` | Seed Pi, both Claude config locations, nested Claude `projects`, Codex `mcp_servers`, OpenCode, checkout and T3-worktree `.mcp.json` and Claude settings with managed and custom entries. After two runs every external MCP map is empty; cached auto-enable choices are cleared. Unrelated hooks, preferences, provider aliases, Git checkout and state database fixture bytes are unchanged. |
  | `test_invalid_or_escaping_agent_config_stops_without_disclosure` | Malformed JSON/TOML, an unsupported alternate profile containing MCP, or a config symlink outside the home causes startup failure before T3 launch, without printing contents or following an escaping link. Reuse atomic replacement patterns; valid files remain parseable. |
  | `test_manager_guidance_replaces_old_symlinks_and_is_required` | Across three private-bundle installs and setup runs, Claude, Codex override and Pi AGENTS files contain the manager block exactly once, not the appended homelab SSH guide. Missing guidance fails before launch, never retaining permissive instructions as fallback. Assert the D11 disabled/missing/unreadable Application rule and X6/work-output prohibitions are present. |
  | `test_startup_and_ssh_do_not_restore_credentials` | Stub startup commands and capture calls: no hub registration, credential helper installation, vault agent or outbound SSH identity setup. SSH shell gets the read-only manager `KUBECONFIG`, not `SSH_AUTH_SOCK`; inbound authorized keys, host-key generation, T3 tunnel and model wrapper behavior remain. Assert startup still invokes `ssh-policy.py` and its resulting policy denies `~/.ssh` in both homes. |
  | `test_manager_requests_and_self_recovery_identity_stay_separate` | Requests are `500m` CPU and `1536Mi` memory; memory limits, one replica, PVC and `OnDelete` remain unchanged. The idle rollout's subject is not the manager; `t3code` Application self-heal remains enabled. |
  | `test_isolated_instructions_keep_their_ssh_guides` | In both isolated runtime suites, retain the existing repeated-install instruction test and explicitly assert Pi instructions include the supplied SSH guide; also stage the entrypoint's Codex override append and assert its environment-specific guide is included. Do not replace either environment's guidance with manager rules. |
  | `rollout-hash.test.sh` | Drop `t3code-github-ssh`/`t3code-vaultwarden` from expected volumes and include the read-only `manager-kubeconfig`. Replace the hub-token rotation fixture/check with a model Secret `t3code.epaflix.com/model-revision` annotation change; assert the pod template's model revision changes. Retain private-bundle and scripts rollout/PVC retention checks. |

  ```python
  assert pod['serviceAccountName'] == 'manager-agent'
  assert not pod['automountServiceAccountToken']
  assert set(secret.get('stringData', secret.get('data', {}))) == {'omp-api-key'}
  assert ssh.read_text() in (home / '.pi/agent/AGENTS.md').read_text()
  assert ssh_override.read_text() in (home / '.codex/AGENTS.override.md').read_text()
  assert any('ssh-policy.py' in call for call in startup_calls) and all(str(h / '.ssh') in policy['secret_protection']['deny_paths'] for h in (home, ssh_home))
  ```

- [ ] **Step 2: Run** `python3 2-k3s/13.t3code/one/tools/manager-runtime.test.py -v` and `bash 2-k3s/13.t3code/env/tools/rollout-hash.test.sh`. Expected: new manager assertions fail on current runtime configuration. Run both isolated runtime suites too; their SSH-guide assertions must pass before and after the manager changes.
- [ ] **Step 3: Implement credential and grant removal.** Remove `MCP_HUB_URL`, `MCP_HUB_TOKEN`, `GITHUB_TOKEN`, `GH_TOKEN`, `AKS_TENANT_ID`; remove `github-ssh`, `vaultwarden`, `bw-scratch` volumes/mounts from both containers. Remove the obsolete inline `t3code` ServiceAccount when switching to the access-owned identity. Keep `ANTHROPIC_*` and only `cliproxy-secrets/omp-api-key`. Add the kubeconfig projection and env to `t3`; give SSH shells the same selection through `sshd.sh::SetEnv`. Keep inbound SSH public login keys and server host keys, which are not outbound authentication grants. Remove SSH agent env from sshd. Make manager `ssh-config.sh` stop installing GitHub/homelab outbound configuration. Remove these verified unused entries from the manager scripts ConfigMap: `git-credential-github.sh`, `vault-ssh-agent.py`, `vault-run.py`, `hub_clients.py`, `hub_services.py`, `homelab-ssh.conf` and `homelab-ssh.md`. Remove outbound vault/SSH setup, but retain the `ssh-policy.py` entrypoint invocation and keep the shared `ssh-policy.py` unchanged, including its `/run/t3-*` deny entries. Retain their shared source copies for other consumers/tests. Keep `entrypoint.sh`, `pi.sh`, `pi-setup.py`, `t3-pi-settings.py`, `private-config.py`, `cc-safety-net-install.py`, `cc-safety-net.patch`, `cc-safety-net-hashes.json`, `ssh-policy.py`, `sshd-authorized-keys`, `sshd.sh` and `ssh-config.sh` for startup, model settings, instructions, safety and inbound SSH; add `manager-setup.py` and `manager-guidance.md`. Replace the hub Secret revision replacement with `cliproxy-secrets` metadata annotation `t3code.epaflix.com/model-revision`, copied to the same pod-template annotation. Keep the idle rollout identity distinct and unchanged.
- [ ] **Step 4: OWNER: edit encrypted model credentials.** This Secret currently contains `github-token` and `aks-tenant-id`, consumed as `GITHUB_TOKEN` and `AKS_TENANT_ID`, in addition to `omp-api-key`. Run `sops 2-k3s/13.t3code/one/cliproxy-secrets.enc.yaml` privately, delete those two plaintext keys, keep the existing model key, and save re-encrypted ciphertext. Set its unencrypted metadata annotation `t3code.epaflix.com/model-revision` to the SHA-256 of the model key ciphertext field, updating it on each model rotation without displaying plaintext. The agent reviews only key names and ciphertext structure. Delete the three obsolete encrypted Secret files and generator references without decrypting them. Argo pruning handles deployed obsolete Secrets at sync. Remove only the manager client digest from the legacy hub's `files/clients.json`; do not revoke upstream credentials here or touch desktop clients/new hubs. Source revocation is the separate Q4 owner decision after acceptance.
- [ ] **Step 5: Implement manager-only startup cleanup and guidance.** Remove `entrypoint.sh` hub validation/registration, Git credential helper and vault-agent startup. Preserve public HTTPS clone, runtime-state mounting, safety-net installation and model setup. In `manager-setup.py`, require nonempty guidance, install the bundle with that guidance through the existing `install` API, preserve its symlinks, then clear the known JSON MCP maps and cached project enablement, including OpenCode `.config/opencode/opencode.json`, and project `.mcp.json`/Claude settings under the checkout and `$HOME/.t3/worktrees`. Parse Codex TOML with `tomllib`; remove every top-level server using `codex mcp remove` with captured output, following `hub_clients.py::codex`. Reject alternate nested MCP profiles rather than silently retain them. Follow `isolated-config.py` cleanup behavior, but do not apply its hub-required input contract to the manager. Fail closed on unhandled legacy config; OWNER: resolve it privately before retrying. Do not broaden this into a general configuration framework.

  Put this short section in `files/manager-guidance.md`, install it into runtime AGENTS instructions after the private bundle, and replace the old Codex homelab-guide append. Manager rules take precedence over bundled personal preferences:

  > ## Manager pod
  > Watch and diagnose the homelab/work runtimes and hubs using context `manager`. Restart means deleting the pod, not scaling or patching its controller. Before acting, get its named Application in `argocd` and require automated sync to be enabled. If automation is absent, explicitly disabled, or unreadable, stop and ask the owner. Recheck immediately before deletion. Code and manifest fixes belong to the homelab pod through a reviewed PR. The manager does not manage `t3code` or perform daily homelab/work tasks.
  > In work namespaces use status and recovery only. Never exec, read pod logs, port-forward or read Secrets. Homelab exec/logs are for diagnosis only. Never use, copy or export credentials found through homelab exec, including for work access. For homelab exec use the POST transport with `KUBECTL_REMOTE_COMMAND_WEBSOCKETS=false`; D7 grants `create pods/exec`, not `get pods/exec`. If that transport is unavailable, stop rather than widening RBAC.
  > Use no external MCP services, vault login, SSH agent, GitHub credentials or alternate Kubernetes credential. Escalate manager recovery and unavailable access to the owner.

  The namespace-to-Application map is exactly the four D7 namespace names mapped to the same Application names. Treat `automated: {}` as enabled unless `enabled: false` is present. Instructions are an operating rule, not an admission controller; the token technically permits deletion even while automation is paused.
- [ ] **Step 6: Wire fixtures and checks.** Update `private-config.test.py::overlay` to stop injecting the removed hub Secret; inject only fake required private/model grants, including a synthetic model-revision annotation. Update `rollout-hash.test.sh` to expect only `t3code-private-config` and `manager-kubeconfig` Secret volumes and to rotate the synthetic model revision instead of the removed hub token. Update mixed smoke staging to install manager guidance/setup rather than require the homelab-guide append. Preserve `hub_clients.py` shared behavior and tests for other consumers. Add the three new test commands and access rendering to CI. Label machine-info `manager`, not `mixed (legacy)`. Use the measured `0.44` CPU and `0.9Gi` to propose `500m`/`1536Mi` requests, with memory headroom and unchanged limits; observe after cutover rather than infer peak usage from one sample. Keep the `40Gi` home PVC unchanged pending the owner decision, because PVC shrinking is not in place.
- [ ] **Step 7: Run verification.** All new tests, `python3 2-k3s/13.t3code/env/tools/private-config.test.py`, `python3 2-k3s/13.t3code/one/files/pi-setup.test.py`, `python3 2-k3s/13.t3code/one/files/t3-pi-settings.test.py`, `python3 2-k3s/13.t3code/one/files/idle-rollout.test.py`, `python3 2-k3s/13.t3code/env/tools/isolated-runtime.test.py`, `python3 2-k3s/13.t3code/env/tools/work-runtime.test.py`, `bash 2-k3s/13.t3code/env/tools/rollout-hash.test.sh`, and `bash 2-k3s/13.t3code/one/tools/sync-shared.sh --check` must pass. `bash -n` on changed shell files exits 0. Run the existing CI T3 config/render and mixed image smoke jobs, plus isolated homelab/work regressions. No verification may read live work output or decrypt Secrets.
- [ ] **Step 8: Commit and open the conversion PR before cutover.** After the branch/base check, commit with `feat: convert mixed runtime into manager`. The still-credentialed agent in `t3code-0` pushes and opens the final PR containing Tasks 2-3, including the owner's ciphertext edit. Follow normal independent review and fix every finding. Record PR/commit references and the owner runbook outside this conversation so reconnecting does not depend on this pod. No attempt to push, merge or sync from the manager after credentials are removed. OWNER: merge only at the checkpoint below.

  ```bash
  git add 2-k3s/13.t3code/one/statefulset.yaml 2-k3s/13.t3code/one/kustomization.yaml 2-k3s/13.t3code/one/ksops-generator.yaml 2-k3s/13.t3code/one/cliproxy-secrets.enc.yaml 2-k3s/13.t3code/one/github-ssh.enc.yaml 2-k3s/13.t3code/one/mcp-hub-client.enc.yaml 2-k3s/13.t3code/one/vaultwarden.enc.yaml 2-k3s/13.t3code/one/files/manager-setup.py 2-k3s/13.t3code/one/files/manager-guidance.md 2-k3s/13.t3code/one/files/entrypoint.sh 2-k3s/13.t3code/one/files/ssh-config.sh 2-k3s/13.t3code/one/files/sshd.sh 2-k3s/13.t3code/one/tools/manager-runtime.test.py 2-k3s/13.t3code/env/tools/private-config.test.py 2-k3s/13.t3code/env/tools/rollout-hash.test.sh 2-k3s/13.t3code/env/tools/isolated-runtime.test.py 2-k3s/13.t3code/env/tools/work-runtime.test.py 2-k3s/13.t3code/env/tools/smoke.sh .github/workflows/ci.yml 2-k3s/23.mcp-hub/files/clients.json 2-k3s/13.t3code/env/isolated-runtime.md
  git commit -m "feat: convert mixed runtime into manager"
  ```

## Task 4: OWNER: cut over from an independent desktop and prove access

**Files:**
- Modify: `2-k3s/13.t3code/env/isolated-runtime.md` (include this runbook in Task 3's conversion commit, before cutover)
- Test: `2-k3s/13.t3code/one/tools/manager-rbac.test.py`, `2-k3s/13.t3code/one/tools/manager-kubeconfig.test.py`, `2-k3s/13.t3code/one/tools/manager-runtime.test.py` (run unchanged); owner authorization/acceptance checks below. No additional recovery controller or live automation.

**Interfaces:** Desktop context `epaflix` remains outside the manager. Runtime context is `manager`. `argocd` CLI below must use the owner's independently authenticated desktop session. The manager never receives those credentials.

- [ ] **Step 1: OWNER: establish the recovery path.** Confirm #1946 is accepted, access PR is merged/synced, final conversion PR passes review, and a desktop can authenticate independently: `kubectl --context epaflix auth can-i patch statefulsets.apps -n t3code` returns `yes`. Confirm access to Git merge and Argo sync from that desktop. Record the pre-cutover revision, current replica/automation policies and idle suspension state privately. Arrange maintenance and end manager sessions; this conversation will be interrupted. If any external access is unavailable, do not proceed.
- [ ] **Step 2: OWNER: preflight effective permissions.** From the desktop use `kubectl --context epaflix --as=system:serviceaccount:t3code:manager-agent --as-group=system:authenticated --as-group=system:serviceaccounts --as-group=system:serviceaccounts:t3code auth can-i ...`. Exercise the entire Task 1 matrix, not just successful cases. Every allowed cell prints `yes`; denied cells print `no` and exit 1. Examples: `get pods -n t3code-work` and `delete pods -n mcp-hub-work` say `yes`; `create pods/exec -n t3code-work`, `get pods/log -n mcp-hub-work`, `create pods/portforward -n t3code-work`, `get secrets -n mcp-hub-work`, `delete pods -n t3code`, and `list nodes` say `no`. Named Application get says `yes`; list/watch/patch and other-name get say `no`. These are authorization requests only, never real work exec/log/Secret requests. Default API discovery/self-access-review permissions are not cluster resource grants.
- [ ] **Step 3: OWNER: provision and verify the real credential.** Run the reviewed Task 2 helper with `--context epaflix`; expect only its readiness message. In a private desktop process, obtain the opaque kubeconfig without displaying it, use a mode-0600 file outside the repository and verify `kubectl --kubeconfig "$MANAGER_CONFIG" config current-context` prints `manager`. The mounted file uses an in-cluster API address that the desktop may not resolve. Repeat the full matrix without impersonation using `kubectl --kubeconfig "$MANAGER_CONFIG" --server "$DESKTOP_API_SERVER" auth can-i ...`, where the owner privately selects the same cluster's desktop-reachable API address from `epaflix`; retain the ServiceAccount user and trusted CA, never copy desktop admin user credentials into the test config. Remove the private test file afterwards. If the actual token fails even though impersonation passed, stop here and repair the grant from the desktop.
- [ ] **Step 4: OWNER: stop reconciliation before merging the conversion.** Following the parent-first pause logic in `env/isolated-runtime.md#input-rotation`, disable `app-of-apps` automated sync, then `t3code` automated sync, from the desktop. Suspend `t3code-idle-rollout` in namespace `t3code`. Verify both Application policies remain disabled, no sync operation is running and no rollout Job is active; wait for any existing Job to finish before continuing. Leave the four managed Applications unchanged. This prevents self-heal or the idle job from rolling this conversation during cleanup.
- [ ] **Step 5: OWNER: merge the final conversion PR from the desktop.** Record its merge SHA as `MERGED_COMMIT_SHA`. Confirm runtime reconciliation is still paused. No merge, manual Argo sync or other Kubernetes mutation is delegated to the manager.
- [ ] **Step 6: OWNER: remove home credentials, then stop the old pod.** In an owner-controlled private terminal, inventory and remove the known home admin kubeconfig file and any duplicate kubeconfigs/admin grants, cached cloud credentials, outbound Git/SSH keys, GitHub CLI/login stores, credential-helper stores and shell/profile token exports. Do not print contents or send findings to the model. Explicit home-file removal, run by the owner through the existing pod before shutdown: `kubectl --context epaflix -n t3code exec t3code-0 -c t3 -- sh -c 'rm -f "$HOME/.kube/config"'`. Audit both the PVC home and passwd home privately; keep only inbound SSH authorized keys/server keys, the private non-credential instruction bundle and model setup. Then immediately run `kubectl --context epaflix -n t3code scale statefulset t3code --replicas=0` and `kubectl --context epaflix -n t3code wait --for=delete pod/t3code-0 --timeout=300s`. Expect scaling success and deletion. Do not rely on `KUBECONFIG` to hide a surviving admin file. Stop if the inventory cannot establish X5; deleting mounts alone is insufficient.
- [ ] **Step 7: OWNER: apply the reviewed conversion and restart explicitly.** Wait for the merged revision's required CI/image publication. `argocd app sync app-of-apps --revision "$MERGED_COMMIT_SHA"` restores tracked parent/child policies. Once the child's automated policy is restored, its auto-sync applies the merged runtime revision and prunes obsolete Secrets, matching the existing rotation procedure. `argocd app wait t3code --sync --timeout 1200` must succeed. Because `spec.replicas` is ignored by the current app, explicitly run `kubectl --context epaflix -n t3code scale statefulset t3code --replicas=1`; wait for Ready with `kubectl --context epaflix -n t3code wait --for=condition=Ready pod/t3code-0 --timeout=1200s`. Verify live controller revision matches the reviewed template. The separate legacy hub auto-sync removes only the manager's digest; confirm its Application is synced and other clients remain intact without printing inventory values.
- [ ] **Step 8: OWNER: accept the new manager without work output.** Confirm actual mounted context `manager`; repeat the authorization matrix using the real in-pod credential. Inspect only names/modes/env names and allow/deny results for credential absence, including a private owner check that the home admin file is gone. Check model access with a harmless synthetic prompt, empty external MCP inventory and correct AGENTS instructions. Check inbound SSH/T3 reconnect and the separate idle-rollout account. Inspect an allowed named Application's automation field; the manager must stop on absent automation, `enabled: false`, or read failure, and recheck before a permitted deletion. Do not pause work Applications just to test this, or delete real work pods as a smoke test. Fresh synthetic startup tests cover configuration; owner observation covers agent compliance.
- [ ] **Step 9: OWNER: finish maintenance.** Restore the recorded idle-rollout suspension state and verify parent and `t3code` self-heal are restored. Observe requests with `kubectl --context epaflix -n t3code top pod t3code-0 --containers` during normal use; retain limits and raise requests through a reviewed change if sustained usage or scheduling problems warrant it. Record only sanitized acceptance results publicly. The manager now watches the four named Applications/namespaces and cannot push, merge, sync or repair its own namespace.

### OWNER: rollback if the account or startup is wrong

- [ ] **OWNER:** Use the desktop `epaflix` kubeconfig, never credentials obtained through homelab exec. Pause `app-of-apps`, then `t3code`, suspend idle rollout, and stop the manager before repairing inputs. If it cannot start, owner access to `t3code` still works. Keep the home admin file removed.
- [ ] **OWNER:** For a bad Role/binding, correct it through a reviewed access PR and manually sync `t3code-manager-access`; if the fix removes a Role, run `argocd app sync t3code-manager-access --prune` so the old grant is deleted. Deleting the generated `manager-agent-kubeconfig` token Secret immediately cuts off a too-broad grant; deleting only the Opaque kubeconfig copy does not revoke an already loaded token. For a bad token/kubeconfig, remove only the failed generated grant as needed and rerun the owner helper; repeat real-credential can-i checks privately. No broad temporary manager Role or ClusterRoleBinding.
- [ ] **OWNER:** For a runtime regression, use a reviewed revert or forward fix that keeps credential removal intact, merge from the desktop and resume with the same controlled sync/start sequence. Do not blindly roll back to the mixed manifest, which would remount old credentials. Restore parent/child automation and idle state only after acceptance. Desktop recovery is the required fallback even if the homelab pod is unavailable.

## Self-review

- S1: D1/D3/D9 are covered by Task 3 runtime cleanup and guidance; D7 by Tasks 1-2 and actual-token checks; D10 by subject-specific denials and separate idle/owner recovery; D11 by installed instructions and the fresh Application check. Legacy deletion remains #1848.
- S2: X1 work-output paths are denied without fetching real output; X2 no old grant enters a new namespace; X3 homelab permissions remain unchanged; X4 public copy uses generic identities; X5 owner home cleanup plus mount/env/Secret removal; X6 explicit installed operating rule. Homelab exec remains the accepted D7 risk, not technical isolation from indirect admin.
- S3: Each task has named tests, exact policy/config values, commands and pass/fail criteria. Interfaces agree on `manager-agent`, `manager-kubeconfig`, key `kubeconfig`, context `manager` and the read-only mount. RF1-RF5 each have owning tests. Tests cannot prove an agent will obey instructions; owner acceptance and the D7 accepted risk remain explicit.
- S4: No manifest/code bodies, new dependency, controller, PVC migration or unrelated hub removal is planned. No design gaps remain beyond the owner decisions below. This plan itself makes no live changes.

## Owner questions

- Q1: OWNER: approve `500m` CPU and `1536Mi` memory requests, keeping existing memory limits?
- Q2: OWNER: keep the existing `40Gi` PVC, or schedule a separate backup/copy migration to a smaller PVC? There is no in-place shrink in this plan.
- Q3: OWNER: which maintenance window will you operate from the independently authenticated desktop for merge, home credential removal, sync and rollback?
- Q4: OWNER: revoke the removed GitHub token, GitHub SSH key and manager Vaultwarden API login at their sources? Recommend revoking after acceptance, once you confirm no remaining desktop or isolated-runtime consumer depends on them. D9 requires removal from the manager, not source revocation.
