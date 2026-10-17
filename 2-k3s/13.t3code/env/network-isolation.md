# Isolated backend network policies

The homelab backend is replacing the legacy mixed backend. Read-only inspection
of the mixed namespace `t3code` found no NetworkPolicies, and its kubeconfig
answered `yes` to `kubectl auth can-i '*' '*' --all-namespaces`. Homelab now
uses the same cluster-admin access and broad runtime egress. Work keeps the
restricted policy introduced in #1851.

Both overlays default-deny ingress and egress. Every pod can use CoreDNS in
`kube-system` on UDP/TCP 53 and the Kubernetes API on the three master IPs,
TCP 6443. Idle-rollout keeps only these grants.

Homelab runtime pods can reach pods in every namespace except `t3code-work`
and `mcp-hub-work`, using a namespace-selector `NotIn` expression without a
port restriction. A separate IPv4 rule permits LAN, node, Tailscale and public
destinations, excluding pod CIDR `10.42.0.0/16` and Service CIDR `10.43.0.0/16`
so it cannot bypass namespace selection for direct cluster destinations.
This permits SSH to every inventory host, the Argo CD server and homelab
observability. The exact-inventory SSH IP list is no longer needed.

The #1851 Loki, private-network and nonessential cluster-service restrictions
no longer apply to homelab. Direct work-namespace pod traffic remains excluded.
The [mounted homelab guide](../../13.t3code-homelab/files/homelab-ssh.md) is the
authoritative operating rule for work resources, the legacy pod's home, node
volume paths, Azure/Entra logins and work credentials. Cluster-admin, SSH,
node traffic and shared proxies can bypass NetworkPolicy. These are policy
boundaries, not technical isolation. Do not query work logs through shared
observability or use a proxy to reach work resources.

Work runtime pods can reach `cliproxy` in `remote-pi` on TCP 8317, their own
hub on TCP 8000, Traefik's vault HTTPS backend on TCP 8443 and public IPv4
destinations. Namespace and pod selectors are combined. Its public rule
excludes RFC1918, zero, loopback, link-local, shared-address, multicast and
reserved high address ranges. The public work AKS API uses this rule.
No work endpoint is published. Neither overlay adds an IPv6 allow rule.
Public IPv4 egress has no port restriction and permits T3 Connect HTTPS
and Cloudflare tunnel TCP/UDP 7844.

TCP 2222 accepts ingress from any IPv4 source; direct ports 3773 and 8087 stay
closed. Both SSH LoadBalancers use `externalTrafficPolicy: Cluster`. Owner LAN
traffic crossing from the kube-vip holder on master-51 to the runtime on
worker-61 arrives masqueraded as `10.42.0.0`. The userspace Tailscale subnet
router also originates traffic in the pod range, so excluding that range cuts
owner SSH. Dedicated per-machine SSH keys and sshd's forwarding restriction to
3773 remain the control. Same-pod loopback needs no rule. Standard NetworkPolicy
does not isolate kubelet/node traffic, including the API server's exec path.

DNS and API rules cover every pod so idle-rollout can use Kubernetes exec
without a direct runtime HTTP connection. Image-prepull runs `true`; node image
pulls do not use pod egress. No mixed-backend or observability policy is changed.

## Historical #1851 evidence and accepted boundary

The evidence below records the original restricted policies. It is not a
claim that homelab still blocks observability or non-work services. Work
retains those restrictions. Recheck live enforcement after the merged sync.

On 2026-10-15, an instant Loki query from each `t3` container reached the gateway:
`sum(count_over_time({namespace="remote-pi", container="transcripts"}[5m]))`
with `X-Scope-OrgID: 1`. Homelab returned count 50 and work 51 in separate query
windows. No log content was requested or printed. Without the tenant header,
HTTP 401 means missing tenant selection, not network isolation.

