# Remove the legacy hub (#1848) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement Task 3 task-by-task. Tasks 1, 2, 4 and 5 are owner steps on the desktops, the cluster and third-party consoles; the agent prepares commands and checks, the owner runs them. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve #1848. Move both desktops and the one remaining script off the legacy hub, revoke what only the legacy hub used while it still runs (so the revocation can be seen to fail), then merge the removal and delete the `mcp-hub` namespace, its Application and its binding. The manager conversion half of #1848 is already done by #1947 (PR #2002).

**Architecture:** The legacy hub (`2-k3s/23.mcp-hub` root overlay, namespace `mcp-hub`, route `mcp.epaflix.com`) and the two instance hubs (`instances/homelab`, `instances/work`) share `files/` through `instances/base`. Removal deletes the root overlay, the three legacy upstreams, the legacy mode in the shared Python, and the Application manifest. The shared gateway code and the two instances stay. `app-of-apps` runs with `prune: false`, so the merge leaves the `mcp-hub` Application object in place. That Application itself has `prune: true` and tracks `main`, and after the merge its path renders nothing; Argo CD's default `allowEmpty: false` blocks an automated sync to an empty result, so the namespace resources stay until the owner deletes them. `app-mcp-hub.yaml` carries no resources finalizer, so deleting the Application orphans its resources; the owner deletes the namespace and the cluster-scoped binding by hand in Task 5. The order is therefore: PR reviewed and open (Task 3), revoke with the legacy pods still running (Task 4), merge and delete (Task 5).

**Tech Stack:** Kustomize, Argo CD, SOPS/KSOPS, Python stdlib and unittest, Traefik IngressRoute, Pi-hole dnsmasq.

**Spec:** `docs/superpowers/specs/2026-10-19-t3-pod-split-design.md` D8, D3, X2, X4. Issue: `gh issue view 1848`. Baseline: `origin/main` at `a0f9d82e`; planning branch `docs/1848-remove-legacy-hub-plan`.

## Global constraints

- D8: "Remove the legacy hub only after homePC and the laptop use the new hubs." Task 3 does not start until Task 1 is verified on both machines and #1847 records the end of the daily-use period.
- D8: "Keep the new pods' scoped vault clients unchanged." Those are the T3 pods' own Bitwarden sidecars from #1846, not the legacy `vaultwarden-mcp`.
- X2: no legacy token or refresh token moves to a new namespace or a new client config, and none is kept aside for testing. The negative probes in Task 4 use the legacy hub's own Secrets in place, through restarts, never through copied values.
- X4: the repo is public. The PR must not add account IDs, OAuth client IDs, token values or private endpoints. Hostnames already in Pi-hole config are deleted, not added.
- Work pod and work namespaces: status only. No step execs into a work pod, reads its logs or reads work data. `work-notion` is checked by listing its tools, not by searching.
- Ask the owner before merging, deleting any cluster resource, or revoking anything.
- Sol implements; Opus reviews; review-gate runs on the Task 3 PR and ran on this plan.

## Decisions the owner records before Task 1

| Code | Question | Options | Decision |
| --- | --- | --- | --- |
| D1 | Pi `searxng` on both desktops | `homelab-searxng`, `work-searxng`, or both | both (2026-10-22) |
| D2 | Pi `vaultwarden` | Bitwarden CLI or desktop app directly; or drop the entry | local `bw` first (2026-10-22). If local use fails during the Task 1 trial, the fallback is a new service on the instance hubs, not the legacy entry; that would be a new issue and would block Task 3 |
| D3 | Pi `kubernetes-epaflix` | `kubectl --context epaflix` from the shell; or drop the entry | local kubeconfig first (2026-10-22). Same fallback rule as D2 |
| D4 | Claude Code `~/.claude.json` and OpenCode `~/.config/opencode/opencode.json` on both desktops: `gmail`, `drive`, `notion`, `searxng`, `vaultwarden`, `kubernetes-epaflix` | switch each to the matching new hub entry, or drop it | drop all six (2026-10-22) |
| D5 | Revoke at the source: the Google OAuth client `mcp-hub` (B4); the dedicated Vaultwarden service login behind `mcp-hub-vaultwarden`; the legacy Notion integration whose grant lives in Secret `mcp-hub-notion-grant` in `mcp-hub` | yes per item, or leave. Only dedicated service credentials; the owner's own accounts stay untouched (issue criterion 3) | revoke all three (2026-10-22) |
| D6 | Delete the `mcp.epaflix.com` records in `1-proxmox/pihole` in the same PR, as commit `7c3416eb` did for the `t3code-ssh` records | yes or later | yes (2026-10-22) |
| D7 | `2-k3s/25.fytorium/minio-setup.sh` fetches its credentials through `2-k3s/13.t3code/one/files/vault-run.py`, which calls the legacy `/vault-secret` route. No new hub serves that route, and D8 forbids removing a route before its consumer moved | rewrite the script to Bitwarden CLI (`bw`) on the LAN PC in the Task 3 PR, or in a separate PR merged before Task 4, since Task 4 revokes the login the old script depends on. Either way delete `vault-run.py`, `vault-run.test.py` and their references, and the rewritten script must use the owner's own `bw` session, not the service login revoked in Task 4 | rewrite in the Task 3 PR (2026-10-22) |

