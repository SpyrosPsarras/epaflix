# Work Notion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close #1945 by giving only the work hub and work runtime a fresh, independently consented Notion connection and proving one owner-run read.

**Architecture:** Reuse the existing hosted Notion OAuth bootstrap, namespace-local grant store and single refresh owner. Most functionality already exists. This is primarily an owner cutover with private inventory changes, two public rollout counters, documentation and focused regression tests, not a new upstream implementation.

**Tech Stack:** Python standard-library OAuth bootstrap, existing MCP 2.2.0 gateway and fixtures, Kubernetes Secrets, Kustomize, Argo CD, isolated runtime input validation.

**Spec:** `docs/superpowers/specs/2026-10-19-t3-pod-split-design.md`, D5, D6, D11, planned order and anti-goals. Operational contracts: `2-k3s/23.mcp-hub/instances/README.md`, `2-k3s/13.t3code-work/README.md` and `2-k3s/13.t3code/env/isolated-runtime.md#input-rotation`.

## Global Constraints

- "The work hub owns its own `mcp-hub-notion-grant`. Do not copy the legacy grant or refresh token."
- "Keep one refresh owner, as the instance contract requires."
- "The work pod is the only T3 pod that receives this service."
- "Keep the existing searxng service on homelab and work. Preserve their independent upstream credentials."
- "For hub changes, stage approved private inputs before merge."
- "Keep the runtime stopped until inputs, digests and the published revision agree."
- "Work data stays outside the manager's model context."
- "Leave homelab/work boundary rules unchanged."
- "A public file names an employer or account identity." is a failure.
- OWNER: Browser sign-in, account/workspace verification, consent, private Secret staging, maintenance stops and merge approval are human checkpoints. The agent never logs in, reads work data or Secret values, or prints credentials.
- OAuth tokens are obtained inside the existing bootstrap process and piped directly to Kubernetes over stdin. No tokens in chat, argv, shell tracing, recordings or Git. Existing ordinary work and search bearers are preserved; this task mints no replacement client bearer.
- No code comments, new dependencies, new backend, new login-on-startup behavior, new namespace permissions or desktop migration.

## Review Focus

- F1: Missing, incomplete or revoked work grant must fail closed without reading any legacy/homelab grant. Task 1 adds failure fixtures.
- F2: A valid work grant must expose `/notion` only on the selected work inventory; wrong-hub, legacy and absent bearers must be rejected. Task 1 adds request assertions.
- F3: Changed runtime inventory with stale hashes must refuse startup; valid replacement must remove stale registrations and retain policies and work state. Task 1 adds a runtime fixture.
- F4: Stable-name Secret edits must roll only the intended work hub after its counter advances; search, homelab and legacy must remain unchanged. Task 1 adds a work render fixture; Task 2 stages before merge.
- F5: Bootstrap without explicit context must do no OAuth or Kubernetes work, and successful work bootstrap must write only its own namespace without emitting tokens. Task 1 retains and strengthens the existing bootstrap fixture.

---

## Existing implementation and file map

`tools/bootstrap-notion.py main()` already supports `--instance work --context <ctx>`, registers a work-specific OAuth client and writes the grant through `_kubectl(ctx, *args, stdin=None, namespace=NS)`. `files/mcp_hub.py instance_app(environment, services_file, credentials_file)` already accepts URL-only Notion configuration, requires an independent usable grant and connects `Grant.headers()` / `Grant.invalidate()` to the existing forwarder. `SecretStore(namespace, name)` uses only that namespace, and `Grant` owns refresh locking and write-back.

The base already supplies namespace-local get/patch RBAC on exactly `mcp-hub-notion-grant`, one replica and `Recreate`. Keep it. The authenticated `instances/searxng/` backend is the model for other authenticated upstreams, but Notion already has OAuth upstream authentication. Do not copy search into a new Notion Deployment or add a static Notion bearer.

Public files to change:

- `2-k3s/23.mcp-hub/tests/test_instances.py`: selected-inventory Notion requests, grant failures, bootstrap assertions and work-only stable-Secret rollout coverage.
- `2-k3s/23.mcp-hub/tests/test_local_clients.py` and `tests/test_searxng.py`: update only work hub counter expectations when the approved revision changes.
- `2-k3s/13.t3code/env/tools/work-runtime.test.py`: synthetic work Notion registration, digest refusal and retained-state checks.
- `2-k3s/23.mcp-hub/instances/work/kustomization.yaml`: work hub `mcp-hub.epaflix.com/inputs-revision`, currently `r2`, to `r3` after staging.
- `2-k3s/13.t3code-work/statefulset.yaml`: `t3code.epaflix.com/inputs-grants-revision`, currently `r3`, to `r4` after staging.
- `2-k3s/23.mcp-hub/instances/README.md`: distinguish homelab search-only inventory from approved work search plus Notion, and document owner consent/read and work rotation.
- `2-k3s/13.t3code-work/README.md`: append the owner-added Notion procedure; preserve the historical search-only first-deployment instructions.

