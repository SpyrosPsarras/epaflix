# Local clients for both hubs

The routes `https://mcp-homelab.epaflix.com` and
`https://mcp-work.epaflix.com` use only Traefik's internal entrypoint and
Vaultwarden's TLS resolver, without Authentik redirects. LAN DNS suppresses
public AAAA fallback. Away from home, use the VPN and LAN resolver.

Pi server names have `homelab-` and `work-` prefixes. Registration replaces
only that instance's maintained entries and preserves unrelated servers,
including the legacy hub. T3 runtimes keep their own in-cluster hub addresses
and tokens.

## Stage client credentials

Run these commands from the repo root in the homelab administration
environment. Both namespaces need their existing `mcp-hub-instance` Secret.

```sh
for instance in homelab work; do
  for machine in homepc laptop; do
    python3 2-k3s/23.mcp-hub/tools/local-clients.py provision \
      --context epaflix --instance "$instance" --machine "$machine"
  done
done
```

Each namespace gets `mcp-hub-client-homepc` and `mcp-hub-client-laptop`, with
the token in key `token`. Only SHA-256 digests enter
`mcp-hub-instance`'s `credentials.json`. Provisioning reuses staged tokens on
retry, refuses a different existing grant, and preserves parallel writes
through resource-version checks and fresh reads.

Stage all four grants before merging the input revision. Both hub
Applications sync automatically with pruning and self-healing. Each overlay's
`mcp-hub.epaflix.com/inputs-revision: r2` loads its updated credentials during
the rollout after merge. Later input changes require another reviewed revision.

## Stage homePC configuration

Read the homelab SSH instructions first, then run from the repo root:

```sh
ssh homepc true
ssh homepc 'install -d -m 700 ~/.local/share/mcp-hub-client ~/.config/mcp-hub'
scp 2-k3s/23.mcp-hub/tools/local-clients.py \
  2-k3s/23.mcp-hub/tools/hub_clients.py \
  2-k3s/23.mcp-hub/tools/hub_services.py homepc:.local/share/mcp-hub-client/
ssh homepc 'for instance in homelab work; do
  python3 ~/.local/share/mcp-hub-client/local-clients.py configure \
    --context epaflix --instance "$instance" --config ~/.config/mcp-hub/local-mcp.json || exit
done'
```

The private staging config contains no token values. Each Pi entry's
`Authorization` is `!echo Bearer $(secret-tool lookup service mcp-hub key <instance>)`,
which Pi runs itself at connect time, so Pi needs no `MCP_HUB_*` export
however it is launched. `configure` reads only the approved service allowlist
and uses `hub_clients.py` to generate Pi entries, without downloading upstream
credentials. Repeat on the laptop by changing the SSH target. Activation
happens in O3 below.

## After review and merge

Stage required owner inputs and coordinate downtime before merge, because
automatic sync can roll each hub immediately. After merge, wait for ArgoCD
rather than applying manifests directly.

1. A1: Follow [the Pi-hole procedure](../../../1-proxmox/pihole/README.md).
   Copy the reviewed dnsmasq file:

   ```sh
   scp 1-proxmox/pihole/dnsmasq.d/10-epaflix.conf pihole:/etc/dnsmasq.d/10-epaflix.conf
   ```

   Add these lines to the live
   `/etc/unbound/unbound.conf.d/no-aaaa-leak.conf`, retaining its comments
   rather than replacing the file:

   ```text
       local-zone: "mcp-homelab.epaflix.com." static
       local-zone: "mcp-work.epaflix.com." static
   ```

   Apply both changes together, then check the restart and answers:

   ```sh
   ssh pihole 'unbound-checkconf /etc/unbound/unbound.conf && unbound-control reload && systemctl restart pihole-FTL'
   ssh pihole 'ps -o lstart= -C pihole-FTL
   for instance in homelab work; do
     dig +short @127.0.0.1 mcp-$instance.epaflix.com A
     dig +short @127.0.0.1 mcp-$instance.epaflix.com AAAA
   done'
   ```

   A must answer `192.168.10.102`; AAAA must be empty. Restarting Pi-hole
   briefly interrupts LAN DNS.

2. A2: Wait for automatic sync to load the reviewed routes and grants:

   ```sh
   for instance in homelab work; do
     argocd app wait mcp-hub-$instance --sync --health --timeout=300
     kubectl --context epaflix -n mcp-hub-$instance rollout status deployment/mcp-hub --timeout=300s
     kubectl --context epaflix -n mcp-hub-$instance get ingressroute mcp-hub-internal -o 'jsonpath={.spec.entryPoints}'
   done
   ```

   Each route must show only `internal`. Using the existing public
   load-balancer address, verify that both hostnames return 404 there.