The `kubernetes-mcp` ServiceAccount token and the `homepc`/`laptop` hub client tokens die with the namespace. They need no separate revocation.

## File map

| Files | Action |
| --- | --- |
| `2-k3s/11.argocd/apps/app-mcp-hub.yaml`; `apps/kustomization.yaml` line 19 | Delete the manifest and its entry. |
| `2-k3s/23.mcp-hub/{kustomization,namespace,deployment,service,ingress,kubernetes-mcp,vaultwarden-mcp,workspace-mcp,ksops-generator}.yaml`, `mcp-hub-gmail.enc.yaml`, `mcp-hub-vaultwarden.enc.yaml` | Delete. The root overlay is the legacy hub. |
| `2-k3s/23.mcp-hub/files/{clients.json,kubernetes-mcp.toml,vaultwarden_mcp.py}`, `files/vaultwarden-mcp/` | Delete. Legacy-only inputs. `files/kustomization.yaml` and the six shared modules stay; `instances/base` reads only those. |
| `2-k3s/23.mcp-hub/files/mcp_hub.py` | Remove legacy mode: the module docstring's legacy paragraph, `CLIENTS_FILE`, `load_clients`, the legacy `upstreams()` table (`/vaultwarden`, `/vault-secret`, `/kubernetes`), the legacy branch in `configured_app()` (line 361), the `add-client.py` hint in `build_app`'s error text (177), and in `_selftest` the `files/clients.json` load (216) and the `upstreams()`, `/vaultwarden`, `/vault-secret` and `VAULTWARDEN_HUB_SECRET` assertions (323-339). Keep `validate_clients` and `instance_app`. |
| `2-k3s/23.mcp-hub/tools/add-client.py`, `tools/sops_secret.py` | Delete both. `sops_secret.py` has no other importer. |
| `2-k3s/23.mcp-hub/tools/{bootstrap-gmail,bootstrap-notion}.py` | Remove the legacy path taken when `--instance` is absent; make `--instance` required. `bootstrap-gmail.py` line 211 keeps its `mcp-hub-gmail` instance-side name. |
| `2-k3s/23.mcp-hub/tools/{hub_clients,local-clients,hub_services}.py` | Keep. `hub_clients.py` lines 3 and 38: drop the `add-client.py` and the `kubernetes-mcp.yaml`/`workspace-mcp.yaml` pin references; line 35's `vault-secret` entry in `MANAGED` stays only if a test still needs the name, otherwise drop it. Then run `2-k3s/13.t3code/one/tools/sync-shared.sh` so the byte copy in `13.t3code/one/files/` follows. |
| `2-k3s/23.mcp-hub/tests/test_instances.py` | Drop `import vaultwarden_mcp` (32), `test_legacy_vault_and_command_time_secret_auth` (400), `test_legacy_render_without_decrypting_secrets` (661), the `apps["mcp-hub"]` assertion (694), the `vault-run.py` load (390) if D7 deletes it, and the `patch.object(mcp_hub, "upstreams"/"load_clients")` guards at 89, 162, 315, 322 and 461. Line 527 asserts bootstrap-notion's `-n mcp-hub` default namespace; it changes when `--instance` becomes required. Keep the `environment: legacy` rejection test (286). Add `test_no_legacy_mode`. |
| `2-k3s/23.mcp-hub/tests/test_bootstrap_gmail.py`, `test_google.py`, `test_local_clients.py` | Drop `test_legacy_*` (317, 327, 335) and `test_legacy_gmail_selftest` (211). In `test_local_clients.py` the `vaultwarden` entry at 273 is an unrelated-entry fixture asserted at 286; rename it to a neutral unrelated entry rather than deleting the assertion. |
| `2-k3s/13.t3code/one/tools/manager-runtime.test.py` line 119 | Stop reading `23.mcp-hub/files/clients.json`; assert the manager has no hub client another way or drop the assertion. |
| `2-k3s/23.mcp-hub/README.md`, `instances/README.md` lines 3-7, 63, 66, 264, 275 and 335, `instances/local-clients.md` line 10 | Rewrite `README.md` around the two instances; delete the `add-client t3code`, entrypoint hub-registration and legacy secrets table (lines 33 and 146-147 go with it). Drop "the legacy hub remains available", the `vault-run.py` "until #1848" sentences and the legacy comparisons. Line 63 names `vault-secret` as a rejected service name and line 335 lists it in the #1845 inventory history; reword both so the Step 4 grep stays clean. Keep `docs/adr/0001` as history. |
| `2-k3s/13.t3code/one/tools/sync-shared.sh` line 8 | Replace "MCP hub registration" in the ownership comment with what `entrypoint.sh` still owns. |
| `.github/workflows/ci.yml` lines 377-380 | Drop the `vaultwarden_mcp.py --selftest` line and the pin comment that names `deployment.yaml` and `vaultwarden-mcp.yaml`. |
| `.github/renovate.json` line 37 | Drop `2-k3s/23.mcp-hub/files/vaultwarden-mcp/package.json`. |
| `1-proxmox/pihole/dnsmasq.d/10-epaflix.conf` line 39, `unbound-no-aaaa-leak.conf` line 20 | Delete if D6 is yes. |
| D7 yes: `2-k3s/25.fytorium/minio-setup.sh`, `25.fytorium/README.md` lines 62 and 75, `2-k3s/13.t3code/one/files/vault-run.py`, `vault-run.test.py`, `.github/workflows/ci.yml` line 360 | Rewrite the script to Bitwarden CLI; delete the runner, its test and the CI line. `env/tools/inventory.sh` lines 25 and 37 stay: they assert the file is absent from the pods. |

