# T3 Connect and Zed SSH access

## T3 Connect

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
