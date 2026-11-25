# T3 Connect and Zed SSH access

## T3 Connect

The isolated homelab and work servers bind only `127.0.0.1:3773`. The app
connects through T3 Connect or an SSH tunnel, never the pod IP or a cluster
HTTP Service. Loopback binding prevents T3 from advertising an unreachable
pod IP as a direct LAN endpoint. T3 Connect registers the loopback HTTP origin
for its managed `cloudflared` tunnel, so it does not need inbound port 3773.

Kubelet probes run `curl` inside the T3 container against the loopback health
URL. The `t3code` ClusterIP Service remains the StatefulSet
`serviceName` to avoid changing that immutable field, but exposes only SSH on
2222. Client SSH uses the separate `t3code-ssh` LoadBalancer. Idle-rollout uses
Kubernetes exec, image-prepull runs `true`, and hub/model calls are outbound;
none needs an HTTP Service or pod-IP access to 3773.

After both replacement pods pass T3 Connect and SSH acceptance, remove any
operator-created `t3code-direct-endpoint-temporary` NetworkPolicy from their
namespaces. The tracked policies continue to allow only SSH ingress on 2222.

Linking is a one-time owner step. Run the namespace-specific link command in
the [homelab README](../../13.t3code-homelab/README.md#t3-connect) or
[work README](../../13.t3code-work/README.md#t3-connect) in the owner's private
terminal. On first use, accept the offer to install the managed relay client,
`cloudflared`, into the pod's persistent T3 home. The owner approves the
device code with the owner's T3 account.

The link takes effect at the next server start. Both runtime Applications
automatically sync merged revisions with pruning and without self-healing.
For pending GitOps updates, `OnDelete` keeps the current pod running until
the idle-rollout job replaces it after 30 idle minutes, or an operator
approves an earlier replacement.
For input or grant changes, follow [input rotation](isolated-runtime.md#input-rotation)
and pause both `app-of-apps` and the runtime Application before scaling down.

For linking alone, replace the pod in an idle window. Set `namespace` to
`t3code-homelab` or `t3code-work` for the pod being linked:

```sh
namespace=t3code-homelab
pod=${namespace#t3code-}-0
kubectl --context epaflix -n "$namespace" delete pod "$pod"
```

After the replacement pod is ready, check status:

```sh
kubectl --context epaflix -n "$namespace" exec "$pod" -c t3 -- \
  t3 connect status --base-dir /home/t3/.t3
```

Status must show `Exposure: enabled`, `Environment link: provisioned` and
relay URL `https://relay.t3.codes`. The `t3` container log must show
`Relay client tunnel connection registered`. Authorization and the relay
client live in the persistent home and survive restarts. Run
`t3 connect unlink --base-dir /home/t3/.t3` in the `t3` container to disable it.

Desktop and phone clients reach the pod through the owner's T3 account.
No SSH tunnel or pairing code is needed for the T3 app. The READMEs retain
`connect-laptop.sh` for SSH acceptance and diagnostic checks.

## Tailscale

Both pods run a `tailscale` sidecar in kernel mode, so T3 can link to
tailnet peers. Each pod joins the tailnet as `t3code-homelab` or `t3code-work`
with `tag:t3code`. It accepts no subnet routes and no tailnet DNS. MagicDNS is
off, so link peers by their `100.x` address. The
[tailnet policy](../../21.tailscale/policy.json) lets `tag:t3code` reach only
`spylinux` on TCP 3773. Its identity lives on the `t3code-tailscale` PVC,
which only the sidecar mounts.

First apply `policy.json` in the Tailscale admin console. Argo CD does not
apply it, and `tag:t3code` must exist before a pod can request it.

Then enroll each pod once, after the merged sync and the pod replacement. Open
the login URL from the sidecar log under the owner account. The tag is approved
with the login because the owner is its tag owner. The sidecar has a ten-year boot timeout,
so a late enrollment does not restart it:

```sh
kubectl --context epaflix -n "$namespace" logs "$pod" -c tailscale | grep -m1 login.tailscale.com
```

In the Tailscale admin console, disable key expiry on both devices. Check:

```sh
kubectl --context epaflix -n "$namespace" exec "$pod" -c tailscale -- tailscale status
```

The laptop's T3 must listen on its tailnet address, for example
`t3 serve --lan`. Then link it from the pod whenever needed, in Settings →
Labs → Linked environments, or run `t3 pair` on the laptop and use a fresh
code for each pod with
`t3 environment link --pairing-code <code> http://100.119.235.123:3773`.

## Zed SSH access

Zed still needs SSH to the pods. The owner decision is to use the same
dedicated per-client keys prepared in the READMEs. Do not use the owner's
general agent key. The homelab pod's own SSH agent holds that key, so the
work pod must not accept it.

After preparing and provisioning both dedicated identities on each client,
the owner adds this as the first line of `~/.ssh/config` on homePC and the
laptop:

```sshconfig
Include ~/.config/t3-homelab/ssh/ssh_config ~/.config/t3-work/ssh/ssh_config
```

Check both connections:

```sh
ssh t3-homelab true && ssh t3-work true
```

Then add SSH connections in Zed for `t3-homelab` with project directory
`/home/t3/projects/homelab` and `t3-work` with project directory
`/home/t3/projects/work`.
