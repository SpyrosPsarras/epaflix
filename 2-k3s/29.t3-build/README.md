# t3-build

The homelab T3 pod has no Docker and no route to the owner's desktops. This
namespace lets it build images and run one-shot containers anyway. Owner
decision of 2026-10-23, options A and D. The work pod has no access since
2026-11-17: it is denied the epaflix cluster.

## What is here

`base/` holds the builder namespace; the `homelab/` overlay names it
`t3-build-homelab`. It contains:

- Pod Security `baseline` (enforce, warn and audit), so no privileged,
  hostPath or host-network pods. Kaniko needs none of these.
- ServiceAccount `builder` bound to Role `builder`. It allows only what the
  helpers' kubectl calls need: pods (create, get, list, watch, delete),
  pods/log (get), pods/attach (create), configmaps (create, get, delete),
  secrets (create, delete) and jobs (create, get, delete). It grants nothing
  anywhere else. `tools/rbac.test.py` pins
  the matrix.
- NetworkPolicies `default-deny` and `public-egress`. Pods get DNS and public
  IPv4 only. The LAN and cluster ranges are closed with the same except list as
  `13.t3code-work/network-policy.yaml`. A registry on the LAN is unreachable.
- ResourceQuota `builder` (10 pods, 4 CPU, 8 GiB, 40 GiB ephemeral storage)
  and LimitRange `builder`. A pod that sets no resources gets 1 CPU and 1 GiB.
  A kaniko build takes 2 CPU and 4 GiB, so each pod runs two builds at once.
- ConfigMap `kaniko-job`, a Job template (`base/files/kaniko-job.yaml`), not a
  live Job. The kaniko image is pinned by digest. Renovate's kubernetes
  manager watches the file. The upstream kaniko repository was archived in
  2025 and v1.24.0 is its last release.

Argo CD Application `t3-build` syncs this directory with pruning.

## How the pods use it

The homelab pod mounts Secret `t3-build-kubeconfig` at `/run/t3-build/kubeconfig`
(`optional: true`) and sets `T3_BUILD_KUBECONFIG` to that path. `KUBECONFIG`
is untouched. The kubeconfig's context names the builder namespace, and
the helpers use it; they never pass `-n`. The homelab pod is cluster-admin but
builds through the same scoped path.

Two helpers ship in `/scripts` (sources in `13.t3code/env/files/`):

```sh
/scripts/t3-build <context-dir> --destination <registry/repo:tag> [--dockerfile path] [--docker-config file]
/scripts/t3-run <image> [-- cmd...]
```

`t3-build` packs the context (without `.git`) into a gzipped tar. It creates
a Job from the `kaniko-job` template, then the docker config Secret, if
given, and the context ConfigMap, both owned by the Job, so the Job's TTL
collects them. It streams the logs and stops on the Job's `Complete` or
`Failed` condition, or after 32 minutes. It deletes the Job, ConfigMap and
Secret on every exit path and retries the delete once. Exit 0: pushed. 1:
failed. 2: pushed, but the delete failed (reported on stderr). `t3-run` is
`kubectl run --rm -i --restart=Never` with stdin and stdout attached and a
`t3-run-<random>` pod name. It deletes the pod on exit, Ctrl-C or SIGTERM and
returns the container's exit code (130 on interrupt). Both refuse to run
without the kubeconfig.

Registry credentials come from the pod's vault. `vault run` hides the child's
output, so send the build log to a private file:

```sh
log=$(mktemp -d)/build.log
vault run --name '<registry item>' --password-env VAULT_REGISTRY_TOKEN -- bash -c \
  'auth=$(printf "%s:%s" "<user>" "$VAULT_REGISTRY_TOKEN" | base64 -w0)
   /scripts/t3-build . --destination ghcr.io/<owner>/<repo>:<tag> \
     --docker-config <(printf "{\"auths\":{\"ghcr.io\":{\"auth\":\"%s\"}}}" "$auth") >'"$log"' 2>&1'
cat "$log"
```

## Handoffs

Files meant for the owner's desktops go in `~/handoffs/` in the pod. The
desktops already SSH into the pods through the `t3code-ssh` LoadBalancer and
pull from there. The pods never push files out, and there is no new transport.

## Owner steps

Once Argo CD has synced `t3-build`, run this from a desktop with an admin
context:

```sh
python3 2-k3s/29.t3-build/tools/build-kubeconfig.py --context epaflix --pod homelab
```

The run creates `builder-token` in `t3-build-homelab` and writes
`t3-build-kubeconfig` into `t3code-homelab`, with `t3-build-homelab` as the
context's namespace. It prints only
`t3-build kubeconfig prepared for homelab.` The pod picks the Secret up at its
next restart. Re-running is a no-op. A differing existing grant stops the
script; delete it by hand first.

To revoke, delete `builder-token` in `t3-build-homelab`.

Argo prunes the former `t3-build-work` namespace with its `builder-token`. The
work pod's leftover grant is removed in
[K2](../13.t3code-work/README.md#moving-from-pinned-contexts).

## Limits

- The gzipped context must stay under 1 MB (one ConfigMap). Larger contexts
  need a different upload path.
- Within one namespace the ServiceAccount can still mount its own registry
  Secret into a pod it creates. Use short-lived, push-only registry tokens.
- A build ends after 30 minutes (`activeDeadlineSeconds`). A finished Job that
  the helper could not delete is removed after 10 minutes, with its
  ConfigMap and Secret.
- Kaniko runs as root inside its pod, which `baseline` allows.
