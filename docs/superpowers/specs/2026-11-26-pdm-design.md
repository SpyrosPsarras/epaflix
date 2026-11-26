# Proxmox Datacenter Manager replaces PegaProx

## Why

PegaProx is a third-party web interface for Proxmox. It runs in CT 1021 on evanthoulaki and is kept current by our own script in `1-proxmox/pegaprox/`. We want one Proxmox-made screen that shows the `e-pavli` cluster and the PBS together. Proxmox Datacenter Manager (PDM) is that screen, and its updates come from the Proxmox apt repository.

PegaProx does nothing on its own (no schedules, no load balancing, no alerts), so removing it loses no automatic work.

## Goals

- PDM shows the `e-pavli` cluster and the PBS (CT 1031) as remotes.
- Login through Authentik, both in front (Traefik forward-auth) and inside PDM (OpenID realm).
- PegaProx, its update script, route and secrets are gone afterwards.

## Non-goals

- PVE 9.3 upgrade and the other findings (governor timer, PBS usage alert, ntfy canary, update notifications). Each is its own PR.
- Using PDM features beyond viewing and manual actions, such as SDN or the update dashboard.

## Approaches

| | Approach | Trade-off |
| --- | --- | --- |
| O1 | New container next to PegaProx, PDM from the apt repo | Easy rollback, but a container install |
| O2 | Wipe CT 1021 and install PDM in it | No new name or address, no rollback, misleading name |
| **O3** | **VM from the PDM ISO, picked** | Install Proxmox supports best, Proxmox kernel, no container limits. Same RAM as PegaProx today |

O3 was picked by Spyros. It matches how shypowder was installed (unattended ISO with an answer file).

## Design

- **VM.** VM 1022 `pdm` on evanthoulaki, storage `local-raid`, 2 cores, 4 GiB RAM, 40 GB disk, `onboot: 1`, `agent: 1`, bridge `vmbr0`, static `192.168.10.22/24`, gateway `192.168.10.1`, DNS `192.168.10.30`. These are the sizes Proxmox recommends. On 2026-11-26 the address had no guest config, no DNS record and did not answer ping.
- **Install.** `proxmox-auto-install-assistant prepare-iso` with an answer file. The root password hash and SSH public keys are fetched from the vault at ISO build time into a private answer file that is never committed. The non-secret answer fields and the steps go in `1-proxmox/pdm/README.md`.
- **Repository.** `pdm-no-subscription`, enterprise source disabled, as on the PVE hosts.
- **Login.** A new Authentik OAuth2/OpenID provider and application for PDM. PDM gets an `openid` realm. Spyros's Authentik admin group gets the Administrator role. The client secret goes straight from Authentik into the vault and PDM, not into chat or git.
- **Remotes.** The `e-pavli` cluster and the PBS, each added with its own API token created for PDM. Token secrets are created on the host and stored in the vault.
- **Access.** `pdm.epaflix.com` in `1-proxmox/pihole/dnsmasq.d/10-epaflix.conf`. A Traefik route like `pegaprox-proxy.yaml` with Authentik forward-auth and backend port 8443. The `Endpoints` object is excluded from Argo CD and gets applied by hand, as the README says.
- **Inventory.** Add `pdm=root@192.168.10.22` to the rackpeek scan list and install `rackpeek-scan` on the VM. Update the guest list in `README.md`.
- **Removal.** After Spyros confirms PDM works: stop CT 1021 and set `onboot: 0`. A few days later, with his yes, delete CT 1021, `1-proxmox/pegaprox/`, the PegaProx route, the `pbs_pegaprox_*` and `pegaprox_*` keys in `.github/instructions/secrets.enc.yaml`, the PegaProx Authentik application and the PegaProx API tokens on PVE and PBS.

## Live changes that need a yes

Creating VM 1022, the Authentik provider, the API tokens and the DNS record. Applying the Traefik `Endpoints` by hand. Stopping and deleting CT 1021.

## Left to the builder

- The PBS role for the PDM token. Use the least privileged role that shows the datastores and tasks.
- The Authentik claim that carries the group.