Private, outside Git: work hub `mcp-hub-instance`, work hub `mcp-hub-notion-grant`, work runtime `t3-runtime-inputs`, their reviewed provisioning source and private acceptance record. No changes to `t3-runtime-grants`, either search Secret, vault grants, Azure enrollment, homelab inputs or legacy inputs. Rebase/recheck counters before implementation; if either advanced, choose its next unused opaque counter and update corresponding expectations.

### Task 1: Pin the existing work-only behavior offline

**Files:**
- `2-k3s/23.mcp-hub/tests/test_instances.py`
- `2-k3s/13.t3code/env/tools/work-runtime.test.py`
 Production Python files remain unchanged unless a failing requirement test proves a gap.

**Interfaces:** Consume existing module function `fixture(path, environment, names=("searxng",))`, `mcp_hub.instance_app(environment, services_file, credentials_file)`, `notion_grant.SecretStore`, `Grant`, and `hub_clients.pi(config, hub, env_var, services=None)`. Runtime tests use existing `WorkRuntimeTests.change(name, transform)`, `prepare()` and `runtime.apply(inputs, home, environment)`. Produce no new runtime interfaces.

- [ ] **Step 1: Add `InstanceTests.test_work_notion_requests_leave_homelab_without_notion` without real HTTP or Kubernetes calls.** work fixture lists `['searxng', 'notion']`, homelab lists `['searxng']`. Configure Notion with exactly `{'url': 'https://mcp.notion.com/mcp'}`; mock grant storage and use ASGI upstream transport. Assert work initialization and a synthetic read return the fixture value, the forwarded Authorization is the synthetic OAuth access token rather than the client bearer, and work rejects absent/legacy/homelab tokens with 401. Assert homelab `/notion` is 404 with its own valid token, search still initializes on both hubs, and homelab construction never touches Notion storage.

```python
assert work_initialize.status_code == 200 and work_read.json() == synthetic_read_result
assert forwarded_authorization == "Bearer " + synthetic_oauth_access_token
assert all(response.status_code == 401 for response in rejected_work_requests)
assert homelab_notion.status_code == 404
assert all(response.status_code == 200 for response in search_initializations)
assert homelab_notion_store.call_count == 0
```

- [ ] **Step 2: Extend `test_notion_refresh_owns_only_each_instances_grant` with: unusable work grants and rejected configuration overrides.** Use synthetic missing Secret, incomplete fields and `invalid_grant` responses; capture exceptions without real requests. Patch legacy upstream creation to raise if called. Keep the existing namespace and refresh ownership assertions rather than duplicating them.

```python
assert isinstance(missing_secret_error, UpstreamError)
assert isinstance(incomplete_grant_error, ValueError)
assert isinstance(invalid_grant_error, UpstreamError)
assert legacy_upstream.call_count == 0
assert isinstance(static_token_error, ValueError)
assert isinstance(namespace_override_error, ValueError)
```

- [ ] **Step 3: Extend `test_bootstrap_targets_only_selected_namespace_without_auth_flows` with: exact missing-context exit status, no side effects and token confinement.** Also mock `_existing`, `_authorize` and `_kubectl` in the missing-context case. Capture stderr as well as stdout during successful bootstrap and use distinct synthetic token values. Keep the existing namespace, context and compatibility fixtures; they are not deployment approval.

```python
assert missing_context_exit.code == 2
assert not any(mock.called for mock in (json_call, existing, authorize, kubectl))
assert kubectl_args == ("fixture-context", "create", "-f", "-")
assert all(token in secret_stdin for token in synthetic_tokens)
assert all(token not in repr(kubectl_args) for token in synthetic_tokens)
assert all(token not in stdout + stderr for token in synthetic_tokens)
```

- [ ] **Step 4: Add `WorkRuntimeTests.test_work_notion_allowlist_rotation_preserves_boundaries`.** write `{'environment':'work','services':['searxng','notion']}` without changing its digest and assert validation refuses it. Recompute through existing `refresh`, seed stale maintained servers, then prepare/apply twice. Assert the private snapshot, Pi `mcp.json`, both Claude registrations, Codex configuration and enforced requirements contain exactly search and Notion on the work hub. Assert literal token references, no credential values, and unchanged policy files, work-auth inputs, grant digests, history and enrolled Azure fixture. Retain the existing search-only homelab fixture coverage.