Both relevant nodes had Flannel VXLAN and active kube-router policy chains.
The existing Vaultwarden ingress policy rejected direct TCP 8080 connections
from both pods, confirming enforcement. Source-node NAT translates
`192.168.10.102:443` to Traefik TCP 8443 before policy filtering, so the allow
selects that backend port. Kubernetes Service `10.43.0.1:443` similarly becomes
`10.0.0.51`, `.52` or `.53` on 6443. Recheck after CNI, kube-proxy or endpoint changes.

R3: The internal HTTPS listener also exposes both environment hubs, the mixed
`mcp-hub` route and `traefik-system/t3code-https`, which forwards to the mixed
T3 backend on 3773, plus other configured internal apps. NetworkPolicy cannot
filter TLS hostnames or Host headers. Pod traffic to `.102` is masqueraded,
so a Traefik ipAllowList cannot distinguish those sources either. Foreign-hub
access therefore relies on application-layer token rejection, as accepted in
review. Baseline probes from both pods reached the work hub and returned HTTP
401. The spec line "In-cluster traffic to anything else is denied, including
... the other environment's hub" is not fully met at the network level.

Read-only inspection of live IngressRoute and Ingress resources confirmed
neither Loki nor Grafana uses the internal listener. Grafana uses
`websecure` on 443, with a `web` HTTP route on 80; this policy permits only
8443 to Traefik. No Loki ingress route was present.

Allowed internet, API, MCP and authenticated SSH access can relay traffic, so
these policies are not an exfiltration boundary. At baseline the work pod had
no vault sidecar; its deployment and acceptance remain separate.

## Post-merge checks

These are operator steps, not implementation-time cluster changes. Work-pod
checks must run from an authorized work or owner admin session, never from
the homelab agent.

