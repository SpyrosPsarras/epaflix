# Work SSH

Use only the owner-approved aliases in the generated SSH config. The private
host list is `/run/t3-work-ssh/hosts.json`. Do not print it, the generated
config, pinned host keys or addresses into model-visible output.

OpenSSH selects this pod's work-only agent through the approved alias config.
T3 processes and SSH login sessions do not receive `SSH_AUTH_SOCK`.
The vault sidecar loads SSH key items from its configured work organization and
collection at startup only when approved hosts exist. Missing or empty host
input keeps the pod usable with no work hosts and an empty agent. Do not infer
a host list or enable egress without owner approval. The socket permits listing and signing, not changing
keys. Never read private keys or ask for a password. Do not forward the agent.

Check an approved non-production alias with `ssh <alias> true`. Report only the
exit status. A failure requires checking the private host approval, pinned host
key, target-only egress policy and work collection membership. Do not disable
host key checks or add a general LAN rule. Key-set changes require an
owner-approved pod restart and replace the old agent.