```python
assert stale_digest_error is not None
assert all(services == {"searxng", "notion"} for services in registered_services)
assert all(url.startswith(work_hub_url) for url in registered_urls)
assert token_references == expected_literal_references and not exposed_credential_values
assert retained_state_after == retained_state_before
assert second_apply_snapshot == first_apply_snapshot
```

- [ ] **Step 5: Extend `test_homelab_stable_secret_requires_tracked_input_revision` with: reciprocal work-only rotation using synthetic search/Notion inventory.** Keep its existing work `r2` assertion until Task 2 advances the counter. Reuse existing replica, strategy, RBAC and stable-reference coverage instead of repeating it; no real Secret is read.

```python
assert rotated_work_secret["data"] != original_work_secret["data"]
assert rotated_work_template == original_work_template
assert revised_work_template != original_work_template
assert revised_work_without_counter == rotated_work
assert revised_search == original_search
assert render(homelab_overlay) == original_homelab
```

- [ ] **Step 6: Run the new tests and the existing suites.** These are regression proofs for already implemented behavior, so passing immediately is expected, not a reason to invent a production change. A failing new assertion requires diagnosis before changing code.

```sh
python3 -m venv /tmp/work-notion-tests
/tmp/work-notion-tests/bin/pip install mcp==2.2.0 -r .github/hooks/requirements.txt
/tmp/work-notion-tests/bin/python -m unittest discover -s 2-k3s/23.mcp-hub/tests -v
/tmp/work-notion-tests/bin/python 2-k3s/13.t3code/env/tools/work-runtime.test.py -v
/tmp/work-notion-tests/bin/python 2-k3s/23.mcp-hub/files/mcp_hub.py --selftest
python3 2-k3s/23.mcp-hub/tools/hub_clients.py --selftest
bash 2-k3s/13.t3code/one/tools/sync-shared.sh --check
```

Expected: unittest suites end `OK` with no skipped required render checks, selftests report `hub selftest OK` and `hub_clients selftest OK`, and shared-copy check exits 0. Fixtures must not print tokens or contact a real workspace. Record failures honestly; do not claim live acceptance from these tests.

- [ ] **Step 7: Commit.** Verify the branch contains `origin/main` and is rebased onto it, then pass the top-level review gate before running these commands. Do not add a co-author.

```sh
git merge-base --is-ancestor origin/main HEAD
git add 2-k3s/23.mcp-hub/tests/test_instances.py 2-k3s/13.t3code/env/tools/work-runtime.test.py
git commit -m "test: pin work-only Notion boundaries"
```

### Task 2: Stage owner inputs and approve the work-only revision

**Files:**
- `2-k3s/23.mcp-hub/instances/work/kustomization.yaml`
- `2-k3s/13.t3code-work/statefulset.yaml`
- `2-k3s/23.mcp-hub/tests/test_instances.py`
- `2-k3s/23.mcp-hub/tests/test_local_clients.py`
- `2-k3s/23.mcp-hub/tests/test_searxng.py`
- `2-k3s/23.mcp-hub/instances/README.md`
- `2-k3s/13.t3code-work/README.md`

Private inputs are owner-managed and never enter the branch.

**Interfaces:** Produce the same serialized `services.json` bytes in work hub and runtime, `{"environment":"work","services":["searxng","notion"]}`. Work credentials retain all existing `clients` and the entire search connection; add only `upstreams.notion = {'url': 'https://mcp.notion.com/mcp'}`. Runtime `runtime.json.sha256['services.json']` binds those exact bytes and `runtime.json.approvals.hub` records the private approval. Preserve every other input, approval and grant digest.

- [ ] **Step 1: OWNER: Confirm prerequisites and approve the window privately.** Confirm the work backend's first-deployment and owner-enrollment acceptance from its README is complete, including existing organizational policies, dedicated grants and work routing approval. Do not rerun `provision.py inputs`, which is creation-only and intentionally requires search-only initial inventory. Do not rerun GitHub, Azure or T3 Connect login. If prerequisites are incomplete, stop and finish #1837 separately. Approve the exact work Notion account/workspace, consent scope and owner read target without publishing their identity. Pause work sessions and any other work hub consumers; account enrollment is separate authorization from Secret staging.

- [ ] **Step 2: OWNER: Stop work through the shared input-rotation procedure.** From a private administrative terminal, set `ctx` to the explicitly approved homelab administrative context. `ctx` must point at the same cluster that the input-rotation procedure targets. Never use credentials found inside a homelab pod for work access.

