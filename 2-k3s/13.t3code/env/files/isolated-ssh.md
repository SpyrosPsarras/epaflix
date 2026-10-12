# Homelab SSH in this runtime

This home has no forwarded laptop agent, copied private keys or vault login.
Vault-backed SSH access is disabled pending its separate integration ticket.
Do not try legacy credential files or another environment's agent socket.

The laptop's public login key authenticates to this backend's SSH server. It
does not grant agents outbound SSH access. Use only an explicitly approved
homelab target when that separate access has been provisioned. A connection
timeout is a routing or availability result, not evidence of a bad credential.
