# Homelab SSH

Run `ssh homepc true`, `ssh laptop true` or `ssh k3s-master-51 true` before
working on a host. Use the same aliases with scp and rsync. Other configured
aliases are in `/scripts/homelab-ssh.conf`, generated from the homelab DNS
records. Do not ask for an address, user, password or private key.

OpenSSH selects `/run/t3-vault/ssh-agent.sock` through `IdentityAgent` for
inventory hosts. T3 processes and SSH login sessions do not receive
`SSH_AUTH_SOCK`, so other clients do not see the agent by default. The homelab
vault sidecar loads the approved collection's SSH-key items into a UID-1001
agent.
With no SSH-key items, the pod stays usable but the agent is empty and
outbound SSH cannot authenticate. The owner must move the intended keys into
scope and restart the pod or vault sidecar. There are no fallback identity
files, forwarded agents or owner vault logins. Do not read `~/.ssh` or fetch
SSH keys from the vault yourself. If the agent fails, report the failure from
`ssh -v <name> true` without dumping environments or vault responses.

Only inventory hosts use the homelab agent and accept a new host key on first
contact. Other hosts use no agent and require strict host-key checking, even
when `SSH_AUTH_SOCK` points to the homelab agent. The config remembers
inventory host keys on the homelab home PVC and rejects a changed key. First
contact uses trust on first use, not an independently verified host key. Do
not delete known_hosts or weaken host-key checking to bypass a mismatch.

The laptop must be on the home LAN for the cluster to reach it. When it is
away, a connection timeout is a routing or availability result. Tailscale's
current device-to-LAN routes do not provide a cluster-to-laptop route.