Not touched: `instances/{base,homelab,work,searxng,google}` (the `workspace-mcp` name in `instances/google/workspace.yaml` is the new backend, not the legacy one), the six shared modules beyond the legacy-mode removal, the negative assertions in `tests/test_searxng.py` (41), `tests/test_google.py` (144), `tests/test_instances.py` (292, 327, 535, 618, 648) and `13.t3code/env/tools/isolated-runtime.test.py` (612, 620), which prove the new hubs reject legacy names and a fake in-cluster URL. The manager and the two pods. The `("env",)` loop named in the handoff is already gone from `test_instances.py`.

## Review focus

- RF1: `kubernetes-mcp.yaml` binds `cluster-admin` through ClusterRoleBinding `mcp-hub-kubernetes-mcp`. Deleting the namespace does not delete it. Task 5 deletes it by name and the check proves it is gone.
- RF2: `instances/base` consumes `../../files`. Deleting a shared file breaks both instance hubs. CI renders `instances/homelab` and `instances/work`; the PR must show both renders and the unittest run green.
- RF3: A desktop or the LAN PC keeps a legacy hub entry after Task 1. The Task 1 check greps every client config on both machines for both the external and the in-cluster hostname.
- RF4: A legacy token or refresh token is copied into a new hub client config or kept aside (X2). Task 1 uses `local-clients.py` for new hub entries; Task 4 probes through restarts of the legacy pods, never through copied values.
- RF5: The PR adds an account ID, client ID or token value (X4). Reviewers grep the diff for `apps.googleusercontent.com`, `@`, `token:` and 32+ character hex or base64 runs.

---

## Task 1: Move both desktops off the legacy hub

Owner steps per D1 to D4, on the laptop and homePC. The agent prepares the commands; the owner runs them.

**Files (not in git):** `~/.pi/profiles/epaflix/mcp.json`, `~/.config/mcp-hub/local-mcp.json`, `~/.claude.json`, `~/.config/opencode/opencode.json`, `~/.codex/config.toml` if present.

