# Home Assistant

Home Assistant for the home devices. Design: `docs/superpowers/specs/2026-11-13-home-assistant-design.md`.

Hostnames https://ha.epaflix.com and https://esphome.epaflix.com on the Traefik `internal` entrypoint at `192.168.10.102`, so they answer on the LAN and over Tailscale only. No route uses `websecure`, `web` or Authentik forward auth; Home Assistant and ESPHome have their own logins.

## What runs

- Deployment `home-assistant`, image `ghcr.io/home-assistant/home-assistant`, one replica with `strategy: Recreate`, because a second pod cannot attach the RWO iSCSI volume during a rollout. No `hostNetwork`.
- PVC `home-assistant-config`, 5Gi `truenas-iscsi`, mounted at `/config`. It holds `.storage`, the UI-made automations, scripts and scenes, and the integration settings.
- ConfigMap `home-assistant-configuration` holds `configuration.yaml`. Init container `configuration` copies it into `/config` on every start. It creates `automations.yaml`, `scripts.yaml` and `scenes.yaml` only when they are missing, so a restart keeps what was made in the UI.
- Traefik reaches the pod through the pod network, so `http.trusted_proxies` is the pod CIDR `10.42.0.0/16`. NetworkPolicy `home-assistant-ingress` lets only Traefik pods reach port 8123.
- Deployment `esphome`, image `ghcr.io/esphome/esphome`, one replica with `strategy: Recreate`, serves the ESPHome dashboard on port 6052 through Service `esphome`. PVC `esphome-config`, 5Gi `truenas-iscsi`, at `/config` holds the device YAML and build caches. The login comes from `ESPHOME_USERNAME` and `ESPHOME_PASSWORD` in Secret `esphome-auth`. NetworkPolicy `esphome-ingress` lets only Traefik pods reach port 6052.
- Reloader release `reloader-home-assistant` watches only this namespace and restarts Home Assistant when Secret `home-assistant-secrets` or ConfigMap `home-assistant-configuration` changes, ESPHome when Secret `esphome-auth` changes, and the Roborock server when Secret `roborock-config` or `api-rr-tls` changes. The releases in `16.reloader` watch only their own namespaces.

## Roborock server

Deployment `roborock` runs [local_roborock_server](https://github.com/Python-roborock/local_roborock_server), image `ghcr.io/python-roborock/local_roborock_server`, a local copy of Roborock's cloud for the vacuum. One replica with `strategy: Recreate`.

- Hostname `api-rr.epaflix.com`. Service `roborock` is a LoadBalancer on `192.168.10.116` with `externalTrafficPolicy: Local`, so the vacuum's HTTPS and MQTT logins keep its own source IP, which onboarding needs. Ports: 555 HTTPS (admin dashboard at `/admin`) and 8881 MQTT over TLS. There is no Traefik route.
- `/app/config.toml` comes read-only from Secret `roborock-config`. It holds the admin password hash, the session secret and the protocol login PIN hash. The dashboard's toggles for new connections and protocol auth try to rewrite this file and cannot, so change `new_connections_enabled` in Git only: re-encrypt `roborock-config` and let Argo CD sync.
- Certificate `api-rr` (`letsencrypt-dns`) writes Secret `api-rr-tls`, mounted read-only at `/run/certs`. The server reads it only at start.
- PVC `roborock-data`, 2Gi `truenas-iscsi`, at `/data`. `/data/runtime/` and `/data/state/` hold the device keys and credentials. Never delete either; the vacuum would need onboarding again.
- Requests 1 CPU and 1Gi, which onboarding needs.
- NetworkPolicy `roborock-ingress` allows TCP 555 and 8881 from any source (the vacuum, Home Assistant, the laptop during onboarding). Egress stays open for the dashboard's cloud import.

## Database

History goes to database `homeassistant`, owned by role `homeassistant`, in `postgres-cluster` (CNPG `Database` and `DatabaseRole` in `postgres-system`, both with reclaim policy `retain`). The role password is in Secret `homeassistant-db-role` in `postgres-system`.

`recorder.db_url` comes from `!secret recorder_db_url` in `/config/secrets.yaml`, mounted from Secret `home-assistant-secrets`. `recorder.db_max_retries: 100` retries a failed connection for about 5 minutes (3 seconds apart), so a Postgres failover or node drain at start does not leave Home Assistant without history until the next restart.

## Secrets

`secrets.enc.yaml` (SOPS) holds the four Secrets. The role password, the ESPHome login (user `admin`) and the Roborock admin password, protocol PIN and session secret are also stored as login items in the homelab Vaultwarden collection. To change a Roborock value, update its vault item, hash it with upstream's `roborock_local_server.security.hash_password`, re-encrypt `roborock-config` and let Argo CD sync; Reloader restarts the server. To change the role password, update its vault item, re-encrypt both Secrets that hold it with the same password and let Argo CD sync; CNPG sets the new password and Reloader restarts Home Assistant. To change the ESPHome password, update its vault item, re-encrypt `esphome-auth` and let Argo CD sync; Reloader restarts ESPHome.

## Recovery

- History: restore `postgres-cluster` from its CNPG backup.
- Configuration and UI-made objects: the `home-assistant-config` PVC. `configuration.yaml` comes back from Git on every start.
- Home Assistant's own backups (Settings, System, Backups) land in `/config/backups` on the same PVC. They do not include the Postgres history.
- ESPHome device YAML: the `esphome-config` PVC.
- Roborock device keys and state: the `roborock-data` PVC.

## Known limits

- Samsung TV power-on uses a Wake-on-LAN broadcast. The broadcast does not leave the pod network, so Home Assistant cannot turn the TV on.
- The ESPHome dashboard finds devices through mDNS, which does not reach a pod, so device online status may show unknown. Flashing over the air works by IP: each device YAML sets `wifi: use_address: <reserved IP>`.
