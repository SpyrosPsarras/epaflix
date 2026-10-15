# Homelab SSH

To reach a homelab machine, run `ssh <name>`. You already have access; do not ask Spyros for an address, a username or a key.

- The names are the `Host` lines of the generated homelab SSH config (`homepc`, `laptop`, `k3s-master-51`, `truenas`, ...):
  - t3code pod: `/scripts/homelab-ssh.conf`
  - homePC and laptop: `~/Documents/Epaflix/k3s-swarm-proxmox/1-proxmox/ssh/homelab.conf`
- Check access first with `ssh <name> true`. Copy files with `scp`/`rsync` using the same names.
- Keys come from an SSH agent backed by Vaultwarden:
  - In the isolated homelab pod, OpenSSH selects the agent through inventory-only
    `IdentityAgent`. `SSH_AUTH_SOCK` is not exported. For diagnostics, use
    `SSH_AUTH_SOCK=/run/t3-vault/ssh-agent.sock ssh-add -l >/dev/null`.
  - The legacy mixed pod uses its exported `SSH_AUTH_SOCK`.
  - homePC and the laptop use Bitwarden Desktop.
  Host keys are handled by the config.
- Never read `~/.ssh`, never look in Vaultwarden for SSH keys, never ask for passwords.
- If `ssh` fails with an agent error on homePC or the laptop, ask Spyros to unlock Bitwarden. In the pod, report the error from `ssh -v <name> true`.
- A machine missing from the list is not set up yet: it needs a `# ssh-user=` DNS line in `1-proxmox/pihole/dnsmasq.d/30-epaflix-lan.conf` and the two homelab keys in its `authorized_keys`.