```sh
kubectl --context "$ctx" -n argocd patch application app-of-apps --type json \
  -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
kubectl --context "$ctx" -n argocd patch application t3code-work --type json \
  -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
```

Expected: both Applications patched. Check their automation is absent and no sync is in flight before continuing. Do not manually sync either while staging. The hub's own automation stays enabled.

```sh
kubectl --context "$ctx" -n t3code-work scale statefulset work --replicas=0
kubectl --context "$ctx" -n t3code-work wait --for=delete pod/work-0 --timeout=300s
```

Expected: StatefulSet scaled and `pod/work-0 condition met`, or verified pod absence. Recheck absence and both paused policies immediately before private writes. Do not stop homelab or the manager.

- [ ] **Step 3: OWNER: Consent in the browser and stage the independent grant.** Run on a workstation with browser and authorized kubectl access, without recording output:

```sh
python3 2-k3s/23.mcp-hub/tools/bootstrap-notion.py --instance work --context "$ctx"
```

The owner verifies the selected work account and workspace before consent. Abort if a personal session is selected. The tool obtains fresh OAuth tokens and sends Secret JSON to `kubectl create/replace -f -` through stdin; never copy a legacy grant. Expected private terminal output ends `wrote Secret mcp-hub-work/mcp-hub-notion-grant. Hard expiry: ...`. Report only pass/fail to the agent. If a work grant already exists, verify its provenance privately before renewing; do not overwrite an unexplained grant. No second hub or test client may refresh this grant.

- [ ] **Step 4: OWNER: Stage the approved work hub and runtime inputs privately.** Use a mode-0700 staging directory outside the repo, mode-0600 backups and the approved private source. Back up work hub inputs and runtime inputs only. Never restore an old rotating Notion grant as a rollback technique. Build resourceVersion-tested JSON patches: first operation tests `/metadata/resourceVersion`; remaining operations replace only `/data/services.json` and `/data/credentials.json` for the hub, and `/data/services.json` and `/data/runtime.json` for the runtime. Use Kubernetes base64 encoding. Preserve all other bytes and private source fields described in Interfaces.

Validate the complete proposed seven-file work input set with `isolated-config.validate(Path(inputs), Path(home), 'work')` in a private process using existing reviewed grants in its clean environment, following the current provisioner's validation pattern. Use a private clean temporary home, suppress diagnostics and never start login. Require success, matching service bytes and digests, unchanged required policies/checks and unchanged work grants before writes. A validation-only attempt must not patch or stop resources.

```sh
kubectl --context "$ctx" -n mcp-hub-work patch secret mcp-hub-instance \
  --type=json --patch-file=/private/work-notion/hub.patch.json
kubectl --context "$ctx" -n t3code-work patch secret t3-runtime-inputs \
  --type=json --patch-file=/private/work-notion/runtime.patch.json
```

Expected: each named Secret reports `patched`, with no values. Run only from the owner's private terminal; suppress API error bodies. A resourceVersion conflict means reread privately and regenerate, never drop the concurrency check. Retain only the required two hub data keys. Update the approved private provisioning source too. The running hub keeps old in-memory configuration until rollout; treat the interval as maintenance downtime. Grant and input staging must finish before merge.

- [ ] **Step 5: Prepare and verify the small public diff.** Advance only the work hub counter `r2` to `r3` and work runtime counter `r3` to `r4`. Update work expectations in `test_homelab_stable_secret_requires_tracked_input_revision`, `IngressTests.test_each_instance_uses_only_internal_traefik_and_lan_dns` and `SearchTests.test_both_hubs_render_independent_authenticated_search_backends`. Do not change search's `r1` or homelab's counter. Update the READMEs with this owner cutover, not private identities. Run Task 1 commands again and:

```sh
kustomize build 2-k3s/23.mcp-hub/instances/work >/dev/null
kustomize build 2-k3s/23.mcp-hub/instances/homelab >/dev/null
kustomize build 2-k3s/13.t3code-work >/dev/null
git diff --check
```

Expected: exit 0, passing suites, and reviewed rendered templates differ only in approved work revisions. Render fixtures prove F4; actual inventory remains private. Follow the repository's independent review gate before calling the code ready. Before any commit, verify the branch is rebased onto `origin/main`; never add a co-author.

- [ ] **Step 6: Commit.** Verify the branch contains `origin/main` and is rebased onto it, then pass the top-level review gate before running these commands. Do not add a co-author. Create this commit before Step 7's merge approval; Step 7 consumes this reviewed commit.

