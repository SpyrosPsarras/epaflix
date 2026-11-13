# Home Assistant

Home Assistant for the home devices. Design: `docs/superpowers/specs/2026-11-13-home-assistant-design.md`.

Hostname https://ha.epaflix.com on the Traefik `internal` entrypoint at `192.168.10.102`, so it answers on the LAN and over Tailscale only. No route uses `websecure`, `web` or Authentik forward auth; Home Assistant has its own login.

## What runs

- Deployment `home-assistant`, image `ghcr.io/home-assistant/home-assistant`, one replica with `strategy: Recreate`, because a second pod cannot attach the RWO iSCSI volume during a rollout. No `hostNetwork`.
- PVC `home-assistant-config`, 5Gi `truenas-iscsi`, mounted at `/config`. It holds `.storage`, the UI-made automations, scripts and scenes, and the integration settings.
- ConfigMap `home-assistant-configuration` holds `configuration.yaml`. Init container `configuration` copies it into `/config` on every start. It creates `automations.yaml`, `scripts.yaml` and `scenes.yaml` only when they are missing, so a restart keeps what was made in the UI.
- Traefik reaches the pod through the pod network, so `http.trusted_proxies` is the pod CIDR `10.42.0.0/16`. NetworkPolicy `home-assistant-ingress` lets only Traefik pods reach port 8123.
- Reloader release `reloader-home-assistant` watches only this namespace and restarts Home Assistant when Secret `home-assistant-secrets` or ConfigMap `home-assistant-configuration` changes. The releases in `16.reloader` watch only their own namespaces.

## Database

History goes to database `homeassistant`, owned by role `homeassistant`, in `postgres-cluster` (CNPG `Database` and `DatabaseRole` in `postgres-system`, both with reclaim policy `retain`). The role password is in Secret `homeassistant-db-role` in `postgres-system`.

`recorder.db_url` comes from `!secret recorder_db_url` in `/config/secrets.yaml`, mounted from Secret `home-assistant-secrets`. `recorder.db_max_retries: 100` retries a failed connection for about 5 minutes (3 seconds apart), so a Postgres failover or node drain at start does not leave Home Assistant without history until the next restart.

## Secrets

`secrets.enc.yaml` (SOPS) holds both Secrets. The role password is also stored as a login item in the homelab Vaultwarden collection. To change it, update the vault item, re-encrypt both Secrets with the same password and let Argo CD sync; CNPG sets the new password and Reloader restarts Home Assistant.

## Recovery

- History: restore `postgres-cluster` from its CNPG backup.
- Configuration and UI-made objects: the `home-assistant-config` PVC. `configuration.yaml` comes back from Git on every start.
- Home Assistant's own backups (Settings, System, Backups) land in `/config/backups` on the same PVC. They do not include the Postgres history.

## Known limits

- Samsung TV power-on uses a Wake-on-LAN broadcast. The broadcast does not leave the pod network, so Home Assistant cannot turn the TV on.
