# Central AKS authentication

All three T3 servers use the Azure CLI session of `spyros` on `192.168.10.240`.
Refresh tokens stay in `/home/spyros/.azure` on that host. Each kubeconfig exec
helper gets an AKS token through a dedicated SSH key restricted to `token` and
`login`. SSH forwarding and PTYs are disabled. The server pins the DavidHorn
tenant and AKS audience and serializes authentication with `flock`.

On any T3 server:

```sh
kubectl --kubeconfig ~/.kube/davidhornkubeconfig --context dev-aks-ft4-uk-south get namespaces
```

When Azure reports that reauthentication is required, the token helper starts
device-code login on the central host automatically and displays the URL and
code in your current session. Complete it in your browser. The pending token
request retries automatically. No separate login command is needed. Other
token requests wait up to 15 minutes for the central authentication lock.
Network and permission failures do not start login. Do not run local `az login`
to refresh this shared AKS access. `~/.local/bin/aks-auth login` remains available
for explicitly starting login.
The central host stays fixed regardless of which session requests login.
An unavailable central host prevents new token requests.

Epaflix remains isolated in `~/.kube/config`. The AKS helper is for kubectl,
not arbitrary Azure CLI commands. Both kubeconfigs and helpers live in each
server's persistent home. New environments require provisioning these files.

Deployment uses `files/aks-auth-central.sh` as the central forced command and
`files/aks-auth.sh` as `~/.local/bin/aks-auth` on each server. Client identities
and pinned host keys live in `~/.local/share/t3-aks-auth`, mode 0700, with keys
mode 0600. Each AKS exec entry invokes the absolute helper path with `token`.
The central server also uses this SSH path, so it takes the same cache lock.

Check the helpers with `python3 env/tools/aks-auth.test.py` from this directory's
parent, `2-k3s/13.t3code`.
