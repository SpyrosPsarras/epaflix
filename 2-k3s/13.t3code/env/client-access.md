# Tailscale and Zed SSH access

## App access

The isolated homelab and work servers bind only `127.0.0.1:3773`. Desktop and
phone apps reach them over Tailscale (below), never the pod IP or a cluster
HTTP Service. T3 Connect is unlinked in both pods; `t3 connect link` would
enable it again.

Kubelet probes run `curl` inside the T3 container against the loopback health
URL. The `t3code` ClusterIP Service remains the StatefulSet
`serviceName` to avoid changing that immutable field, but exposes only SSH on
2222. Client SSH uses the separate `t3code-ssh` LoadBalancer. Idle-rollout uses
Kubernetes exec, image-prepull runs `true`, and hub/model calls are outbound;
none needs an HTTP Service or pod-IP access to 3773. Remove any
operator-created `t3code-direct-endpoint-temporary` NetworkPolicy; the tracked
policies allow only SSH ingress on 2222.

Both runtime Applications automatically sync merged revisions with pruning and
without self-healing. For pending GitOps updates, `OnDelete` keeps the current
pod running until the idle-rollout job replaces it after 30 idle minutes, or an
operator approves an earlier replacement. For input or grant changes, follow
[input rotation](isolated-runtime.md#input-rotation) and pause both
`app-of-apps` and the runtime Application before scaling down.

The READMEs retain `connect-laptop.sh` for SSH acceptance and diagnostic checks.

## Tailscale

Both pods run a `tailscale` sidecar in kernel mode, so T3 can link to
tailnet peers. Each pod joins the tailnet as `t3code-homelab` or `t3code-work`
with `tag:t3code`. It accepts no subnet routes and no tailnet DNS. MagicDNS is
off, so the pods resolve tailnet names only through `hostAliases`. The
[tailnet policy](../../21.tailscale/policy.json) lets `tag:t3code` reach only
`spylinux` on TCP 3773. Its identity lives on the `t3code-tailscale` PVC,
which only the sidecar mounts.

Set `namespace` to `t3code-homelab` or `t3code-work` for the commands below:

```sh
namespace=t3code-homelab
pod=${namespace#t3code-}-0
```

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
`t3 environment link --pairing-code <code> http://spylinux.tailea078a.ts.net:3773`.
T3 refuses plain `http` to a `100.x` address but accepts `*.ts.net` names.
MagicDNS is off, so a `hostAliases` entry in both pods maps that name to
`100.119.235.123`. Update it if the laptop's tailnet address changes.

The sidecar also serves tailnet TCP 3773 to T3 on `127.0.0.1:3773`, from
`env/files/tailscale-serve.json`. T3 itself still binds only loopback. The
policy lets the owner's devices reach `tag:t3code` on TCP 3773. The phone uses
Pi-hole with MagicDNS off, so
[`40-tailnet.conf`](../../../1-proxmox/pihole/dnsmasq.d/40-tailnet.conf) maps
each pod's `*.ts.net` name to its tailnet address. Update it if a pod is
enrolled again and gets a new address. To pair a phone or desktop app, mint a
code in the pod and open it at the pod's tailnet name. Scan the QR code on the
phone, or open the printed URL in the desktop app:

```sh
client=phone
code=$(kubectl --context epaflix -n "$namespace" exec "$pod" -c t3 -- \
  t3 pair --label "$client" --base-dir /home/t3/.t3 | grep -o 'token=[A-Z0-9]*' | cut -d= -f2)
url="http://$namespace.tailea078a.ts.net:3773/pair#token=$code"
echo "$url"; qrencode -t ansiutf8 "$url"
```

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
