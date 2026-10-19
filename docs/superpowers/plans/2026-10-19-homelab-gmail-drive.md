# Homelab Gmail and Drive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete #1944 with fresh Google consent, an authenticated homelab-only Workspace upstream, and one Gmail read and one Drive read through the homelab runtime.

**Architecture:** Run the pinned community Workspace server behind a small authenticated gateway in one homelab Deployment. Register a local `gmail_send_draft` tool inside the raw Workspace process, so one endpoint advertises all 31 tools without gateway aggregation. Bind the raw server to pod loopback, expose only the gateway, and reuse the maintained hub forwarder and bearer middleware. Stage private inventories before merging, then rotate the stopped homelab runtime's allowlist through the existing input contract.

**Tech Stack:** Python stdlib, Starlette, `mcp==2.2.0`, `ghcr.io/taylorwilsdon/google_workspace_mcp:2.0.1`, Kubernetes, Kustomize, Argo CD, unittest.

**Spec:** `docs/superpowers/specs/2026-10-19-t3-pod-split-design.md`, D4, D6, D11, planned order A1 and Q2. This plan does not implement the manager conversion, Notion migration, desktop migration or legacy retirement.

## Global Constraints

The following requirements are copied from the spec. They apply to every task.

- "Use a new upstream bearer and fresh Google OAuth consent."
- "The owner signs in through the browser and verifies the account before approving consent."
- "Copy no legacy token or refresh token."
- "Keep upstream credentials on the hub, not in clients."
- "NetworkPolicy alone is not sufficient."
- "Keep the existing searxng service on homelab and work."
- "Leave homelab/work boundary rules unchanged."
- "Keep private evidence outside this public repo."

All browser sign-in, account checks, consent, private Secret staging, stopping/resuming consumers and merge approvals are marked `OWNER:`. The agent never logs in. No token, email, account identifier, private endpoint or Google content enters chat, public files, logs used as public evidence, shell arguments or shell history. Generate random bearers inside the process that consumes them and send Secret documents directly to kubectl stdin. Capture and suppress Secret command output and error bodies. Add no code comments. Keep legacy manifests, grants and clients unchanged.

## Review Focus

- R1: A browser has the wrong account selected, consent is denied, scopes are incomplete or PKCE state is wrong. Abort without writing a grant. Task 2 pins this with `test_instance_consent_failures_write_nothing`.
- R2: A caller reaches the upstream Service directly or guesses a raw port. Reject missing, malformed and ordinary client bearers on every MCP method; the raw listener must not bind the pod IP. Task 1 pins this with `test_direct_auth_all_methods` and `test_homelab_only_render`.
- R3: A concurrent Secret update or interrupted input rotation occurs. Never overwrite another change or start a runtime with mismatched digests. Tasks 2 and 3 pin this with `test_instance_write_conflict_is_closed` and `test_google_inventory_digest_mismatch_stops_startup`.
- R4: Google credentials expire or the backend restarts. Exactly one Workspace process writes the credentials file; the send-draft helper refreshes access tokens in memory only. Credentials remain writable, startup never enrolls an account, and Notion refresh ownership stays unchanged. Task 1 pins this with `test_seed_and_restart_contract` and `test_send_draft_refresh_never_writes`; Task 3 runs the existing Notion ownership test.
- R5: A stale client file or an unexpected tool inventory survives cutover. Homelab advertises exactly the approved complete Gmail and Drive tool set, work remains unchanged, and `start_google_auth` stays absent. Unknown new tools after an image bump fail the inventory check, never silently enter the approved set. Tasks 1 and 3 pin this with `test_pinned_image_complete_inventory` and `test_google_homelab_registration_only`.

---

## Decisions and source checks