- [ ] **Step 1: Pi.** Run `python3 2-k3s/23.mcp-hub/tools/local-clients.py configure --context epaflix --instance <homelab|work> --config ~/.pi/profiles/epaflix/mcp.json` once per instance (D1: both). Each run rewrites every `<instance>-*` entry from that hub's allowlist, so `homelab-searxng` and `work-searxng` appear next to the existing `homelab-gmail`, `homelab-drive` and `work-notion`, using the `MCP_HUB_<INSTANCE>_TOKEN` grant the machine already holds from #1946. Then delete the old `searxng`, `vaultwarden` and `kubernetes-epaflix` entries (D2, D3: local `bw` and kubeconfig). Do not copy the old token anywhere. The 48 hours of Task 2 double as the D2/D3 trial: if local `bw` or `kubectl` fails the owner for a real task, stop and open the fallback issue before Task 3.
- [ ] **Step 2: Claude Code and OpenCode.** Per D4, edit the `mcpServers` and `mcp` maps by hand or with a `wizard`-generated script. Entries that switch point at the matching new hub entry's URL and header; entries that drop are removed.
- [ ] **Step 3: Verify on each machine.**

  ```sh
  for f in ~/.pi/profiles/epaflix/mcp.json ~/.config/mcp-hub/local-mcp.json ~/.claude.json ~/.config/opencode/opencode.json ~/.codex/config.toml; do
    [ -e "$f" ] && grep -lE 'mcp\.epaflix\.com|mcp-hub\.mcp-hub\.svc' "$f"
  done; echo done
  ```

  Expected: only `done`. Then in Pi: one `searxng` search through the D1 entry returns results; one `homelab-gmail` label list and one `homelab-drive` file list return results; `mcp({ server: "work-notion" })` lists tools. No Notion search.
- [ ] **Step 4: Record.** Comment on #1847 that the daily-use period ended on the date of Step 3, and on #1848 which decisions D1 to D7 were taken. Syncthing carries `mcp.json` to the other machine; confirm the other machine's copy matches before Step 3 there.

## Task 2: Prove the legacy hub has no consumer

- [ ] **Step 1:** 48 hours after Task 1, count requests to the legacy routes. The hub logs uvicorn access lines (`mcp_hub.py` runs with `log_level="info"`); the namespace is homelab-side, so reading them is allowed.

  ```sh
  kubectl --context epaflix -n mcp-hub logs deploy/mcp-hub --since=48h | grep -cE '"(GET|POST) /(searxng|gmail|drive|notion|vaultwarden|vault-secret|kubernetes)'
  ```

  Expected: `0`. Health probes and `/absent` 404s do not count. Any hit means a consumer was missed; find it before Task 3. A 48-hour window does not catch rare callers like `minio-setup.sh`; D7 covers that one, and the Task 3 Step 4 grep catches any other script in the repo.
- [ ] **Step 2:** `kubectl --context epaflix get networkpolicy,ingressroute -A | grep mcp-hub` lists only the `mcp-hub` namespace and the two instance namespaces. Nothing else routes to it.

## Task 3: Remove the legacy hub from the repo

Branch `feat/1848-remove-legacy-hub` from `main`. Implementer: Sol. Reviewer: Opus, through review-gate. One PR.

**Files:** the file map above.

- [ ] **Step 1: Write the failing checks first.** In `2-k3s/23.mcp-hub/tests/test_instances.py` add `test_no_legacy_mode`: `mcp_hub` has no attribute `load_clients`, `upstreams` or `CLIENTS_FILE`, and `mcp_hub.configured_app` raises the `HUB_INSTANCE` `ValueError` when the three instance env vars are unset. Run `python3 -m unittest discover -s 2-k3s/23.mcp-hub/tests -p test_instances.py`. Expected: the new test fails, the rest pass.
- [ ] **Step 2: Delete the manifests.** Remove the files in the first three rows of the file map and the `app-mcp-hub.yaml` kustomization entry. Run `kustomize build 2-k3s/11.argocd/apps | grep -c 'name: mcp-hub$'`. Expected: `0`. Run `kustomize build 2-k3s/23.mcp-hub/instances/homelab >/dev/null && kustomize build 2-k3s/23.mcp-hub/instances/work >/dev/null`. Expected: both succeed, proving RF2.
- [ ] **Step 3: Strip legacy mode from the Python and the tests.** `mcp_hub.py`, the two bootstraps, `add-client.py`, `sops_secret.py`, `hub_clients.py`, `manager-runtime.test.py`, and the test rows of the file map. Run `2-k3s/13.t3code/one/tools/sync-shared.sh` then `sync-shared.sh --check`. Expected: no drift. Run `python3 -m unittest discover -s 2-k3s/23.mcp-hub/tests -v`, `python3 2-k3s/23.mcp-hub/files/mcp_hub.py --selftest`, `python3 2-k3s/23.mcp-hub/tools/hub_clients.py --selftest` and `python3 2-k3s/13.t3code/one/tools/manager-runtime.test.py`. Expected: all pass, including Step 1's test.
- [ ] **Step 4: Docs, CI, Renovate, DNS, fytorium.** Per the file map and D6, D7. The rewritten `minio-setup.sh` passes `bash -n` and the owner runs it once against fytorium with `bw` before the PR is marked ready. Grep the tree:

  ```sh
  rg -n 'add-client|clients\.json|vaultwarden-mcp|kubernetes-mcp|vault-secret|sops_secret|mcp-hub-vaultwarden|mcp\.epaflix\.com|mcp-hub\.mcp-hub\.svc' --glob '!docs/superpowers/**' --glob '!**/adr/**'
  ```

  Expected: every remaining hit is one of the negative assertions listed under "Not touched", or `hub_clients.py` `MANAGED` if kept. The PR description lists each surviving hit with its reason.