1. A1: Record health and discovery identity from the existing homePC and laptop
   SSH connections before merging. Both runtime Applications sync automatically
   after merge, and policies affect existing pods immediately. Check active work
   and keep external administrative kubectl access available. If SSH or health
   drops, pause `app-of-apps` first, then the affected runtime Application,
   using the automation patches in
   [input rotation](isolated-runtime.md#input-rotation).
   Confirm both policies remain disabled and any in-flight sync has completed.
   Only then run
   `kubectl --context epaflix -n <ns> delete networkpolicy t3code-runtime default-deny t3code-control-plane`
   for the affected namespace. Removing only `t3code-runtime` leaves the default
   deny and does not restore SSH. Review and merge the policy rollback, then
   sync `app-of-apps` to that merged revision to restore parent and runtime
   automation, as in the input rotation procedure. Verify automatic sync and
   SSH health after the rollback.

   ```sh
   argocd app wait t3code-homelab --sync --health --timeout 1200
   argocd app wait t3code-work --sync --health --timeout 1200
   ```

2. A2: Require network refusal from the work pod to Loki. Any HTTP response
   fails this check. The script resolves DNS before testing, so a DNS outage
   fails rather than passing as isolation. Repeat using the gateway's current
   numeric Service and pod addresses. Homelab may reach Loki, Grafana, Argo CD
   and other non-work cluster services. Verify homelab namespace exclusion in
   the rendered policy and CNI rules without probing either work namespace.
   Use fresh connections because conntrack accepts established ones.

   ```sh
   for ns in t3code-work; do
     kubectl --context epaflix -n "$ns" exec -i "${ns#t3code-}-0" -c t3 -- python3 - <<'PY'
   import socket
   import urllib.error
   import urllib.parse
   import urllib.request
   host = 'loki-gateway.observability.svc.cluster.local'
   socket.getaddrinfo(host, 80, type=socket.SOCK_STREAM)
   query = 'sum(count_over_time({namespace="remote-pi", container="transcripts"}[5m]))'
   url = f'http://{host}/loki/api/v1/query?' + urllib.parse.urlencode({'query': query})
   request = urllib.request.Request(url, headers={'X-Scope-OrgID': '1'})
   opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
   try:
       opener.open(request, timeout=5).close()
   except urllib.error.HTTPError:
       raise SystemExit('FAIL: Loki returned HTTP')
   except (urllib.error.URLError, TimeoutError, OSError):
       print('PASS: Loki network connection refused or timed out')
   else:
       raise SystemExit('FAIL: Loki query still reachable')
   PY
   done
   ```

3. A3: Run these health and SSH checks. From both owner clients reconnect over
   LAN and Tailscale, retrieve `/.well-known/t3/environment` through the tunnel
   and compare the saved identity. Peer-pod connections to runtime HTTP and the
   vault API must fail; pod-origin SSH is allowed and must require an approved key.

   ```sh
   for ns in t3code-homelab t3code-work; do
     kubectl --context epaflix -n "$ns" exec "${ns#t3code-}-0" -c t3 -- \
       curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:3773/health
     kubectl --context epaflix -n "$ns" exec "${ns#t3code-}-0" -c sshd -- \
       bash /scripts/sshd.sh check
   done
   ```

4. A4: Create a no-tool Pi model thread in each backend and require exactly
   `NETWORK_OK`. Invoke a harmless own-hub tool without printing its token.
   Do not probe the work hub from homelab. From homelab, run
   `kubectl auth can-i '*' '*' --all-namespaces` and `kubectl get namespaces` with
   `KUBECONFIG=/run/t3-inputs/kubeconfig` and `ssh <name> true` for approved LAN
   targets. From work, perform a permitted inventory read in the already
   enrolled AKS context. Report pass/fail only, not endpoints or authentication
   output. Record unavailable targets as incomplete checks.

5. A5: Once each backend has its own accepted vault sidecar, use an authorized
   private operator session to create a synthetic item in its approved org and
   collection. Check a password, field and attachment with `vault run` and a
   child that returns only an exit code. Permanently delete the item afterwards.
   Do not use owner items, print values or names/IDs, inspect Secret data or copy
   grants. Record missing sidecars as blocked checks.

6. A6: In each backend's existing checkout run `git ls-remote origin HEAD
   >/dev/null` and `gh api user --silent`. Check the next image update workflow
   and PreSync image-prepull succeed. When both backends are idle:

   ```sh
   for ns in t3code-homelab t3code-work; do
     kubectl --context epaflix -n "$ns" create job \
       --from=cronjob/t3code-idle-rollout t3code-netpol-acceptance
     kubectl --context epaflix -n "$ns" wait \
       --for=condition=Complete job/t3code-netpol-acceptance --timeout=300s
   done
   ```

   A no-pending-revision exit proves only API reads. During the next approved
   idle revision rollout, verify exec, pod deletion, startup and retained backend
   identity. Do not force a busy restart. Remove acceptance jobs after recording status.

7. A7: Through administrative Grafana, run a count-only Loki query for the fresh
   harmless model request. Confirm Alloy collection, ingestion, datasource health,
   Prometheus targets, normal alert delivery and the mixed backend's health.

## Offline verification

```sh
python3 2-k3s/13.t3code/env/tools/network-policy.test.py -v
kustomize build 2-k3s/13.t3code-homelab >/dev/null
kustomize build 2-k3s/13.t3code-work >/dev/null
```

Render tests check policy decisions, not live NAT, conntrack, node exemptions
or application authentication. Live acceptance remains required.

For the homelab replacement, the owner or an admin must first wait for the
runtime Application to sync the merged SHA and for an approved idle pod
replacement. The guide ConfigMap is hash-suffixed, so the existing pod keeps
the old guide. `OnDelete` does not replace it on sync. Confirm the replacement
is Ready and compare its `/scripts/homelab-ssh.md` with the merged guide before
syncing the access Application.

Only after confirming the new guide inside the pod may the owner or an admin
dry-run and sync `t3code-homelab-access` at the merged SHA with pruning. This
adds cluster-admin and removes obsolete read grants. The homelab agent cannot
patch Applications before this first grant. Follow the guide's resource-scoped
Kubernetes API patch, dry-run first, then run the README's `auth can-i` checks.
Neither the service-account token nor its kubeconfig input needs rotation.
Retiring the mixed pod or deleting its home requires a separate owner decision.
