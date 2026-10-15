# Isolated backend network policies

The homelab and work overlays default-deny ingress and egress. Runtime pods
can use CoreDNS in `kube-system` on UDP/TCP 53, the Kubernetes API, `cliproxy`
in `remote-pi` on TCP 8317, their own hub on TCP 8000, the vault HTTPS path
and public IPv4 destinations. Namespace and pod selectors are combined.

Public egress excludes RFC1918 space, including pod CIDR `10.42.0.0/16`,
Service CIDR `10.43.0.0/16`, node network `10.0.0.0/24` and LAN
`192.168.10.0/24`. Zero, loopback, link-local, shared address, multicast and
reserved high address ranges are also excluded. The public work AKS API uses
this rule; no work endpoint is published. There is no IPv6 allow rule.

Only homelab gets LAN SSH egress, on TCP 22 to the exact addresses in the
tracked generated `one/files/homelab-ssh.conf`. The render test compares address
sets for equality so missing and stale targets fail. Update both files together.

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

## Read-only evidence and accepted boundary

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

These are operator steps, not implementation-time cluster changes.

1. A1: Record health and discovery identity from the existing homePC and laptop
   SSH connections. Check active work, then manually sync the two applications.
   Policies affect existing pods immediately. Keep external administrative
   kubectl access available. If SSH or health drops, run
   `kubectl --context epaflix -n <ns> delete networkpolicy t3code-runtime default-deny t3code-control-plane`
   for the affected namespace, stop syncing and revert before another sync.
   Removing only `t3code-runtime` leaves the default deny and does not restore SSH.

   ```sh
   argocd app sync t3code-homelab
   argocd app wait t3code-homelab --sync --health --timeout 1200
   argocd app sync t3code-work
   argocd app wait t3code-work --sync --health --timeout 1200
   ```

2. A2: Require network refusal from each pod to Loki. Any HTTP response fails
   this check. The script resolves DNS before testing, so a DNS outage fails
   rather than passing as isolation. Repeat using the gateway's current numeric
   Service and pod addresses, and probe the foreign hub's Service and pod IP
   on 8000 with a bounded TCP connect. Obtain addresses through administrative
   kubectl. Use fresh connections because conntrack accepts established ones.

   ```sh
   for ns in t3code-homelab t3code-work; do
     kubectl --context epaflix -n "$ns" exec -i t3code-0 -c t3 -- python3 - <<'PY'
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
     kubectl --context epaflix -n "$ns" exec t3code-0 -c t3 -- \
       curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:3773/health
     kubectl --context epaflix -n "$ns" exec t3code-0 -c sshd -- \
       bash /scripts/sshd.sh check
   done
   ```

4. A4: Create a no-tool Pi model thread in each backend and require exactly
   `NETWORK_OK`. Invoke a harmless own-hub tool. With that backend's existing
   token, confirm foreign-hub initialization is rejected without printing the
   token or response body. From homelab, run `kubectl get namespaces` with
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