- [ ] **Step 5: Commit and gate.** Commit message `feat(mcp-hub): remove the legacy hub (#1848)`. Capture the outputs of Steps 2 to 4 verbatim for the reviewers. Run review-gate with the Spec axis fed #1848's body and this plan's file map, and the Standards axis fed RF1 to RF5 as failure conditions. Open the PR only on a clean pass. CI must be green, including the hub unittest job and the job that lost line 360. Do not merge: the merge is Task 5 Step 2, after Task 4.

## Task 4: Revoke while the legacy hub still runs

Owner steps per D5, with the Task 3 PR open but not merged, so the legacy pods still run with their Secrets in place. Each probe makes the legacy pod use its own credential once and prints only an HTTP status or a readiness flag. No value leaves the pod. Confirm with the owner before each revocation and each probe.

- [ ] **Step 1: Google.** The `workspace-mcp` pod seeds its credential file at start from Secret `mcp-hub-gmail` and only calls Google on use, so the probe is a token refresh from inside the pod. Save it as `probe-google.sh`:

  ```sh
  kubectl --context epaflix -n mcp-hub exec deploy/workspace-mcp -- /app/.venv/bin/python -c '
  import glob, json, os, urllib.error, urllib.parse, urllib.request
  c = json.load(open(glob.glob(os.environ["WORKSPACE_MCP_CREDENTIALS_DIR"] + "/*.json")[0]))
  data = urllib.parse.urlencode({"client_id": c["client_id"], "client_secret": c["client_secret"],
                                 "refresh_token": c["refresh_token"], "grant_type": "refresh_token"}).encode()
  try:
      print(urllib.request.urlopen("https://oauth2.googleapis.com/token", data).status)
  except urllib.error.HTTPError as e:
      print(e.code)
  '
  ```

  Run it once before the revocation. Expected: `200`, which proves the probe reaches Google. Then in the Google Cloud console delete the OAuth client `mcp-hub`, keep `homelab-mcp`, and run it again. Expected: `400` or `401`, and the Credentials page lists `homelab-mcp` only. Then one `homelab-gmail` and one `homelab-drive` read from Pi still work.
