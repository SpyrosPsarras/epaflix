# t3-build

The homelab and work T3 pods have no Docker and no route to the owner's
desktops. This namespace lets them build images and run one-shot containers
anyway. Owner decision of 2026-10-23, options A and D.

## What is here

- Namespace `t3-build` with ServiceAccounts `builder-homelab` and
  `builder-work`. Both bind to Role `builder`. It allows create, get, list,
  watch and delete on pods, pods/log, pods/attach, configmaps, secrets and
  jobs, plus create on pods/exec. It grants nothing outside the namespace.
  `tools/rbac.test.py` pins the matrix.
- NetworkPolicies `default-deny` and `public-egress`. Pods get DNS and public
  IPv4 only. The LAN and cluster ranges are closed with the same except list as
  `13.t3code-work/network-policy.yaml`. A registry on the LAN is unreachable.
- ResourceQuota `builder` (10 pods, 4 CPU, 8 GiB, 40 GiB ephemeral storage)
  and LimitRange `builder`. A pod that sets no resources gets 1 CPU and 1 GiB.
  A kaniko build takes 2 CPU and 4 GiB, so two builds run at once.
- ConfigMap `kaniko-job`, a Job template (`files/kaniko-job.yaml`), not a
  live Job. The kaniko image is pinned by digest. Renovate's kubernetes
  manager watches the file. The upstream kaniko repository was archived in
  2025 and v1.24.0 is its last release.

Argo CD Application `t3-build` syncs this directory with pruning.

## How the pods use it

Each pod mounts Secret `t3-build-kubeconfig` at `/run/t3-build/kubeconfig`
(`optional: true`) and sets `T3_BUILD_KUBECONFIG` to that path. `KUBECONFIG`
is untouched. The homelab pod is cluster-admin but builds through the same
scoped path, so the work boundary stays an RBAC fact.

Two helpers ship in the pods' `/scripts` (sources in `13.t3code/env/files/`):

```sh
/scripts/t3-build <context-dir> --destination <registry/repo:tag> [--dockerfile path] [--docker-config file]
/scripts/t3-run <image> [-- cmd...]
```

`t3-build` packs the context (without `.git`) into a gzipped tar. It puts the
tar in a ConfigMap and the docker config, if given, in a Secret, then creates
a Job from the `kaniko-job` template. It streams the logs and exits 0 if the
image was pushed, 1 otherwise. It deletes the Job, ConfigMap and Secret on
every exit path. `t3-run` is `kubectl run --rm -i --restart=Never` with stdin
and stdout attached. It returns the container's exit code. Both refuse to run
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
python3 2-k3s/29.t3-build/tools/build-kubeconfig.py --context epaflix --pod work
```

Each run creates `builder-<pod>-token` in `t3-build` and writes
`t3-build-kubeconfig` into `t3code-<pod>`. It prints only
`t3-build kubeconfig prepared for <pod>.` A pod picks the Secret up at its
next restart. Re-running is a no-op. A differing existing grant stops the
script; delete it by hand first.

To revoke a pod, delete its `builder-<pod>-token` Secret.

## Limits

- The gzipped context must stay under 1 MB (one ConfigMap). Larger contexts
  need a different upload path.
- Both pods share one namespace. Either ServiceAccount can read the other's
  in-flight registry Secret, logs and token Secret, or mount them into a pod.
  The work pod could therefore read a homelab registry credential while a
  homelab build runs. Use short-lived, push-only registry tokens. Split into
  one namespace per pod if that is not acceptable.
- Either ServiceAccount can delete the other's token Secret, which breaks that
  pod's builds until the owner re-runs `build-kubeconfig.py`.
- A build ends after 30 minutes (`activeDeadlineSeconds`). A finished Job that
  the helper could not delete is removed after 10 minutes.
- Kaniko runs as root inside its pod. The namespace sets no Pod Security level.
