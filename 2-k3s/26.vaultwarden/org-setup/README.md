# One-off vault organization setup

Implements #1838. Run the operator commands in the mixed t3code pod or an admin
shell with the same cluster access. Nothing runs on homePC or the laptop.
Required tools are Python 3, PyYAML, kubectl, kustomize and argocd.

The pinned `bw` 2026.8.0 rejects `register` and `create organization`. Playwright
drives those two web-vault forms; the stock CLI handles encrypted collections
and membership confirmation. With SMTP disabled, invitations to registered
users are auto-accepted, so the Job can confirm both owners without invite clicks.
It checks synthetic isolation, deletes its canaries and deletes the setup account.

The permanent account policy lives in the base `server.yaml`. Merge it with
this implementation, before enabling the temporary component or running setup.
The [account policy](../README.md#account-policy) records the settings and
accepted risk. Removing the component must not revert that policy.

## Identities and Secrets

`identities.json` defines the orgs, namespaces, non-deliverable agent email
pattern and checkpoint names. Agent addresses are independent of the owner's
mailbox. Preparation reads the configured single owner privately into the setup
Secret. Passwords go directly into Secrets; existing Secrets and unrelated keys
are preserved.

| Namespace | Secret | Keys |
| --- | --- | --- |
| `t3code-homelab` | `vaultwarden-agent` | `BW_EMAIL`, `BW_PASSWORD`, `BW_SERVER`, `BW_ORGANIZATION_ID`, `BW_COLLECTION_ID` |
| `t3code-work` | `vaultwarden-agent` | Same keys |
| `vaultwarden` | `vaultwarden-org-setup` | Temporary `ADMIN_TOKEN` and `BW_PASSWORD`; private owner identity and checkpoints |

Each agent is a confirmed ordinary member of its own org only, with explicit
read-write access to one collection. No agent gets admin, owner, group,
management or all-collections grants. Unexpected memberships and ambiguous
names stop setup. Credentials never reach logs, CLI profiles use separate
memory-backed directories and browser requests stay on the vault origin.
The Job renderer resolves kubectl from the parent Kustomization's image pin.

## Post-merge runbook

All Deployment changes use reviewed commits to `main` and Argo. The base has
no setup RBAC or scripts ConfigMap. The temporary `org-setup` component owns
those resources. Reserve a maintenance window because it disables SMTP,
including email 2FA and password-reset mail. Do not move existing vault items
until the component has been reverted and temporary access has been removed.

### A1. Prepare namespaces and Secrets

After merging the implementation and permanent base policy, use context
`epaflix`. Preparation creates missing backend namespaces from `identities.json`
before writing Secrets. Run it before enabling the component:

```sh
kubectl config current-context
kubectl -n vaultwarden get jobs,pods
kubectl -n vaultwarden exec deploy/vaultwarden -- sh -ec 'test ! -e /data/config.json'
python3 2-k3s/26.vaultwarden/org-setup/prepare.py
```

Stop if a backup is capturing data or the config-file check fails. File overrides
need a private review. Do not prepare a new state Secret while the component
is already active.

### A2. Merge the temporary component and run the Job

Add to `2-k3s/26.vaultwarden/kustomization.yaml`:

```yaml
components:
  - org-setup
```

After that edit merges:

```sh
argocd app sync vaultwarden
argocd app wait vaultwarden --sync --health --timeout 300
kubectl -n vaultwarden rollout status deployment/vaultwarden --timeout=300s
bash 2-k3s/26.vaultwarden/org-setup/run-job.sh
kubectl -n vaultwarden wait --for=condition=complete job/vault-org-setup --timeout=1800s
kubectl -n vaultwarden logs job/vault-org-setup -c setup
kubectl -n vaultwarden get secret vaultwarden-org-setup -o jsonpath='{.data.stage}' | base64 -d
printf '\n'
```

The component permits org creation only for the setup account and enables its
temporary admin token. Proceed only when the checkpoint is `finalized`.
If it is `waiting-for-owner`, the setup account remains intact. Report the
blocked owner account, acceptance or policy step without dumping API responses.
The owner resolves that step in the web vault. Delete the completed Job and
rerun `run-job.sh` to reuse the existing accounts and orgs. Never attempt the
owner's login, 2FA or recovery yourself.

### A3. Merge the component revert and remove temporary access

Remove only the `components` entry added in A2, leaving the permanent base
policy unchanged. After that edit merges:

```sh
argocd app sync vaultwarden
argocd app wait vaultwarden --sync --health --timeout 300
kubectl -n vaultwarden rollout status deployment/vaultwarden --timeout=300s
kubectl -n vaultwarden patch secret vaultwarden-org-setup --type=merge -p '{"data":{"ADMIN_TOKEN":null,"BW_PASSWORD":null}}'
kubectl -n vaultwarden delete job vault-org-setup --ignore-not-found --wait=true
kubectl delete -f 2-k3s/26.vaultwarden/org-setup/access.yaml --ignore-not-found
kubectl -n vaultwarden delete configmap vaultwarden-org-setup-scripts --ignore-not-found
```

The revert restores SMTP and the original org-creation policy, and disables
admin access. Fresh agents can log in without email verification. Argo pruning
is disabled, so the explicit deletions remove the now-untracked setup resources.
Keep the checkpoint Secret to prevent accidental setup recreation. Do not delete
backend Secrets or vault data.

### A4. Owner confirms permission and moves folders

Confirm the employer allows work secrets in this vault before moving the work folder.
In the web vault, move the `homelab` folder's items into the homelab org's
`homelab` collection, then the `work` folder's items into the work org's `work`
collection. New agent items go directly into their matching org.

## Checks

```sh
npm test --prefix 2-k3s/26.vaultwarden/org-setup
python3 2-k3s/26.vaultwarden/org-setup/tests/render.py
bash -n 2-k3s/26.vaultwarden/org-setup/run-job.sh
python3 -m py_compile 2-k3s/26.vaultwarden/org-setup/prepare.py 2-k3s/26.vaultwarden/org-setup/render-job.py 2-k3s/26.vaultwarden/org-setup/tests/*.py
python3 2-k3s/26.vaultwarden/org-setup/render-job.py | kubectl apply --dry-run=client -f - -o name
git diff --check
```

The render check uses Secret metadata stubs. A full encrypted build needs the
approved KSOPS environment:

```sh
kustomize build --enable-alpha-plugins --enable-exec 2-k3s/26.vaultwarden >/dev/null
```

For the disposable cluster rehearsal, set `VAULT_REHEARSAL_URL` to an isolated
`https://vault-org-1838-test.<domain>` hostname covered by the existing trusted
wildcard certificate. Set `VAULT_REHEARSAL_INGRESS_IP` to its internal ingress IP,
then run:

```sh
python3 2-k3s/26.vaultwarden/org-setup/tests/rehearse.py
```

It tests owner waiting/resumption, confirmed memberships, denied cross-org
canary reads, setup-account deletion, unchanged rerun credentials and IDs,
temporary-resource cleanup and fresh agent logins after SMTP restoration.
It also checks that ordinary agents cannot invite through emergency access.
Those logins assert the agent profiles remain email-unverified. SMTP points to
a closed loopback port; the test sends no mail and needs no SMTP receiver.

The rehearsal derives scratch backend namespaces from the shared org list,
refuses existing scratch namespaces and deletes its resources in `finally`.
If killed, inspect its `vault-org-1838-*` namespaces before removing them.
The fixed Job name prevents concurrent setup runs. Never delete an active Job
to start another.
