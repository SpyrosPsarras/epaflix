# Split the T3 pods: manager, homelab and work

Date: 2026-10-19. Status: approved in chat, D1 to D10; D11 is the
orchestrator's call, not objected to.

## Goal

Keep three T3 pods with separate jobs. The mixed pod becomes the manager.
It watches, restarts and fixes the homelab and work pods. Homelab work
belongs in the homelab pod. Work data stays outside the manager's model context.

Success means each daily-use pod has its approved services, desktop clients
have left the legacy hub, and the manager has only its recovery permissions
plus the accepted homelab exec access in D7.

## Current state

The owner verified these live facts before this design:

- C1: `t3code-0` runs in `t3code`, `homelab-0` in `t3code-homelab`,
  and `work-0` in `t3code-work`.
- C2: The mixed pod's home kubeconfig authenticates as `system:admin` in
  `system:masters`. Homelab has cluster-admin through `homelab-agent`.
  Its operating rules forbid access to `t3code-work` and `mcp-hub-work`.
- C3: The legacy `mcp-hub` in namespace `mcp-hub` provides Gmail, Drive,
  Notion, Kubernetes, Vaultwarden and searxng. Gmail is the built-in
  `files/gmail_mcp.py` module. Drive uses `workspace-mcp`, the same Google
  account and Secret `mcp-hub-gmail`.
- C4: Each new hub and each isolated pod's `mcp.json` lists only `searxng`.
  The new hubs forward to explicit authenticated upstreams. They cannot
  load the legacy built-in modules or reuse legacy upstream credentials.
- C5: The mixed pod's T3 container requests 1 CPU and 2Gi, with a 12Gi
  memory limit. The live sample was about 0.44 CPU and 0.9Gi. Its home PVC
  is 40Gi. Keeping this pod means it runs continuously.

The [mixed StatefulSet](../../../2-k3s/13.t3code/one/statefulset.yaml) has
one replica and `OnDelete` updates. The
[idle rollout job](../../../2-k3s/13.t3code/one/idle-rollout.yaml) replaces
an outdated pod after 30 idle minutes. It does not scale idle pods down.

The [legacy hub README](../../../2-k3s/23.mcp-hub/README.md) documents its
routes and write-tool approvals. Its old rollout note says the laptop was
not yet a client. The owner's current live inventory lists homePC, laptop
and t3code as legacy clients. Confirm desktop registrations at cutover.

Issue #1847, "Move daily use to the two new T3 pods", remains open.
Issue #1848, "Remove the mixed T3 pod and the legacy MCP hub", is blocked by it.

## D1. Keep the manager permanently

Keep the mixed pod and convert it into the manager instead of deleting it.
Its only job is to watch, restart and fix the homelab and work pods.
It is not a third daily-use environment.
"Fix" means diagnosing a broken pod and restarting it by deleting it; code
or manifest fixes go to the homelab pod by pull request.

## D2. Homelab owns homelab work

The homelab pod handles servarr, SSH to homelab hosts, homePC and the laptop,
and other cluster work. The manager does none of these jobs.

Keep the existing boundaries in the
[homelab access guide](../../../2-k3s/13.t3code-homelab/files/homelab-ssh.md).
Homelab's work-namespace exclusion remains an operating rule, not technical
isolation from its cluster-admin credential. It gains no work access.

## D3. No manager MCP services

Remove all hub registrations and hub credentials from the manager.
It gets no Gmail, Drive, Notion, Kubernetes, Vaultwarden or searxng service.
Do not leave a legacy route available through another agent configuration.
Kubernetes recovery uses the restricted ServiceAccount in D7, not MCP.

## D4. Gmail and Drive on the homelab hub

Add a separately authenticated Google Workspace MCP upstream in
`mcp-hub-homelab`. It provides both Gmail and Drive tools to that hub.
The homelab pod is the only T3 pod that receives these services.
Desktop access follows the local-client migration below.

Use a new upstream bearer and fresh Google OAuth consent. The owner signs
in through the browser and verifies the account before approving consent.
This is a human checkpoint. Copy no legacy token or refresh token.

The [instance contract](../../../2-k3s/23.mcp-hub/instances/README.md)
requires upstream authentication on direct requests. NetworkPolicy alone
is not sufficient. The legacy unauthenticated Drive backend is not a
suitable connection, and the new hub cannot instantiate the Gmail module.

Add the approved `gmail` and `drive` routes to the private homelab inventory
and runtime allowlist. Keep upstream credentials on the hub, not in clients.
The exact Gmail tool set and write limits remain builder choices below.

## D5. Notion on the work hub

Use `bootstrap-notion.py --instance work` with an explicit cluster context.
The owner verifies the approved work account and consents in the browser.
Account selection and consent are human checkpoints.

The work hub owns its own `mcp-hub-notion-grant`. Do not copy the legacy
grant or refresh token. Keep one refresh owner, as the instance contract
requires. Add `notion` to the private work hub and runtime inventories.
The work pod is the only T3 pod that receives this service.

## D6. Keep searxng on both hubs

Keep the existing searxng service on homelab and work. Preserve their
independent upstream credentials. The manager receives neither route.

## D7. Restrict manager Kubernetes access