```sh
git merge-base --is-ancestor origin/main HEAD
git add 2-k3s/23.mcp-hub/instances/work/kustomization.yaml 2-k3s/13.t3code-work/statefulset.yaml 2-k3s/23.mcp-hub/tests/test_instances.py 2-k3s/23.mcp-hub/tests/test_local_clients.py 2-k3s/23.mcp-hub/tests/test_searxng.py 2-k3s/23.mcp-hub/instances/README.md 2-k3s/13.t3code-work/README.md
git commit -m "feat: enable approved work Notion connection"
```

- [ ] **Step 7: OWNER: Approve merge only after private staging and review pass.** Keep parent and work runtime paused. Merge the reviewed commit from Step 6 and wait for any referenced runtime image to be published. The work hub syncs automatically and rolls immediately; no manual hub restart or scaling is needed.

```sh
argocd app wait mcp-hub-work --sync --health --timeout 600
kubectl --context "$ctx" -n mcp-hub-work rollout status deployment/mcp-hub --timeout=300s
```

Expected: work hub Synced/Healthy and Deployment successfully rolled out with the approved counter, one replica and `Recreate`. Use status only in agent-visible evidence, never work hub logs. OWNER: Before resuming work, privately verify authorized MCP initialization through `/notion`, 401 with no client bearer, and hosted Notion rejection without OAuth. Record no response bodies. OWNER: Using existing independently held client grants in a private process, verify wrong-hub and legacy bearers receive 401 from work `/notion`, with no content returned. Do not expose grants or response bodies to the agent; Task 1 also pins these cases offline. Preserve search health and check homelab/legacy status without reading their data.

### Task 3: Resume work and obtain owner-only end-to-end acceptance

**Files:** No additional tracked files. Acceptance record stays private.

**Interfaces:** Consume Task 2's staged inputs and reviewed merged revision. Produce only sanitized owner confirmation, not tool output or workspace identifiers.

- [ ] **Step 1: OWNER: Resume through the shared procedure after all inputs, counters and image agree.**

```sh
revision=MERGED_COMMIT_SHA
argocd app sync app-of-apps --revision "$revision"
argocd app wait t3code-work --sync --health --timeout 1200
kubectl --context "$ctx" -n t3code-work wait --for=condition=Ready pod/work-0 --timeout=300s
```

Expected: parent and child automation restored, work Synced/Healthy and `work-0` Ready with the approved runtime counter. Runtime pruning stays enabled without self-healing; hubs retain pruning and self-healing. Do not force a restart of a running runtime to compensate for incorrect inputs.

- [ ] **Step 2: OWNER: Check registrations locally in work, then perform one real read there.** In the owner's private terminal in `work-0`'s `t3` container, verify `/home/t3/.pi/agent/mcp.json` contains `notion` and `searxng` on the work hub with environment-variable bearer references. Verify the generated runtime service snapshot, Claude/Codex registrations and enforced Codex requirements agree. Do not print entire configuration files to the agent. Retained organizational hooks and policy checks must pass, and existing work history and enrollment must remain intact.

Using the work pod's configured MCP client, discover the hosted Notion tool schema and invoke its read/fetch operation on the owner's preapproved read target. Tool naming comes from live discovery, not a guessed method. Expected: a successful non-error tool result, confirmed by the owner. This is the issue's end-to-end read. The agent does not run it, inspect its result, request work pod logs/exec output, or accept workspace content into its context. `initialize`/`tools/list` alone do not satisfy the read criterion. Return only `work Notion read: PASS` or `FAIL`.

- [ ] **Step 3: OWNER: Confirm the exclusion and finish maintenance.** Privately confirm the unchanged homelab hub inventory and pod registrations do not include Notion, and an authenticated homelab `/notion` request returns 404. Do not add Notion to homelab, manager or desktop clients. Verify existing work search remains usable with a public synthetic query. Resume paused work hub consumers only after these checks. Public evidence may state only test results, revision markers and sanitized pass/fail.

- [ ] **Step 4: OWNER: If acceptance fails, stop instead of weakening the boundary.** Keep or return work to the paused-parent/paused-child, stopped-runtime state before input changes. Restore previous reviewed hub/runtime inventory bytes, their matching digests and approval references, then advance both affected public counters again through review and merge before resuming. Keep the hub's single refresh owner; never replay a backed-up rotating Notion refresh token. Obtain fresh owner consent if needed. Preserve all other work grants and homelab/legacy resources. Close #1945 only after the owner read and exclusion checks pass.

## Execution

Confirm the execution method and account choice with the owner before starting. The account and read target stay private and never enter the conversation. Only approval and read completion status are shared.
