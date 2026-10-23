# Two agent hubs on off-the-shelf parts

Date: 2026-10-21. Status: approved by the owner, D1 to D6 and C1 to C3.

## Intent

The owner wants less of our own code behind the MCP hubs. Each world, homelab
and work, gets one hub that holds what its agents need. Existing tools do
the work; we write glue only where no tool exists.

## Goals

- G1. Replace our gateway code with agentgateway, one per hub.
- G2. Replace our searxng module with mcp-searxng.
- G3. Agents in the pods reach Kubernetes only through kubernetes-mcp-server
  on their own hub. Pods lose kubeconfig, `kubectl`, `helm` and `argocd`.
- G4. homePC and the laptop use both hubs. Their agents use local logins
  first and the hubs as fallback.
- G5. Each hub exposes Prometheus metrics.

## Non-goals

- N1. Vaultwarden stays the vault for both worlds. `vault run`, the `bw`
  sidecar and the vault SSH agent stay unchanged in both pods.
- N2. SSH stays as it is. No Tailscale SSH, no new tailnet devices.
  PCs keep reaching pod sshd on the LAN addresses, or through the subnet
  router when away.
- N3. cliproxy, GitHub PATs and the manager pod (#1947) stay as designed.
- N4. No External Secrets Operator. Hub Secrets stay sops-encrypted.
- N5. No Headscale. The VPN assessment recommended hosted Tailscale over
  Headscale, whose guide lists Cloudflare as unsupported. See option O3 in
  [vpn-alternatives.md][vpn-assessment].

[vpn-assessment]:
  ../../../2-k3s/18.cloudflare-tunnel/research/vpn-alternatives.md

## Anti-goals

- X1. New custom code where an existing tool does the job.
- X2. Scope beyond this list, or long new test suites. Add only the checks
  each change needs.
- X3. A homelab credential in the work hub, or the reverse.
- X4. Work pod losing AKS access before the work hub's Kubernetes path works.

## Decisions

- D1. Gateway: agentgateway (Linux Foundation, Apache 2.0). Static YAML
  config. Client keys as `keyHash: sha256:<hex>`. Upstream tokens from
  mounted files via `backendAuth.key.value.file`.
- D2. Homelab hub serves: searxng (mcp-searxng), gmail and drive
  (google_workspace_mcp plus our `gmail_send_draft`), kubernetes
  (kubernetes-mcp-server, cluster-admin), argocd (argoproj-labs/mcp-for-argocd,
  for `sync_application`).
- D3. Work hub serves: searxng, notion (hosted), kubernetes for AKS dev
  once a non-personal AKS login exists.
- D4. Keep `gmail_send_draft`. It reuses
  `2-k3s/23.mcp-hub/files/gmail_mcp.py`, so that file stays.
- D5. Keep `2-k3s/23.mcp-hub/files/notion_grant.py` as the Notion token
  refresher. agentgateway OSS has token exchange, not refresh-token rotation.
- D6. Keep `2-k3s/23.mcp-hub/tools/hub_clients.py` and
  `2-k3s/13.t3code/one/files/hub_clients.py`. They write Pi, Claude and Codex
  client configs. Their imports stay too:
  `2-k3s/23.mcp-hub/tools/hub_services.py` and
  `2-k3s/13.t3code/one/files/hub_services.py`.

## Orchestrator's calls, approved by the owner

- C1. Each hub is one pod: agentgateway plus each MCP server as a sidecar
  listening on 127.0.0.1. No raw MCP port leaves the pod. This replaces the
  contract rule that every upstream authenticates direct requests, because
  nothing outside the pod can reach them. One pod restart takes all tools
  down together; acceptable for this use.
- C2. `notion_grant.py` runs as a sidecar that writes the current access
  token to a shared file that agentgateway reads. Small change to existing
  code, also replacing its `UpstreamError` import from `upstream.py` so that
  `upstream.py` can go after #1848.
- C3. Skip ESO (N4).

## What gets deleted

With the new hubs:

- `2-k3s/23.mcp-hub/instances/searxng/searxng.py` after A3, once both hubs
  have moved.
- `2-k3s/23.mcp-hub/instances/google/gateway.py` after A1, since only the
  homelab hub uses it.

When each pod's Kubernetes path moves to its hub, A2 and A4:

- `2-k3s/13.t3code/env/files/work-aks-auth.py`
- `2-k3s/13.t3code-work/auth/work-aks-auth.py`
- In `2-k3s/13.t3code/env/files/isolated-config.py`, remove the homelab
  kubeconfig check at A2. At A4, remove the work check calling `work-aks-auth`
  and the shared kubeconfig parsing, input and approval list entries.
- Kubeconfig provisioning in `2-k3s/13.t3code-homelab/tools/inputs.py` at A2.

After #1848, since the legacy hub and the mixed pod use these until then:

- `2-k3s/23.mcp-hub/files/mcp_hub.py`
- `2-k3s/23.mcp-hub/files/hub_services.py`, used at runtime only by
  `2-k3s/23.mcp-hub/files/mcp_hub.py`. Update its tests with the deletion.
- `2-k3s/23.mcp-hub/files/upstream.py`
- `2-k3s/23.mcp-hub/files/searxng_mcp.py`
- `2-k3s/23.mcp-hub/files/vaultwarden_mcp.py`
- `2-k3s/13.t3code/one/files/vault-run.py`
- `2-k3s/13.t3code/one/files/vault-ssh-agent.py`
- `2-k3s/23.mcp-hub/kubernetes-mcp.yaml`
- `2-k3s/23.mcp-hub/workspace-mcp.yaml`
- `2-k3s/23.mcp-hub/vaultwarden-mcp.yaml`

## Order

1. A1. Homelab hub on agentgateway with searxng, gmail, drive. Replaces the
   gateway part of #1944; owner Google consent is unchanged.
2. A2. Add kubernetes and argocd to the homelab hub. Then remove the homelab
   pod's kubeconfig and CLIs, and update its agent instructions.
3. A3. Work hub on agentgateway with searxng and notion (#1945).
4. A4. Work kubernetes after the AKS login exists. Only then remove the work
   pod's kubeconfig and AKS helper (X4).
5. A5. Continue #1946, #1947, #1847, #1848 as written.

## Owner steps

- W1. Google consent (#1944) and Notion consent (#1945), as already planned.
- W2. Argo CD API token for mcp-for-argocd, stored in the homelab vault.
- W3. A non-personal AKS login for the work hub, arranged with the employer.
- W4. Runbooks that ran kubectl from the pods now run from homePC or the
  laptop.
- W5. Desktop agent instructions: local logins first, hubs as fallback.

## Left to the builder

- B1. Container images and pinned versions for mcp-searxng and mcp-for-argocd.
  Not checked.
- B2. ServiceAccount for kubernetes-mcp-server in `mcp-hub-homelab`; reuse
  the `homelab-agent` binding or add one cluster-admin binding.
- B3. kubernetes-mcp-server toolsets and `denied_resources`.
- B4. Rewrite the contract to match C1 in
  [`instances/README.md`](../../../2-k3s/23.mcp-hub/instances/README.md).
- B5. Remove leftovers that load or mention deleted files when each deletion
  lands. Check `2-k3s/23.mcp-hub/files/kubernetes-mcp.toml`, the hub selftests
  in `.github/workflows/ci.yml`, `2-k3s/23.mcp-hub/tests/test_searxng.py`,
  `2-k3s/23.mcp-hub/tests/test_google.py`,
  `2-k3s/23.mcp-hub/tests/test_instances.py`, and the `vault-ssh-agent.py`
  mention in `.github/renovate.json`.

## Unverified

- agentgateway, mcp-searxng, mcp-for-argocd: read from docs and source, not run.
- Whether kubernetes-mcp-server's Entra ID mode works behind agentgateway.
- Whether T3 handles MCP tool-only cluster access well for daily homelab work
  (no patch, port-forward, wait, `auth can-i` or `cp` tools).