- [ ] **Step 2: Vaultwarden.** Delete the dedicated service login the legacy `vaultwarden-mcp` used. Then `kubectl --context epaflix -n mcp-hub rollout restart deploy/vaultwarden-mcp`. Expected: after 60 seconds the pod is not Ready (its readiness probe needs `"status":"unlocked"`), and `kubectl -n t3code-homelab get pod homelab-0 -o jsonpath='{.status.containerStatuses[?(@.name=="vault")].ready}'` prints `true`, so the homelab pod's own sidecar is unaffected (D8). The work pod is checked by status only: `kubectl -n t3code-work get pod work-0` shows `Running`.
- [ ] **Step 3: Notion.** Remove the legacy integration from the workspace's connections page. The hub reads the grant from Secret `mcp-hub-notion-grant` through the Kubernetes API on use, not from env, so there is no in-pod probe that avoids touching the grant by hand. Criterion 4 is not verified for Notion by this plan; the owner accepts Notion's own behaviour (a removed integration's tokens are rejected) and the connections page no longer listing it as the evidence. Then `mcp({ server: "work-notion" })` in Pi still lists tools, since the work hub uses its own grant (#1945).
- [ ] **Step 4: Record.** Comment on #1848 which of the three were revoked, with the pod status lines, no log content.

## Task 5: Merge, then delete the live Application, namespace and the orphaned binding

Owner steps, after Task 4. Confirm with the owner before the merge and before each delete.

- [ ] **Step 1: Baseline.** `kubectl --context epaflix -n t3code get pod t3code-0 -o custom-columns=NAME:.metadata.name,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount`. Record the restart count.
- [ ] **Step 2: Merge, then delete.** The owner merges the Task 3 PR (and the D7 PR first, if separate). After `app-of-apps` has synced, check that nothing was removed by itself:

  ```sh
  kubectl --context epaflix -n argocd get application mcp-hub -o custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,PHASE:.status.operationState.phase
  kubectl --context epaflix -n mcp-hub get pods
  ```

  Expected: the Application is still there (`app-of-apps` does not prune) and reports `OutOfSync` or `Unknown` with no running operation, and the legacy pods are still listed, because Argo CD refuses an automated sync to an empty result (`allowEmpty` defaults to false). If the pods are already gone, stop and tell the owner before continuing. Then:

  ```sh
  kubectl --context epaflix -n argocd delete application mcp-hub
  kubectl --context epaflix delete clusterrolebinding mcp-hub-kubernetes-mcp
  kubectl --context epaflix delete namespace mcp-hub
  ```

  With no finalizer the first command orphans the resources; the next two remove them.
- [ ] **Step 3: Verify.**

  ```sh
  kubectl --context epaflix get namespace mcp-hub; kubectl --context epaflix get clusterrolebinding mcp-hub-kubernetes-mcp; kubectl --context epaflix -n argocd get application mcp-hub
  kubectl --context epaflix -n argocd get application mcp-hub-homelab mcp-hub-work t3code t3code-homelab t3code-work -o custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status
  kubectl --context epaflix -n t3code get pod t3code-0 -o custom-columns=NAME:.metadata.name,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount
  kubectl --context epaflix -n t3code get pvc home-spyros -o custom-columns=NAME:.metadata.name,STATUS:.status.phase
  kubectl --context epaflix auth can-i delete pods -n t3code-homelab --as=system:serviceaccount:t3code:manager-agent
  kubectl --context epaflix auth can-i delete pods -n t3code-work --as=system:serviceaccount:t3code:manager-agent
  kubectl --context epaflix auth can-i create pods --subresource=exec -n t3code-work --as=system:serviceaccount:t3code:manager-agent
  kubectl --context epaflix -n t3code-homelab get pod homelab-0 -o jsonpath='{.status.containerStatuses[?(@.name=="vault")].ready}{"\n"}'
  kubectl --context epaflix -n t3code-work get pod work-0
  curl -sS -o /dev/null -w '%{http_code}\n' https://mcp.epaflix.com/searxng
  ```

  Expected: three `NotFound`; all five Applications `Synced` and `Healthy`; `t3code-0` ready with the Step 1 restart count; `home-spyros` `Bound`; `yes`, `yes`, `no`; `true`; `work-0` `Running`; and the curl returns 404 or a connection error, so the route the legacy client tokens were issued for no longer exists. From Pi on the laptop: one `searxng`, one `homelab-gmail` and one `homelab-drive` read work, and `mcp({ server: "work-notion" })` lists tools.
- [ ] **Step 4: DNS.** If D6 is yes, apply the Pi-hole change the way `1-proxmox/pihole/README.md` describes, then `dig +short mcp.epaflix.com @<pihole>` returns nothing.
- [ ] **Step 5: Close #1848.** Tick all four criteria: hub removed (Tasks 3 and 5), manager running and `home-spyros` kept (#1947 and Step 3), credentials revoked after approval and seen to fail for Google and Vaultwarden, Notion not probed (Task 4), both pods and the manager still work (Step 3). Then remind the owner of C1 to C3 from the handoff: the rotation backups and `mcp.json.bak-1946`, the laptop WIP patch and `stash@{0}`, and the `git clean` losses.

## Rollback

- Before Task 5 Step 2 nothing live has changed except the desktops (Task 1) and the revoked credentials (Task 4). Closing the PR unmerged is the rollback for the repo.
- After Task 4: the deleted OAuth client, service login and integration cannot come back; a fresh grant through `bootstrap-gmail.py`/`bootstrap-notion.py` would be the only way, and there is no reason to want it.
- After the merge, before the namespace is deleted: `git revert` the merge; Argo re-renders the overlay and the still-present resources go back to `Synced`. If the Application was already deleted, `app-of-apps` recreates it from the reverted manifest and it adopts them. After the namespace is gone, recreating the hub needs the encrypted Secrets from git history plus the owner's SOPS key and the revoked grants, so there is no rollback; the desktops' Task 1 configs are the only consumer-side change and revert by hand.
