# Work SSH

Outbound SSH works only for owner-approved hosts in this pod's generated SSH
config, plus GitHub. The private host list is `/run/t3-work-ssh/hosts.json`. Do not print it,
the generated config, pinned host keys or addresses into model-visible output.

For an alias stored in `SSH_TARGET`, check without connecting by filtering
`ssh -G` to the agent setting:

```sh
ssh -G "$SSH_TARGET" 2>/dev/null | awk '$1 == "identityagent" {print $2}'
```

An unapproved host reports `none`; an approved alias selects this pod's work
agent. The [work README](https://github.com/SpyrosPsarras/epaflix/blob/main/2-k3s/13.t3code-work/README.md#outbound-work-ssh)
describes host approval.

OpenSSH selects this pod's work-only agent through the approved alias config.
T3 processes and SSH login sessions do not receive `SSH_AUTH_SOCK`.
The vault sidecar loads SSH key items from its configured work organization and
collection at startup only when approved hosts exist. Missing or empty host
input keeps the pod usable with no work hosts and an empty agent. Do not infer
a host list or enable egress without owner approval. The socket permits listing and signing, not changing
keys. Never read private keys or ask for a password. Do not forward the agent.

`github.com` is the single allowed hostname, so `git@github.com:` remotes fetch
and push through this pod's work agent. GitHub must know the public key of a
work collection SSH key item. The agent loads keys only while approved hosts
exist. Host keys are pinned from GitHub's published keys; never use
`ssh-keyscan` or accept a new GitHub host key. Check with `ssh -T git@github.com`
and report only the exit code: 1 is success, because GitHub prints a greeting
and closes the session.

Check an approved non-production alias with `ssh <alias> true`. Report only the
exit status. A failure requires checking the private host approval, pinned host
key, target-only egress policy and work collection membership. Do not disable
host key checks or add a general LAN rule. The agent picks up key-set changes
within about 10 seconds and drops removed keys. An SSH command that was
authenticating while the agent reloaded can fail once; run it again.