- D1: The existing `workspace-mcp.yaml` is Drive-only because its command selects `--tools drive`, not because the image lacks Gmail. The pinned [README](https://github.com/taylorwilsdon/google_workspace_mcp/blob/v2.0.1/README.md) documents `--tools gmail drive`. Pinned `main.py` supports `--single-user`, `--tool-tier complete` and `--disabled-tools`. Its legacy HTTP mode has no MCP authentication; `--single-user` cannot be combined with its OAuth 2.1 server mode. Do not mistake Google OAuth for an independent inbound bearer.
- D2: Use an authenticated sidecar gateway, following `instances/searxng/searxng.py`. The raw server listens at `127.0.0.1:8001`; only the gateway listens on `0.0.0.0:8000`. The Service exposes only 8000. The gateway's `/mcp` checks its independent bearer before forwarding. Loopback confinement, not just a Service declaration or NetworkPolicy, prevents ordinary clients from bypassing it.
- D3: One raw server, one credentials-file writer, one fresh grant. Pinned `auth/credential_store.py` reads JSON containing `refresh_token`, `token_uri`, `client_id`, `client_secret` and `scopes`. Follow the legacy credential-file format and URL-quoted filename in a writable memory-backed `/creds` volume. After the raw container's seed step, exactly one process writes the credentials file, the Workspace process. D6's helper in that same process refreshes its own access token in memory only and never writes the shared credentials file. Google refresh tokens do not rotate on refresh, so even an in-memory access-token refresh by a second process would not invalidate the grant. No second process is needed here. The gateway and hub receive no Google grant or OAuth client and do not refresh Google credentials. Keep one replica and `Recreate`. No new Kubernetes Secret-read RBAC or changes to `notion_grant.py` are needed.
- D4: `/gmail` and `/drive` both forward to this single authenticated `/mcp` and use the same Workspace upstream identity. Both registrations therefore advertise the same combined tool list. They are aliases, not per-service authorization boundaries. This avoids a new tool-filtering proxy and keeps one credentials-file writer. Existing hub routing already supports both names.
- D5: The owner resolved design Q2 with tool scope "Legacy plus extra tools" and approvals "No approval, run directly". Enable every Gmail and Drive tool offered by the pinned image's complete tier, including tools the legacy pod never had: Gmail filters, label management, drafts, send and trash, and all Drive writes and sharing. The agent may send, trash and share without asking. There are no hub-side or client-side approval gates for these homelab tools. Enrollment remains an owner-only bootstrap step, so `start_google_auth` stays disabled.
- D6: The owner chose "Add our own send-draft tool" because v2.0.1 cannot send an existing draft; use option c, a raw-process startup extension in `instances/google/workspace.py`. The existing hub cannot combine a local module and a forwarded upstream on one endpoint because matching routes select one handler and upstream forwarding does not aggregate MCP tools. The launcher imports pinned `main`, saves `main.filter_server_tools`, and replaces that hook with a function that runs the original filter, calls `register_send_draft(server, credential_path)` after tier filtering, and returns the original filter's return value for `main.py`'s `tools_removed` startup summary. Call `main.main()` to retain the pinned CLI and HTTP lifecycle without patching upstream source or shared hub routing. The version-specific hook risks breaking after an image bump; the required real-image test must exercise it and block incompatible changes.

The added tool has signature `gmail_send_draft(draft_id: str, user_google_email: str | None = None) -> str`. It ignores `user_google_email`, which pinned `SecureFastMCP.call_tool` injects when `USER_GOOGLE_EMAIL` is set and OAuth 2.1 is off. Reuse legacy `gmail_mcp.Auth`, `_api` and `_send_draft`, loading only that module in the raw container without calling its `server()` or registering its other tools. Initialize its `AUTH` from the seeded JSON, not a legacy grant. The tool rejects empty, whitespace-only or control-containing ids before HTTP; missing arguments are MCP validation errors. It calls the Gmail `users.drafts.send` REST operation, `POST /gmail/v1/users/me/drafts/send` with exactly `{"id": draft_id}`, and returns the legacy JSON result with `sent`, `message_id` and `thread_id`. Adapt expected failures to the raw server's `fastmcp.exceptions.ToolError` with fixed messages, never raw Google response bodies or credential values. Do not use legacy `_run`, which imports the hub SDK's error class. No automatic retry of a send and no approval gate. Reuse the legacy selftest and hub MCP error-test pattern; neither currently tests send-draft HTTP, so add that coverage in Task 1.

Keep the legacy OpenCode `ASK` table unchanged and do not extend it; its client prompts do not apply to Pi or gate this homelab upstream, whose complete tool set the owner approved for direct execution.

For D5, launch with `--transport streamable-http --single-user --tools gmail drive --tool-tier complete --disabled-tools start_google_auth`, without `--read-only`. Consent and seed exactly this 12-scope set, named `complete_scope_set` in tests:

```text
https://www.googleapis.com/auth/drive
https://www.googleapis.com/auth/drive.file
https://www.googleapis.com/auth/drive.readonly
https://www.googleapis.com/auth/gmail.compose
https://www.googleapis.com/auth/gmail.labels
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.send
https://www.googleapis.com/auth/gmail.settings.basic
https://www.googleapis.com/auth/userinfo.email
https://www.googleapis.com/auth/userinfo.profile
openid
```

Verified from the v2.0.1 source archive fetched with `curl` from `https://api.github.com/repos/taylorwilsdon/google_workspace_mcp/tarball/v2.0.1`. In pinned [auth/scopes.py](https://github.com/taylorwilsdon/google_workspace_mcp/blob/v2.0.1/auth/scopes.py), `get_scopes_for_tools(["gmail", "drive"])` with read-only mode off returns exactly `BASE_SCOPES ∪ GMAIL_SCOPES ∪ DRIVE_SCOPES`, the set above. Retain all entries, including scopes implied by broader scopes, rather than substituting a guessed minimal grant. The homelab bootstrap requests exactly this set and requires the returned scope set to equal it. The legacy default remains unchanged.

Expected Gmail tools, 14: `search_gmail_messages`, `get_gmail_message_content`, `get_gmail_messages_content_batch`, `send_gmail_message`, `get_gmail_attachment_content`, `get_gmail_thread_content`, `modify_gmail_message_labels`, `list_gmail_labels`, `manage_gmail_label`, `draft_gmail_message`, `list_gmail_filters`, `manage_gmail_filter`, `get_gmail_threads_content_batch`, `batch_modify_gmail_message_labels`.

Expected Drive tools, 16: `search_drive_files`, `get_drive_file_content`, `get_drive_file_download_url`, `create_drive_file`, `create_drive_folder`, `import_to_google_doc`, `import_to_google_slides`, `import_to_google_sheets`, `get_drive_shareable_link`, `list_drive_items`, `copy_drive_file`, `update_drive_file`, `manage_drive_access`, `set_drive_file_permissions`, `get_drive_file_permissions`, `check_drive_file_public_access`.

The pinned image's base union is these 30 names. D6 adds `gmail_send_draft`, so the exact served union on `/mcp` and both hub aliases is 31 tools, 15 Gmail and 16 Drive. Agents see one `gmail` registration with all 31 tools, not an extra send-draft route. The 30 upstream names were verified against pinned [core/tool_tiers.yaml](https://github.com/taylorwilsdon/google_workspace_mcp/blob/v2.0.1/core/tool_tiers.yaml), the cumulative core + extended + complete resolution in [core/tool_tier_loader.py](https://github.com/taylorwilsdon/google_workspace_mcp/blob/v2.0.1/core/tool_tier_loader.py), and the tool registrations and `require_google_service` scope decorators in [gmail/gmail_tools.py](https://github.com/taylorwilsdon/google_workspace_mcp/blob/v2.0.1/gmail/gmail_tools.py) and [gdrive/drive_tools.py](https://github.com/taylorwilsdon/google_workspace_mcp/blob/v2.0.1/gdrive/drive_tools.py). Scope aliases are resolved in pinned [auth/service_decorator.py](https://github.com/taylorwilsdon/google_workspace_mcp/blob/v2.0.1/auth/service_decorator.py). Exclude only `start_google_auth` from the tier union. Gmail trash uses label modification with `TRASH`; there is no separate trash tool to invent. The real-image test must confirm this exact union before rollout; a mismatch blocks release rather than relaxing the assertion.

## File map

Paths below are repository-relative. `HUB` in commands means `2-k3s/23.mcp-hub`.

| Action | Path | Responsibility |
|---|---|---|
| Create | `2-k3s/23.mcp-hub/instances/google/gateway.py` | Authenticated forwarding, no Google enrollment or refresh |
| Create | `2-k3s/23.mcp-hub/instances/google/seed.py` | Private writable credential-file initialization |
| Create | `2-k3s/23.mcp-hub/instances/google/workspace.py` | Raw-process startup extension and legacy send-draft reuse |
| Create | `2-k3s/23.mcp-hub/instances/google/workspace.yaml` | Loopback server, gateway, Service and hardening |
| Create | `2-k3s/23.mcp-hub/instances/google/kustomization.yaml` | Backend resources and content-hashed runner ConfigMap |
| Create | `2-k3s/23.mcp-hub/tests/test_google.py` | Gateway, send-draft, seed, render and real-image contract tests |
| Create | `2-k3s/23.mcp-hub/tests/test_bootstrap_gmail.py` | Synthetic OAuth and namespace-bound Secret writes |
| Modify | `2-k3s/23.mcp-hub/tools/bootstrap-gmail.py` | Explicit homelab target; unchanged legacy default |
| Modify | `2-k3s/23.mcp-hub/instances/homelab/kustomization.yaml` | Include Google backend and approved hub input revision |
| Modify | `2-k3s/23.mcp-hub/tests/test_instances.py` | Homelab routing and revision fixtures |
| Modify | `2-k3s/23.mcp-hub/tests/test_searxng.py` | Expected homelab Deployment set and revision |
| Modify | `2-k3s/23.mcp-hub/tests/test_local_clients.py` | Homelab revision expectation only |
| Modify | `2-k3s/13.t3code/env/tools/isolated-runtime.test.py` | Google allowlist, digest and generated-registration fixtures |
| Modify | `2-k3s/13.t3code-homelab/statefulset.yaml` | Separately reviewed runtime input revision after private staging |
| Modify | `.github/workflows/ci.yml` | Required synthetic pinned-image inventory check |
| Modify | `2-k3s/23.mcp-hub/instances/README.md`, `2-k3s/23.mcp-hub/README.md` | New instance-only setup, Q2 outcome and stopped rollout procedure |
| Reuse unchanged | `2-k3s/23.mcp-hub/files/gmail_mcp.py` | Raw-container mount of existing send-draft and memory-only auth logic |

Do not modify the shared instance base, work overlays, legacy `workspace-mcp.yaml`, Notion implementation, shared service registry or generated client helpers. The existing registries already accept `gmail` and `drive`. No public Secret manifest is added, encrypted or otherwise. Private provisioning sources are outside this repo and must be updated by the owner.

### Task 1: Build the authenticated, complete-tier Google backend

**Files:**
- Create: `2-k3s/23.mcp-hub/instances/google/gateway.py`
- Create: `2-k3s/23.mcp-hub/instances/google/seed.py`
- Create: `2-k3s/23.mcp-hub/instances/google/workspace.py`
- Create: `2-k3s/23.mcp-hub/instances/google/workspace.yaml`
- Create: `2-k3s/23.mcp-hub/instances/google/kustomization.yaml`
- Modify: `.github/workflows/ci.yml`
- Test: `2-k3s/23.mcp-hub/tests/test_google.py`, create.

**Interfaces:** Produce `gateway.app(token: str, *, transport=None) -> Starlette`, with `transport` passed to `Upstream` for offline tests. Produce `seed.seed(directory: Path, email: str, client_id: str, client_secret: str, refresh_token: str) -> Path` and `workspace.register_send_draft(server, credential_path: Path) -> None`. `workspace.main() -> None` installs D6's post-filter registration hook and calls pinned `main.main()`. The registration function reads the seeded grant once, validates all required fields before creating `gmail_mcp.Auth`, and registers only `gmail_send_draft(draft_id: str, user_google_email: str | None = None) -> str` on the supplied FastMCP server, ignoring `user_google_email`. The raw entrypoint derives the credential path from `WORKSPACE_MCP_CREDENTIALS_DIR` and `USER_GOOGLE_EMAIL` using the seed filename rule. Executable entrypoints read environment variables, not secret-valued CLI arguments. Consume unchanged `mcp_hub.build_app(clients, modules=(), ups=...)`, `Upstream` and legacy `gmail_mcp` from `files/`.

- [ ] **Step 1: Write failing gateway tests.** `test_direct_auth_all_methods` asserts missing, wrong, malformed, non-ASCII, synthetic `homelab.`-prefixed, `work.`-prefixed and legacy-prefixed bearers return 401 on GET/POST/DELETE `/mcp` without reaching a fake raw backend. All credentials are made-up fixtures. A valid independent fixture bearer initializes, lists and calls synthetic read tools. `/healthz` returns 200 without auth; no legacy hub routes exist. Assert the raw backend receives no incoming bearer or cookie, while MCP session/protocol headers and JSON/SSE responses survive. Define `raw_requests` as the list of requests the fake raw backend received and `response` as the unauthorized gateway response.
- [ ] **Step 2: Add seed tests.** `test_seed_and_restart_contract` asserts a mode-0600 file at `directory / (quote(email, safe="@._-") + ".json")`, D5's exact `complete_scope_set`, a fresh refresh-token field, and no output containing fixture credentials or identity. Missing inputs or an unwritable directory fail before launching the server. Simulate Workspace refresh rewriting the file, then a new empty volume seeded from the independently staged grant. Define `credential_path` as the seeded fixture file and `credential_json` as its decoded JSON.
- [ ] **Step 3: Add rendering tests.** `test_homelab_only_render` initially renders a synthetic composition of the backend and existing base; Task 3 extends it to the real overlay. Assert raw loopback host/port, no raw `PORT` environment entry, `enableServiceLinks: false`, raw `HOME=/tmp`, writable `/tmp` and `/creds`, the raw mounts for `seed.py`, `workspace.py` and unchanged legacy `gmail_mcp.py`, and the hardening in Steps 10-12. Pinned `main.py` reads `PORT` before `WORKSPACE_MCP_PORT`; disabling service links and omitting `PORT` prevents it overriding 8001. Assert the gateway mounts only its required code and writable temp/dependency volumes, gets no Google grant/client environment variables, no grant Secret and no `/creds`. `GOOGLE_UPSTREAM_TOKEN` is its independent inbound bearer, not a Google credential. Define `service_ports` as the rendered Service's port numbers.

  Representative assertions for Steps 1-3:

  ```python
  assert response.status_code == 401
  assert raw_requests == []
  assert credential_path.stat().st_mode & 0o777 == 0o600
  assert set(credential_json["scopes"]) == set(complete_scope_set)
  assert service_ports == [8000]
  ```

- [ ] **Step 4: Add send-draft tests.** `test_send_draft_listed_and_sent` calls the tool through the real pinned `SecureFastMCP` server with `USER_GOOGLE_EMAIL` set to a synthetic value, OAuth 2.1 off, a synthetic seeded grant and mocked `gmail_mcp.urllib.request.urlopen`. Assert `gmail_send_draft` appears exactly once on `tools/list` with required string argument `draft_id`; calling it with only `draft_id` succeeds despite the server-injected `user_google_email` and sends exactly one `users.drafts.send` request with method POST, path `/gmail/v1/users/me/drafts/send`, JSON `{"id": "draft-fixture"}` and the fake refreshed access token. Assert the legacy JSON result's `sent`, `message_id` and `thread_id`. Reuse legacy `_send_draft` directly rather than duplicating its HTTP body/result mapping. Add host test `test_legacy_gmail_selftest` that runs unchanged `gmail_mcp._selftest()` with captured output and HTTP forbidden, so normal hub discovery also retains those existing checks. Define `send_result` as the decoded JSON result of that real-server call with mocked Google HTTP.

  ```python
  assert send_result["sent"] is True
  ```
- [ ] **Step 5: Add send-draft failure tests.** `test_send_draft_invalid_id_is_mcp_error` covers omitted, empty, whitespace-only and control-containing ids without HTTP, plus mocked Google 400/404 responses for a syntactically valid but nonexistent id. Assert an MCP validation error or `isError` tool result as appropriate, no response-body/credential leakage and no retry. `test_send_draft_missing_credentials_no_network` covers absent/unreadable JSON and missing/empty OAuth client or refresh fields before startup, and an unavailable auth object at call time. Assert network mocks are never invoked and no interactive auth is attempted. `test_send_draft_refresh_never_writes` expires the helper's cached access token and mocks a second refresh with the same refresh token; forbid all file mutations after seed, assert credential bytes and mtime stay unchanged, and assert only in-memory token state changes.
- [ ] **Step 6: Run the new unit tests to red.** Run `/tmp/hub-fixture-venv/bin/python -m unittest discover -s 2-k3s/23.mcp-hub/tests -p test_google.py -v`. Expected failure is the absent new module or manifest, not a network or dependency failure. Use the instance README's pinned fixture installation if needed; raw FastMCP tests run in the pinned-image mode defined in Step 15, not against the gateway SDK.
- [ ] **Step 7: Implement the gateway.** Validate a bearer with the searxng runner's length and character rules, then use its SHA-256 digest with `build_app`, `modules=()` and one `/mcp` `Upstream` to `http://127.0.0.1:8001/mcp`. Supply no credential to the raw server. Disable gateway access logs. There is no aggregation or JSON-RPC rewriting in this container.
- [ ] **Step 8: Implement the seed function.** Seed only the documented credential JSON into `/creds`. The seed entrypoint runs in the raw container, reads the existing Google environment names from legacy `workspace-mcp.yaml`, writes its file, and exits. Unset `GOOGLE_REFRESH_TOKEN` before exec of the raw launcher. OAuth client fields and the grant never enter the gateway container.
- [ ] **Step 9: Implement the raw startup extension.** Implement the two `workspace.py` interfaces and D6's post-filter hook, returning the original filter's return value after registration so `main.py` retains its `tools_removed` startup summary. Use an unambiguous alias for pinned `main` and leave `/app` as the upstream working directory; `/runner` supplies the launcher and `/legacy` supplies unchanged `gmail_mcp.py`. Validate the seeded credential fields before any HTTP and initialize only legacy `AUTH`. Register the tool with the raw server's decorator, keep legacy `Auth`, `_api` and `_send_draft` unchanged, and translate expected failures to sanitized FastMCP tool errors. Recheck auth availability before dispatch. The helper never writes credentials, starts another server, enrolls, adds approvals or retries a send. Use fixed startup error text too, because a credential path can contain the account identity.
- [ ] **Step 10: Define the raw Deployment container.** In `instances/google/workspace.yaml`, use `workspace-mcp`, one replica and `Recreate`, with the existing image pin. Set `WORKSPACE_MCP_HOST=127.0.0.1`, `WORKSPACE_MCP_PORT=8001`, `WORKSPACE_MCP_DISABLE_LOCAL_FILES=true`, `WORKSPACE_MCP_CREDENTIALS_DIR=/creds`, `HOME=/tmp` and no `PORT`. Run seed then exec `/app/.venv/bin/python /runner/workspace.py` with D5's exact CLI arguments. Raw alone consumes `mcp-hub-gmail`. Mount `seed.py` and `workspace.py` under `/runner`, and existing `mcp-hub-app` ConfigMap key `gmail_mcp.py` read-only at `/legacy/gmail_mcp.py`; set raw `PYTHONPATH=/app:/legacy` so the launcher imports upstream modules and only the reused legacy module, not the hub's entire module directory. Mount memory-backed `/creds` and writable `/tmp`. `HOME=/tmp` is intentional because upstream writes logs under `~/.google_workspace_mcp/logs` with a read-only root filesystem. Keep legacy raw resource requests/limits. Raw probes execute against loopback `/health`.
- [ ] **Step 11: Define the gateway container.** Use Python 3.13 Alpine with `mcp==2.2.0`, `/app` from existing `mcp-hub-app` and only `gateway.py` from the runner ConfigMap mounted at `/runner/gateway.py` using a read-only subPath. Mount writable temp/dependency directories as needed, never `/creds`, `seed.py`, `workspace.py` or the grant Secret. Read only `GOOGLE_UPSTREAM_TOKEN` from `mcp-hub-google-upstream/token`; no other container consumes that Secret. Keep searxng gateway resource budgets and `/healthz` probes.
- [ ] **Step 12: Apply pod hardening.** Both containers use non-root UID/GID 1000, no privilege escalation, dropped capabilities and read-only root filesystems. Set `automountServiceAccountToken: false` and `enableServiceLinks: false`. Readiness requires both containers.
- [ ] **Step 13: Define the Service.** In `instances/google/workspace.yaml`, expose only gateway 8000, with no ingress or exposed 8001.
- [ ] **Step 14: Generate runner configuration.** Add `instances/google/kustomization.yaml` with backend resources and a content-hashed runner ConfigMap containing `gateway.py`, `seed.py` and `workspace.py`; the shared application ConfigMap remains unchanged.
- [ ] **Step 15: Pin the actual pinned-image behavior.** Add `test_pinned_image_complete_inventory`, enabled by `WORKSPACE_MCP_IMAGE_TEST=1`. Start the exact pinned image with the synthetic grant, mounted extension and legacy module, and production raw launcher/D5 arguments under `--network none`. Use `docker exec` for initialization/list requests and raw FastMCP tests from Steps 4-5, so neither token refresh nor send can reach Google. Assert equality with the exact 31-name served union, including `gmail_send_draft`, every upstream write tool and no `start_google_auth`. Unknown new tools or a broken startup hook block rollout, including after an image bump. Remove all containers in cleanup. Define `served_tool_names` as the set returned by `tools/list` and `expected_tool_names` as D5/D6's exact 31-name set.

  ```python
  assert served_tool_names == expected_tool_names
  assert "start_google_auth" not in served_tool_names
  ```

- [ ] **Step 16: Require the image check in CI.** Add a required `.github/workflows/ci.yml` step after existing hub tests to run Step 15's image mode with `WORKSPACE_MCP_IMAGE_TEST=1`, not a silently skipped optional check.
- [ ] **Step 17: Run green unit tests.** Repeat Step 6. Expected all host unit tests pass; raw FastMCP tests are explicitly owned by required image mode.
- [ ] **Step 18: Run green image tests.** Run `WORKSPACE_MCP_IMAGE_TEST=1 /tmp/hub-fixture-venv/bin/python -m unittest discover -s 2-k3s/23.mcp-hub/tests -p test_google.py -v`. Expected raw tool tests and exact inventory pass with mocked Google HTTP and no external access. Missing Docker or an image pull failure is a blocker, not a pass.
- [ ] **Step 19: Review the backend diff.** The top-level session runs review-gate against the actual diff and test output. Do not merge or reference the backend from work.
- [ ] **Step 20: Verify current base ancestry.** Refresh `origin/main` and verify the branch is rebased onto it before committing. If rebasing changes the diff, repeat tests and review.
- [ ] **Step 21: Commit the backend only.** No co-author trailer.

  ```bash
  git add 2-k3s/23.mcp-hub/instances/google/gateway.py 2-k3s/23.mcp-hub/instances/google/seed.py 2-k3s/23.mcp-hub/instances/google/workspace.py 2-k3s/23.mcp-hub/instances/google/workspace.yaml 2-k3s/23.mcp-hub/instances/google/kustomization.yaml 2-k3s/23.mcp-hub/tests/test_google.py .github/workflows/ci.yml
  git commit -m "feat: add authenticated homelab Google upstream"
  ```

### Task 2: Add a fresh homelab OAuth target without changing legacy enrollment

**Files:**
- Modify: `2-k3s/23.mcp-hub/tools/bootstrap-gmail.py`
- Test: `2-k3s/23.mcp-hub/tests/test_bootstrap_gmail.py`, create.

**Interfaces:** Extend `_client(args: argparse.Namespace, namespace: str = "mcp-hub") -> tuple[str, str]` without changing default callers. Extend `_authorize(client_id, client_secret, *, instance=False) -> dict` without changing the default flow. Add `_write_instance_secret(context: str, data: dict[str, str]) -> None`, fixed to `mcp-hub-homelab/mcp-hub-gmail`. Produce CLI `bootstrap-gmail.py --instance homelab --context APPROVED_CONTEXT --client-json PRIVATE_DESKTOP_JSON`; selected-namespace `--reuse` may reuse only its OAuth client fields for a new consent, never its refresh token.

- [ ] **Step 1: Write failing tests.** `test_instance_requires_context_and_rejects_work` asserts argument failure before browser, network, Secret reads or writes for missing context or `--instance work`. `test_instance_never_reads_legacy_or_writes_sops` asserts the selected namespace on every kubectl call, exactly `client-id`, `client-secret`, `refresh-token`, `email` Secret data keys, no SOPS/output file operation, and no access token persistence. `test_legacy_default_unchanged` asserts the old namespace, original scope pair and encrypted-output path remain the default. `test_instance_complete_scopes` asserts the homelab authorization request and accepted response use exactly D5's 12-scope set, verified against pinned `auth/scopes.py` and the complete-tier decorators; no omitted, extra or automatically minimized scopes are accepted.
- [ ] **Step 2: Pin failed consent and concurrent-write cases.** `test_instance_consent_failures_write_nothing` covers denied consent, timeout, wrong state, missing refresh token, partial or broader-than-requested scopes and mismatch with an expected account entered privately before opening the browser. Assert no Secret mutation and no sensitive stdout/stderr. `test_instance_write_conflict_is_closed` asserts create-on-absent and resourceVersion-checked replacement-on-present, a conflict aborts without retrying over another grant, and no Secret output, HTTP response body, authorization URL or exception body reaches output. Include failed browser opening and wrong Desktop-client JSON cases. A separate favicon-then-valid-callback case must succeed exactly once; favicon-only traffic must still time out without a write.
  Define `secret_mutations` as mocked kubectl create/replace calls, `legacy_namespace_calls` as mocked calls targeting the legacy namespace, `fixture_refresh_token` as a made-up grant value, `stdout` and `stderr` as captured output, `replacement` as the proposed Secret document, `existing_resource_version` as its mocked prior version, and `secret_data` as decoded proposed Secret data. Representative assertions for Steps 1-2:

  ```python
  assert secret_mutations == []
  assert legacy_namespace_calls == []
  assert fixture_refresh_token not in stdout + stderr
  assert replacement["metadata"]["resourceVersion"] == existing_resource_version
  assert set(secret_data) == {"client-id", "client-secret", "refresh-token", "email"}
  ```

- [ ] **Step 3: Run red tests.** Run `/tmp/hub-fixture-venv/bin/python -m unittest discover -s 2-k3s/23.mcp-hub/tests -p test_bootstrap_gmail.py -v`. Expected new instance arguments or helpers are absent, while the legacy fixture still works.
- [ ] **Step 4: Implement the narrow instance branch.** Add `--instance` with only `homelab` accepted and require an explicit `--context`. In instance mode reject `--out` and inline client-id/client-secret arguments; accept a private Desktop-client JSON or selected-namespace `--reuse`. Use exactly D5's complete 12-scope set, PKCE S256, fresh state, `access_type=offline` and `prompt="select_account consent"`. Require the returned scope set to equal that set; previously approved broader scopes must not silently enter the new grant. Read the expected account with a non-echoing local prompt; compare `_profile` after exchange without displaying the account. Wait through irrelevant loopback requests until the bounded callback deadline. Abort on every failed checkpoint before writing. Open the URL directly in the owner's browser; do not print it for agent fallback. No browser in this environment means the owner runs the command on a workstation.
- [ ] **Step 5: Write the new grant directly.** `_write_instance_secret` sends the document on kubectl stdin with captured output, using create or resourceVersion-checked replace, never apply and never `--from-literal`. Read only the selected Secret as needed for replacement metadata; do not display it. Return a fixed success line `homelab Google grant staged; rollout still required`. Preserve the legacy encrypted-output branch. Secret staging neither restarts the server nor triggers account enrollment elsewhere.
- [ ] **Step 6: Run green bootstrap tests.** Repeat Step 3. Expected all pass with synthetic OAuth/network/kubectl fixtures, no real browser or cluster.
- [ ] **Step 7: Run the hub regression suite.** Run `/tmp/hub-fixture-venv/bin/python -m unittest discover -s 2-k3s/23.mcp-hub/tests -v`. Expected all pass, including the existing Notion bootstrap test.
- [ ] **Step 8: Review the consent diff.** The top-level session runs review-gate against the actual diff and test output. Do not run enrollment as an agent.
- [ ] **Step 9: Verify current base ancestry.** Refresh `origin/main` and verify the branch is rebased onto it before committing. Repeat tests and review if rebasing changes the diff.
- [ ] **Step 10: Commit the consent target only.**

  ```bash
  git add 2-k3s/23.mcp-hub/tools/bootstrap-gmail.py 2-k3s/23.mcp-hub/tests/test_bootstrap_gmail.py
  git commit -m "feat: add homelab Google consent target"
  ```

### Task 3: Wire homelab inventories and prove client isolation

**Files:**
- Modify: `2-k3s/23.mcp-hub/instances/homelab/kustomization.yaml`
- Modify: `2-k3s/23.mcp-hub/instances/README.md`
- Modify: `2-k3s/23.mcp-hub/README.md`
- Test: `2-k3s/23.mcp-hub/tests/test_instances.py`, modify.
- Test: `2-k3s/23.mcp-hub/tests/test_searxng.py`, modify.
- Test: `2-k3s/23.mcp-hub/tests/test_local_clients.py`, modify.
- Test: `2-k3s/23.mcp-hub/tests/test_google.py`, modify.
- Test: `2-k3s/13.t3code/env/tools/isolated-runtime.test.py`, modify.

The runtime StatefulSet revision belongs to Task 4 after staging, not this task.

**Interfaces:** Consume Tasks 1 and 2. Approved private homelab inventory is `{"environment":"homelab","services":["searxng","gmail","drive"]}` in both hub and runtime. Keep existing client digests and the searxng connection unchanged. Both added connections point to `http://workspace-mcp.mcp-hub-homelab.svc.cluster.local:8000/mcp` with the independently minted Workspace bearer. No Google grant or upstream bearer goes into client configuration.

- [ ] **Step 1: Add failing integration fixtures.** `test_google_homelab_registration_only` asserts both routes initialize and synthetic reads succeed through the real homelab hub and gateway, ordinary client credentials never reach the backend, wrong-environment and legacy tokens return 401, and the unchanged work inventory has neither route with its own authorized client. All other existing work services remain intact. Assert generated homelab Pi `mcpServers` has exactly `searxng`, `gmail`, `drive` with token environment references, and work has neither Google name. Extend existing Claude/Codex/OpenCode allowlist fixtures for the same inventories and stale managed-entry cleanup; no new helper code should be needed.
- [ ] **Step 2: Add runtime and render checks.** In `RuntimeTests`, add `test_google_inventory_digest_mismatch_stops_startup`: changing only `services.json` fails validation; recomputing its exact-byte digest allows the reviewed inventory, while other inputs, approvals and grant digests remain unchanged. Extend the installed-snapshot and Codex-requirements tests to include both Google routes. Extend `test_homelab_only_render` to assert only the homelab overlay contains `workspace-mcp`, its gateway Secret reference is `mcp-hub-google-upstream/token`, and only the raw container consumes `mcp-hub-gmail`. Work's rendered resources and revisions remain equal to their pre-change fixture. Update existing exact Deployment-set assertions from two to three for homelab only. Preserve search and Notion assertions.
  Define `homelab_config` and `work_config` as generated Pi configuration fixtures, `runtime_sha256` as the fixture runtime digest map, `stored_services_bytes` as its serialized services input, and `work_render_before` and `work_render_after` as the work overlay's baseline and proposed rendered resources. Representative assertions for Steps 1-2:

  ```python
  assert set(homelab_config["mcpServers"]) == {"searxng", "gmail", "drive"}
  assert not {"gmail", "drive"} & set(work_config["mcpServers"])
  assert runtime_sha256["services.json"] == hashlib.sha256(stored_services_bytes).hexdigest()
  assert work_render_after == work_render_before
  ```

- [ ] **Step 3: Run red hub tests.** Run `/tmp/hub-fixture-venv/bin/python -m unittest discover -s 2-k3s/23.mcp-hub/tests -v`. Expected Google render/routing fixtures fail before overlay inclusion.
- [ ] **Step 4: Run red runtime tests.** Run `python3 2-k3s/13.t3code/env/tools/isolated-runtime.test.py`. Expected Google fixtures expose any remaining integration gap; digest-negative cases already reject mismatches.
- [ ] **Step 5: Include `../google` only in the homelab overlay.** Do not edit `hub_services.py`, `hub_clients.py` or `sync-shared.sh` output merely to add already-known names.
- [ ] **Step 6: Document the contract in both hub READMEs.** Document fresh owner enrollment, the exact 31-tool served union and complete scope set, the shared tool-list aliases, loopback authentication boundary, one Google credentials-file writer, memory-only send-draft refresh, writable temporary credential file and re-consent on revoked grants. State that writes run directly by owner decision, including send-draft, trash and sharing, with no hub-side or client-side approval gates. The legacy OpenCode `ASK` table stays unchanged and is not extended. Distinguish the new instance path from the unchanged legacy Gmail module and Drive backend. Document separate hub and backend revision bumps for later grant/bearer rotations. Keep the inventory labeled pending until Task 4 succeeds.
- [ ] **Step 7: Run green hub tests.** Repeat Step 3. Expected all pass, including `test_notion_refresh_owns_only_each_instances_grant` and the legacy Gmail selftest.
- [ ] **Step 8: Run green runtime tests.** Repeat Step 4. Expected all pass.
- [ ] **Step 9: Verify existing client helpers.** Run `python3 2-k3s/23.mcp-hub/tools/hub_clients.py --selftest`. Expected pass without helper changes.
- [ ] **Step 10: Verify generated output is unchanged.** Run `bash 2-k3s/13.t3code/one/tools/sync-shared.sh --check`. Expected exit 0.
- [ ] **Step 11: Verify homelab rendering.** Run `kustomize build 2-k3s/23.mcp-hub/instances/homelab >/dev/null`. Expected exit 0 without decrypting Secrets.
- [ ] **Step 12: Verify work rendering.** Run `kustomize build 2-k3s/23.mcp-hub/instances/work >/dev/null`. Expected exit 0 without decrypting Secrets.
- [ ] **Step 13: Verify the extended pinned image.** Re-run Task 1 Step 18. Expected all raw tool and exact 31-tool inventory checks pass.
- [ ] **Step 14: Review the integration diff.** The top-level session runs review-gate against the actual diff and command output. Leave revision selection for approved private staging in Task 4. The owner must stage inputs before any branch commit is merged because hub automation can roll immediately.
- [ ] **Step 15: Verify current base ancestry.** Refresh `origin/main` and verify the branch is rebased onto it before committing. Repeat tests and review if rebasing changes the diff.
- [ ] **Step 16: Commit integration, but do not merge.**

  ```bash
  git add 2-k3s/23.mcp-hub/instances/homelab/kustomization.yaml 2-k3s/23.mcp-hub/tests/test_instances.py 2-k3s/23.mcp-hub/tests/test_searxng.py 2-k3s/23.mcp-hub/tests/test_local_clients.py 2-k3s/23.mcp-hub/tests/test_google.py 2-k3s/13.t3code/env/tools/isolated-runtime.test.py 2-k3s/23.mcp-hub/instances/README.md 2-k3s/23.mcp-hub/README.md
  git commit -m "feat: wire homelab Gmail and Drive routes"
  ```

### Task 4: Stage, approve and verify the stopped cutover

**Files:**
- Modify: `2-k3s/23.mcp-hub/instances/homelab/kustomization.yaml`
- Modify: `2-k3s/23.mcp-hub/instances/google/workspace.yaml`
- Modify: `2-k3s/13.t3code-homelab/statefulset.yaml`
- Modify: `2-k3s/23.mcp-hub/instances/README.md`, pending status only after verification.
- Test: `2-k3s/23.mcp-hub/tests/test_instances.py`, update revision fixtures if pinned.
- Test: `2-k3s/23.mcp-hub/tests/test_searxng.py`, update revision fixtures if pinned.
- Test: `2-k3s/23.mcp-hub/tests/test_local_clients.py`, update revision fixtures if pinned.
- Test: `2-k3s/13.t3code/env/tools/isolated-runtime.test.py`, update runtime render revision if pinned.

All Secret documents, backups, accounts, consent evidence and private provisioning sources remain outside Git.

**Interfaces:** Consume the inventory and Secret names from Task 3, the bootstrap CLI from Task 2 and the backend from Task 1. Use new opaque revisions, never secret-derived values. Read current tracked revisions at execution time; do not copy historical README revision numbers. Commands below are instructions for an approved future rollout, not authorization for this design agent to mutate the cluster.

- [ ] **Step 1: OWNER: Approve the exact account, private inputs, downtime and merge.** Q2 is settled by D5: complete Gmail and Drive tools, with no approval gates on tool execution. Check active sessions, coordinate with other operators and close homelab desktop sessions if present. Leave legacy sessions and work unchanged. Authorize owner enrollment separately from Secret staging. Verify the approved Google project has Gmail and Drive APIs, an appropriate published consent screen and a private Desktop OAuth client. Do not copy the legacy grant or read it to discover the account.
- [ ] **Step 2: OWNER: Stop the homelab runtime through the input-rotation procedure.** Follow `2-k3s/13.t3code/env/isolated-runtime.md#input-rotation` exactly: pause `app-of-apps` first, then `t3code-homelab`; wait for in-flight syncs to finish; scale StatefulSet `homelab` to zero. Keep both hub Applications automated. Verify both selected automation policies remain absent and the runtime pod is gone before staging inputs. Do not manually sync a paused runtime or force-delete a running runtime to roll it.

  Run `kubectl --context "$context" -n t3code-homelab wait --for=delete pod/homelab-0 --timeout=300s`. Expected deletion confirmed, or a read-only absence check confirms it was already absent. A timeout blocks staging. `context` is the privately approved cluster context, not a value copied from a public example.

- [ ] **Step 3: OWNER: Sign in and consent from the browser workstation.** Run `python3 2-k3s/23.mcp-hub/tools/bootstrap-gmail.py --instance homelab --context "$context" --client-json "$private_desktop_json"`. Enter the expected account privately, sign in manually, verify the displayed Google account before consenting, and approve exactly D5's complete 12-scope set. Expected `homelab Google grant staged; rollout still required`. No account, authorization URL, token or refresh token should be printed. Abort on an account mismatch. Retain approved private recovery material outside the repo; delete temporary client downloads through the owner's normal private workflow.
- [ ] **Step 4: OWNER: Stage the independent upstream bearer.** Use the approved private provisioning process, not `add-client.py` or `local-clients.py provision`. In the process constructing the Secret documents, generate `secrets.token_urlsafe(48)` in memory and pipe a Secret with exactly key `token` to `mcp-hub-homelab/mcp-hub-google-upstream` through kubectl stdin. Use create-on-absent; an existing Secret requires explicitly approved rotation, not silent replacement. Retain the new bearer only in the approved private source for Step 5, never public output or agent context.
- [ ] **Step 5: OWNER: Stage hub inputs.** Take a private mode-0600 backup. Preserve all ordinary client digests and the entire searxng connection, add only Task 3's two connections with Step 4's bearer, and replace `mcp-hub-instance` using its current resourceVersion with exactly its two required data keys. Capture and suppress kubectl output and failure bodies. Update the private source with identical approved inputs. Expected only the approved homelab resources change; work, legacy and Notion grants remain unchanged. Partial staging keeps consumers stopped until the approved change is completed or reversed.
- [ ] **Step 6: OWNER: Stage the stopped runtime allowlist and digest together.** Take a private mode-0600 backup. Recheck parent/child automation and pod absence. Replace only `services.json` with Task 3's exact inventory, recompute `runtime.json.sha256["services.json"]` over stored serialized bytes, and record #1944 approval in `runtime.json.approvals.hub`. Preserve every other input, digest, approval, `grantSha256` and all grants, vault and ordinary client Secrets. Validate complete proposed inputs with real `isolated-config.py validate PRIVATE_INPUTS PRIVATE_HOME homelab` and the runtime's approved grants in its required environment; capture diagnostics privately. Expected exit 0. Use a resourceVersion-tested update, abort on conflict, and update the private source. Do not use first provisioning or re-enroll vault accounts.
- [ ] **Step 7: Advance the homelab hub marker.** Change only `mcp-hub.epaflix.com/inputs-revision` in `2-k3s/23.mcp-hub/instances/homelab/kustomization.yaml` to a new opaque revision for staged hub inputs. Keep searxng/work revisions unchanged.
- [ ] **Step 8: Advance the Google backend marker.** Change the Deployment's `mcp-hub.epaflix.com/inputs-revision` in `2-k3s/23.mcp-hub/instances/google/workspace.yaml` to its own opaque revision for independently staged grant/bearer inputs. Keep tracked replicas at one.
- [ ] **Step 9: Advance the runtime marker.** Change only homelab `t3code.epaflix.com/inputs-grants-revision` in `2-k3s/13.t3code-homelab/statefulset.yaml` to the approved opaque runtime revision. Keep tracked replicas at one and all work values unchanged.
- [ ] **Step 10: Update pinned revision fixtures.** Update literal expectations only where pinned in Task 4's four listed test files. No production logic change.
- [ ] **Step 11: Verify hub tests.** Run Task 3 Step 7. Expected pass.
- [ ] **Step 12: Verify runtime tests.** Run Task 3 Step 8. Expected pass.
- [ ] **Step 13: Verify client helpers.** Run Task 3 Step 9. Expected pass.
- [ ] **Step 14: Verify generated output.** Run Task 3 Step 10. Expected exit 0.
- [ ] **Step 15: Verify homelab rendering.** Run Task 3 Step 11. Expected exit 0 and intended new pod-template markers.
- [ ] **Step 16: Verify work rendering.** Run Task 3 Step 12. Expected exit 0 and no work revision change.
- [ ] **Step 17: Verify the extended pinned image.** Run Task 1 Step 18. Expected exact 31-tool inventory and mocked send-draft tests pass.
- [ ] **Step 18: Review rollout-marker diffs.** The top-level session reviews actual diffs, rendered pod templates and command output through review-gate.
- [ ] **Step 19: Verify current base ancestry.** Refresh `origin/main` and verify the branch is rebased onto it. Repeat tests and review if rebasing changes the diff.
- [ ] **Step 20: Commit rollout markers, but do not merge.** Stage only changed paths from this exact list.

  ```bash
  git add 2-k3s/23.mcp-hub/instances/homelab/kustomization.yaml 2-k3s/23.mcp-hub/instances/google/workspace.yaml 2-k3s/13.t3code-homelab/statefulset.yaml 2-k3s/23.mcp-hub/tests/test_instances.py 2-k3s/23.mcp-hub/tests/test_searxng.py 2-k3s/23.mcp-hub/tests/test_local_clients.py 2-k3s/13.t3code/env/tools/isolated-runtime.test.py
  git commit -m "feat: advance homelab Google rollout markers"
  ```

- [ ] **Step 21: OWNER: Approve and merge.** Merge reviewed commits only after Steps 3-6 are staged and verified and Step 20's commit exists.
- [ ] **Step 22: Verify automatic backend rollout.** Run `kubectl --context "$context" -n mcp-hub-homelab rollout status deployment/workspace-mcp --timeout=300s`. Expected one ready replica and successful rollout. Inspect status/metadata only to confirm its intended annotation and merged revision.
- [ ] **Step 23: Verify automatic hub rollout.** Run `kubectl --context "$context" -n mcp-hub-homelab rollout status deployment/mcp-hub --timeout=300s`. Expected one ready replica and successful rollout. Inspect status/metadata only to confirm its intended annotation and merged revision.
- [ ] **Step 24: Prove wrong-hub, legacy-token and gateway rejection without reading credentials.** The agent uses only its own runtime token environment variable plus made-up synthetic wrong, legacy-prefixed and `work.`-prefixed tokens to prove the 401 responses. It never reads any Secret value or another client's token. Use a private HTTP client to test unauthorized hub access with synthetic tokens and direct gateway GET/POST/DELETE with no bearer, its own ordinary-client bearer and synthetic bearers. Expect 401 in every negative case. Print only statuses, never headers, tokens or bodies. Do not fetch work logs or Secrets. Any check that needs a real non-agent credential is an `OWNER:` step, including the independent gateway bearer or a real work/legacy token. If the agent's own environment is unavailable while the runtime is stopped, `OWNER:` performs pre-resume checks privately and the agent repeats its own checks after Step 27. Never recover a token from a Secret or another runtime.
- [ ] **Step 25: OWNER: Verify authorized hub initialization before resuming consumers.** Run owner-approved private Python tooling using `mcp.client.streamable_http.streamable_http_client`, `mcp.ClientSession.initialize()` and `list_tools()` through each alias. Any real non-agent credential stays inside the owner's process, never agent context or shell arguments. Expected each route exposes exactly the approved 31 tools. If checking a valid independent gateway bearer directly, this is also owner-only. Report only statuses and tool names. This is private rollout tooling, not a new public script.
- [ ] **Step 26: Prove raw listener confinement.** Probe port 8001 using the pod IP, not loopback. Expected connection failure; print status only. No credential is needed.
- [ ] **Step 27: OWNER: Resume through the parent sync only when revisions agree.** Keep runtime stopped until private inputs/digests, reviewed markers and published runtime image agree. Set `revision` to the approved merged commit, then run `argocd app sync app-of-apps --revision "$revision"` and `argocd app wait t3code-homelab --sync --health --timeout 1200` through the input-rotation procedure. Expected parent self-healing restored, child automated pruning restored without self-healing, and one runtime replica. Run `kubectl --context "$context" -n t3code-homelab wait --for=condition=Ready pod/homelab-0 --timeout=300s`. Expected Ready with the new input revision. Do not restart work.
- [ ] **Step 28: Verify generated homelab registration names.** Inspect only `mcpServers` key names from generated Pi `mcp.json`, not headers. Expected `searxng`, `gmail`, `drive`; Claude/Codex metadata and the installed private service snapshot agree. Use only the runtime's own existing token environment variable for subsequent MCP calls. Repeat Step 24 now if its agent checks were deferred.
- [ ] **Step 29: Verify the Gmail read.** Through generated `gmail`, call `ClientSession.call_tool("get_gmail_message_content", arguments)` for an owner-approved harmless message. Check the expected private fixture marker in-process and report non-error pass/fail only. IDs and content remain private.
- [ ] **Step 30: Verify the Drive read.** Through generated `drive`, call `ClientSession.call_tool("get_drive_file_content", arguments)` for an owner-approved harmless text file. Check the expected private fixture marker in-process and report non-error pass/fail only. No live write is required for either smoke test.
- [ ] **Step 31: Verify served tool inventories.** Confirm both aliases advertise exactly D5/D6's approved 31-tool union, including `gmail_send_draft` and all upstream writes, with `start_google_auth` absent. There is no extra agent-visible send-draft route.
- [ ] **Step 32: OWNER: Verify work exclusion if outside the homelab boundary.** Inspect only work's generated registration names and unchanged reviewed inventory, with no work output, token or Secret value entering agent context. Expected neither Google name is present; supply pass/fail only. Preserve both searxng routes and legacy availability.
- [ ] **Step 33: OWNER: Close the maintenance window after successful verification.** Do not resume stale desktop homelab configurations; every existing consumer needs a matching private allowlist. Desktop legacy migration remains separate A3 work. Approve the documentation-only pending-to-active change and retain sanitized pass/fail evidence for #1944.
- [ ] **Step 34: Update the approved inventory status.** Change pending to active only in `2-k3s/23.mcp-hub/instances/README.md` after Step 33.
- [ ] **Step 35: Review the status diff.** The top-level session runs review-gate against the documentation-only diff and verification evidence.
- [ ] **Step 36: Verify current base ancestry.** Refresh `origin/main` and verify the branch is rebased onto it before committing. Repeat review if rebasing changes the diff.
- [ ] **Step 37: Commit the verified inventory status.** Owner approval is required before merging this documentation change.

  ```bash
  git add 2-k3s/23.mcp-hub/instances/README.md
  git commit -m "docs: mark homelab Google inventory active"
  ```

**OWNER: Failure rule.** On any cutover failure, keep consumers stopped. If already resumed, pause parent and homelab runtime automation again and stop the runtime before reverting input authority. Restore previous complete private hub/runtime inputs and matching reviewed revisions through the same procedure. Do not revoke the Google application's permissions as rollback; that can affect legacy grants even when refresh-token values differ. Do not delete the legacy hub or its backends as part of this issue.

## Self-review and handoff

- S1: D4 is covered by Tasks 1-4. Independent bearer rejection, fresh owner consent, both inventory names, generated homelab registration, work exclusion and two real reads each have an explicit check. Spec D6's searxng requirement and Notion ownership are preserved, not redesigned. Plan D6's send-draft addition is covered by Task 1 and the 31-tool rollout checks.
- S2: Each implementation task has named tests, red/green commands, defined interfaces and a review checkpoint. The rollout is a separate owner-approved window; no step treats tests or rendered YAML as deployment authorization.
- S3: Credential names, namespace, complete scope set, exact 31-tool served union, two aliases and signatures are consistent across tasks. All five Review Focus conditions have named tests. Private staging failure and rollback also have explicit stopped-state rules.
- S4: No full implementation bodies, live values, extra services, generated helper churn or code comments are required. No live operations were needed to produce this plan. The pinned source was inspected; image behavior and end-to-end reads remain implementation/rollout checks, not claims of completed verification.

## Execution

Confirm the execution method with the owner before starting. Q2 is settled by D5; no further tool-scope or write-approval decision is pending.