3. A3: Verify from homePC, then repeat from the laptop with `--machine laptop`:

   ```sh
   ssh homepc 'python3 ~/.local/share/mcp-hub-client/local-clients.py check --context epaflix --machine homepc'
   ```

   Run `check` before O4 only: it reads the `mcp-hub-client-<machine>`
   staging Secrets, which O4 deletes, so it fails after a completed hand-off.
   `check` uses that machine's staged tokens to initialize one approved
   model-facing route per hub and requires HTTP 401 for the other hub's token.
   It disables redirects and environment proxies and prints only fixed status
   messages. It performs no vault read or customer-data tool call.

## Owner-only vault and local agent steps

Run this section in the owner's local shell, not as an agent or from the
mixed pod. It requires homelab kubectl and the Bitwarden CLI installed as
`bw`; no Azure or work account login is needed.

1. O1: Unlock an existing vault CLI login without printing the session:

   ```sh
   BW_SESSION="$(bw unlock --raw)"
   export BW_SESSION
   ```

   For a first login, configure the owner's vault server and use
   `BW_SESSION="$(bw login --raw)"` instead. Do not copy the mixed pod's login
   cache or session.

2. O2: Copy the four tokens into separate vault items. Each invocation asks
   for a title; enter `mcp-hub <instance> <machine>`, for example
   `mcp-hub homelab homepc`, because O3 reads the items by that title. Run
   once because `vault`
   creates items rather than updating them:

   ```sh
   for instance in homelab work; do
     for machine in homepc laptop; do
       python3 ~/.local/share/mcp-hub-client/local-clients.py vault \
         --context epaflix --instance "$instance" --machine "$machine" || break 2
     done
   done
   ```

   Values pass into `bw encode` and `bw create item` over stdin, with command
   output captured rather than displayed.

3. O3: On each machine, set `machine` to `homepc` or `laptop`, then copy
   the O2 items into the desktop keyring, activate Pi configuration and
   launch. The snippet works in both zsh and bash. Pi reads the hub tokens
   from the keyring itself; only the cliproxy model key is exported. It prints
   only `ok` or `missing` per value and starts `/usr/bin/pi` without the
   `BW_*` session variables. Keep shell tracing disabled and do not dump the
   environment:

   ```sh
   machine=homepc
   bw sync >/dev/null
   for instance in homelab work; do
     token="$(bw get password "mcp-hub $instance $machine")" && [ -n "$token" ] &&
       printf %s "$token" | secret-tool store --label "mcp-hub $instance" service mcp-hub key "$instance"
     if [ -n "$(secret-tool lookup service mcp-hub key "$instance")" ]; then echo "$instance ok"; else echo "$instance missing"; fi
     python3 ~/.local/share/mcp-hub-client/local-clients.py configure \
       --context epaflix --instance "$instance"
   done
   CLIPROXYAPI_API_KEY="$(secret-tool lookup service cliproxy key api)"
   export CLIPROXYAPI_API_KEY
   if [ -n "$CLIPROXYAPI_API_KEY" ]; then echo "CLIPROXYAPI_API_KEY ok"; else echo "CLIPROXYAPI_API_KEY missing"; fi
   python3 -c 'import os; os.execve("/usr/bin/pi", ["pi"], {k: v for k, v in os.environ.items() if not k.startswith("BW_")})'
   unset token CLIPROXYAPI_API_KEY
   ```

   Later launches need only Pi, from any shell or program, since the keyring
   items stay. The launcher strips the vault session from the agent
   environment. Complete one approved harmless tool call through each hub
   without reading work customer data; a handshake alone does not meet that
   acceptance check.

4. O4: After O2, O3 and A3 succeed for both machines, delete only the temporary
   token staging Secrets:

   ```sh
   for instance in homelab work; do
     kubectl --context epaflix -n mcp-hub-$instance delete secret \
       mcp-hub-client-homepc mcp-hub-client-laptop
   done
   ```

   Hub digests and vault items remain. `provision`, `vault` and `check` need
   the staging Secrets, so do not rerun them after cleanup.

## Offline checks

Follow [the instance offline checks](README.md#offline-verification). For
just the local-client tests, run `python3 -m unittest test_local_clients`
from `2-k3s/23.mcp-hub/tests`.
