# AKS authentication

The `t3code` pod uses the Azure CLI session of `spyros` in its own home.
Refresh tokens stay in `/home/spyros/.azure`. Each work kubeconfig exec
entry calls `~/.local/bin/aks-auth token`, which runs
`~/.local/bin/aks-auth-central` locally. That helper pins the work tenant
and AKS audience and serializes authentication with `flock`.

```sh
kubectl --kubeconfig ~/.kube/work-kubeconfig --context '<aks-dev-context>' get namespaces
```

When Azure reports that reauthentication is required, the token helper starts
device-code login and shows the URL and code in your current session. Complete
it in your browser and the pending token request retries. Other token requests
wait up to 15 minutes for the lock. Network and permission failures do not
start login. `~/.local/bin/aks-auth login` starts login explicitly.

Epaflix stays isolated in `~/.kube/config`. The AKS helper is for kubectl, not
arbitrary Azure CLI commands.

`files/aks-auth-central.sh` is `~/.local/bin/aks-auth-central` and
`files/aks-auth.sh` is `~/.local/bin/aks-auth` in the pod home. Both live on the
home PVC, not in the image, so copy them by hand after editing.

The mixed `t3code` pod is no longer an AKS authentication path. The helpers
above describe its legacy setup; `AKS_TENANT_ID` is no longer supplied by
`cliproxy-secrets`. The work pod's `/scripts/work-aks-auth.py` reads `tenantId`
from the reviewed `/run/t3-inputs/work-auth.json`, mounted from
`t3-runtime-inputs` in `t3code-work`, and verifies its digest against
`runtime.json` before authentication. See
[`../../13.t3code-work/README.md`](../../13.t3code-work/README.md) for the
current work authentication procedure.

Check the helpers with `python3 env/tools/aks-auth.test.py` from
`2-k3s/13.t3code`.