Replace the home `system:admin` kubeconfig with a ServiceAccount credential.
Grant namespaced Roles in `t3code-homelab`, `t3code-work`,
`mcp-hub-homelab` and `mcp-hub-work` for recovery. In all four, allow
get/list/watch on pods, StatefulSets, Deployments and events, and delete on
pods. Grant no scale, patch, update or create permission, except `create`
on `pods/exec` in `t3code-homelab` and `mcp-hub-homelab`, which exec
requires.

Owner's decision: in `t3code-homelab` and `mcp-hub-homelab`, also allow pod
exec, including shell access, and log reads. The RBAC rules are `create`
on `pods/exec` and `get` on `pods/log`.
In `t3code-work` and `mcp-hub-work`, deny exec, logs, port-forward and Secret
reads. Their output can contain work data that must not reach the model.
Use status and recovery operations without fetching that output.
Remove the manager's own admin credentials, including the `system:admin`
kubeconfig and any other admin credential in its home or mounts. The only
remaining admin path is the accepted homelab exec access below.

Accepted risk: `homelab-0` holds a cluster-admin kubeconfig for ServiceAccount
`homelab-agent`, bound to `cluster-admin`, and runs a vault sidecar. The
`mcp-hub-homelab` pod holds the hub's upstream credentials. Exec there gives
the manager indirect cluster-admin and access to homelab credentials. The
owner accepts this risk so the manager can debug the homelab pod and hub.

The manager follows the homelab agent's operating rule: it must not use
credentials it finds inside homelab pods. It must never use them to act in
`t3code-work` or `mcp-hub-work`, or to read work data or work Secrets.

The manager has no Argo CD sync permission. Kubernetes RBAC cannot grant
sync without also granting edits to the Application source. All four
Applications already sync automatically. Allow only `get` on
`applications.argoproj.io` in `argocd`, limited by `resourceNames` to
`t3code-homelab`, `t3code-work`, `mcp-hub-homelab` and `mcp-hub-work`.
Grant no list or watch. Kubernetes RBAC `resourceNames` does not restrict a
plain list or watch.

## D8. Rewrite #1848

Change #1848 from deleting the mixed pod to converting it into the manager.
Remove the legacy hub only after homePC and the laptop use the new hubs.
Removal includes legacy `vaultwarden-mcp`, `kubernetes-mcp` and
`workspace-mcp`, their routes and their obsolete client configuration.
Keep the new pods' scoped vault clients unchanged.

## D9. Remove direct manager credentials

Remove the Vaultwarden login Secret mounted for the vault SSH agent, the
vault SSH agent itself, the GitHub SSH key and `GITHUB_TOKEN`/`GH_TOKEN`.
The manager holds no credentials to SSH, read secrets or push. Fixes by pull
request belong to the homelab pod. Keep CLIProxy model access so the
manager's agent can run.

## D10. Keep manager recovery outside its scope

The manager does not cover its own namespace `t3code`. The StatefulSet
controller recreates a crashed or deleted manager pod. Argo CD self-heal on
the `t3code` Application restores manifest drift. The owner handles anything
else.

## D11. Delete pods to restart them

Orchestrator's call; the owner did not object.

Restarting a pod means deleting it. The runtime StatefulSets use `OnDelete`.
The manager does not act on a pod whose Argo CD Application has automated
sync turned off. That means an input rotation or another owner operation is
in progress. See the
[stopped input rotation procedure](../../../2-k3s/13.t3code/env/isolated-runtime.md#input-rotation).

## Planned order

1. A1: Add Gmail and Drive to the homelab hub through D4.
2. A2: Add Notion to the work hub through D5.
3. A3: Move homePC and the laptop off legacy Gmail, Drive and Notion.
   Follow [local clients](../../../2-k3s/23.mcp-hub/instances/local-clients.md).
   Use separate machine and environment tokens. Remove legacy registrations
   deliberately; the local wrapper preserves them when adding new entries.
4. A4: Remove manager services and reduce its rights through D3, D7 and D9.
5. A5: Complete the #1847 daily-use period on homelab and work.
6. A6: Remove the legacy hub through the rewritten #1848.

For hub changes, stage approved private inputs before merge. Hub Applications
sync automatically with pruning and self-healing and can roll immediately.
Runtime allowlist changes need their own reviewed input revision through the
[stopped input rotation procedure](../../../2-k3s/13.t3code/env/isolated-runtime.md#input-rotation).
Pause parent and selected runtime automation as documented. Keep the runtime
stopped until inputs, digests and the published revision agree.

Verify each step before starting the next. Check authorized requests and
rejection of direct unauthenticated upstream calls and wrong-hub tokens.
Test manager recovery permissions and denied work access without reading
real work output or Secrets. Keep private evidence outside this public repo.

## Non-goals and anti-goals

- N1: Import no thread history into any pod.
- N2: Leave homelab/work boundary rules unchanged.

These outcomes are failures even if the services work:

- X1: The manager can read work pod output or work Secrets directly, or
  through any path other than the accepted D7 homelab exec.
- X2: A legacy token or refresh token enters a new namespace.
- X3: Homelab gains rights over `t3code-work`.
- X4: A public file names an employer or account identity.
- X5: The manager's home or mounts hold a credential that directly grants
  admin rights or work access. The accepted D7 homelab exec path is the only
  exception.
- X6: Using credentials found through the D7 homelab exec path. See the D7
  operating rule.

## Left to the builder

- Q1: How far should the manager's CPU, memory requests and PVC shrink?
- Q2: Which Google Workspace MCP Gmail tools are enabled? Should sending
  and trashing mail retain the legacy `/gmail` write-tool approval limits?
