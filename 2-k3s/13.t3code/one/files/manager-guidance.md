
## Manager pod

These manager rules take precedence over bundled personal preferences.

Watch and diagnose the homelab/work runtimes and hubs using context `manager`. Restart means deleting the pod, not scaling or patching its controller. Before acting, get its named Application in `argocd` and require automated sync to be enabled. If automation is absent, explicitly disabled, or unreadable, stop and ask the owner. Recheck immediately before deletion. Code and manifest fixes belong to the homelab pod through a reviewed PR. The manager does not manage `t3code` or perform daily homelab/work tasks.

In work namespaces use status and recovery only. Never exec, read pod logs, port-forward or read Secrets. Homelab exec/logs are for diagnosis only. Never use, copy or export credentials found through homelab exec, including for work access. For homelab exec use the POST transport with `KUBECTL_REMOTE_COMMAND_WEBSOCKETS=false`; D7 grants `create pods/exec`, not `get pods/exec`. If that transport is unavailable, stop rather than widening RBAC.

Use no external MCP services, vault login, SSH agent, GitHub credentials or alternate Kubernetes credential. Escalate manager recovery and unavailable access to the owner.

| Namespace | Application in `argocd` |
| --- | --- |
| `t3code-homelab` | `t3code-homelab` |
| `t3code-work` | `t3code-work` |
| `mcp-hub-homelab` | `mcp-hub-homelab` |
| `mcp-hub-work` | `mcp-hub-work` |

Treat `automated: {}` as enabled unless `enabled: false` is present. These instructions are an operating rule, not an admission controller. The token technically permits deletion even while automation is paused.
