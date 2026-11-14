# Home Assistant

Home Assistant for the home devices. Design: `docs/superpowers/specs/2026-11-13-home-assistant-design.md`.

Hostnames https://ha.epaflix.com and https://esphome.epaflix.com on the Traefik `internal` entrypoint at `192.168.10.102`, so they answer on the LAN and over Tailscale only. No route uses `websecure`, `web` or Authentik forward auth; Home Assistant and ESPHome have their own logins.

## What runs

- Deployment `home-assistant`, image `ghcr.io/home-assistant/home-assistant`, one replica with `strategy: Recreate`, because a second pod cannot attach the RWO iSCSI volume during a rollout. No `hostNetwork`.
- PVC `home-assistant-config`, 5Gi `truenas-iscsi`, mounted at `/config`. It holds `.storage`, the UI-made automations, scripts and scenes, and the integration settings.
- ConfigMap `home-assistant-configuration` holds `configuration.yaml` and `http.json`. Init container `configuration` copies `configuration.yaml` into `/config` on every start. It creates `/config/.storage/http` from `http.json`, and `automations.yaml`, `scripts.yaml` and `scenes.yaml`, only when each is missing, so a restart keeps what was made in the UI.
- Traefik reaches the pod through the pod network, so the trusted proxy is the pod CIDR `10.42.0.0/16`. Home Assistant 2026.11 keeps its HTTP settings in `.storage/http` and ignores `http:` in `configuration.yaml` after a one-time import that must be confirmed in the UI within 5 minutes. `http.json` seeds that file on a fresh volume, so Home Assistant starts with the proxy settings and no trial. After that Home Assistant owns the file: change HTTP settings in the UI, not in Git. NetworkPolicy `home-assistant-ingress` lets only Traefik pods reach port 8123.
- Deployment `esphome`, image `ghcr.io/esphome/esphome`, one replica with `strategy: Recreate`, serves the ESPHome dashboard on port 6052 through Service `esphome`. PVC `esphome-config`, 5Gi `truenas-iscsi`, at `/config` holds the device YAML and build caches. The login comes from `ESPHOME_USERNAME` and `ESPHOME_PASSWORD` in Secret `esphome-auth`. NetworkPolicy `esphome-ingress` lets only Traefik pods reach port 6052.
- Reloader release `reloader-home-assistant` watches only this namespace and restarts Home Assistant when Secret `home-assistant-secrets` or ConfigMap `home-assistant-configuration` changes, ESPHome when Secret `esphome-auth` changes, the Roborock server when Secret `roborock-config` or `api-rr-tls` changes, and Mosquitto when Secret `mosquitto-config` changes. The releases in `16.reloader` watch only their own namespaces.

## Roborock server

Deployment `roborock` runs [local_roborock_server](https://github.com/Python-roborock/local_roborock_server), image `ghcr.io/python-roborock/local_roborock_server`, a local copy of Roborock's cloud for the vacuum. One replica with `strategy: Recreate`.

- Hostname `api-rr.epaflix.com`. Service `roborock` is a LoadBalancer on `192.168.10.116` with `externalTrafficPolicy: Local`, so the vacuum's HTTPS and MQTT logins keep its own source IP, which onboarding needs. Ports: 555 HTTPS (admin dashboard at `/admin`) and 8881 MQTT over TLS. There is no Traefik route.
- `/app/config.toml` comes read-only from Secret `roborock-config`. It holds the admin password hash, the session secret and the protocol login PIN hash. The dashboard's toggles for new connections and protocol auth try to rewrite this file and cannot, so change `new_connections_enabled` in Git only: re-encrypt `roborock-config` and let Argo CD sync.
- Certificate `api-rr` (`letsencrypt-dns`) writes Secret `api-rr-tls`, mounted read-only at `/run/certs`. The server reads it only at start.
- PVC `roborock-data`, 2Gi `truenas-iscsi`, at `/data`. `/data/runtime/` and `/data/state/` hold the device keys and credentials. Never delete either; the vacuum would need onboarding again.
- Requests 1 CPU and 1Gi, which onboarding needs.
- NetworkPolicy `roborock-ingress` allows TCP 555 and 8881 from any source (the vacuum, Home Assistant, the laptop during onboarding). Egress stays open for the dashboard's cloud import.

## Tibber Pulse broker

Design: `docs/superpowers/specs/2026-11-14-tibber-pulse-local-design.md`. The Tibber Pulse on the meter publishes to our own Mosquitto instead of Tibber's cloud. Home Assistant reads it locally, and a bridge forwards the Pulse topics to Tibber, so the Tibber app and the e-Golf smart charging keep working.

- Deployment `mosquitto`, image `eclipse-mosquitto`, one replica with `strategy: Recreate`, no volume: Pulse messages are live readings.
- Service `mosquitto` is a LoadBalancer on `192.168.10.117:1883` with `externalTrafficPolicy: Local`, so the Pulse's own IP reaches the pod. Plain MQTT with a login, no TLS (shortcut: plain MQTT on the LAN, move to 8883 with a cert-manager certificate if the Pulse accepts a custom CA).
- Secret `mosquitto-config` (SOPS) holds `mosquitto.conf` with the bridge, the password file, the ACL and the Pulse's certificate, key and CA. It also holds the Pulse device ID, which stays out of plain Git. Users: `tibber_pulse` (the Pulse) and `home_assistant`. Their passwords are login items in the homelab vault.
- The bridge `tibber` connects to Tibber's AWS IoT endpoint on 8883 with the Pulse's certificate and reports its state on `homelab/tibber-bridge/state` (1 connected, 0 not). Home Assistant shows it as `binary_sensor.tibber_bridge`.
- NetworkPolicy `mosquitto` allows 1883 only from the Pulse (`192.168.10.230`, a DHCP reservation in the router), Home Assistant and the blackbox exporter, and egress only to DNS and TCP 8883 on public addresses.
- Home Assistant reads the Pulse through the custom integration `tibber_pulse_mqtt` ([mrhedstrom/ha-tibber-pulse-mqtt](https://github.com/mrhedstrom/ha-tibber-pulse-mqtt)). The `configuration` init container installs the release pinned in `TIBBER_PULSE_MQTT_VERSION` into `/config/custom_components` when the installed version differs, and checks it against `TIBBER_PULSE_MQTT_SHA256`. A failed download keeps the installed copy, so Home Assistant starts without internet. A wrong checksum stops the init container. Renovate bumps the version but cannot update the checksum, so its PR waits with the `needs-checksum` label until someone copies the release asset's sha256.
- Blackbox probe job `mosquitto-probe` raises `MosquittoProbeFailed` when the broker stops answering.

If the broker or the cluster is down, Tibber gets no Pulse data and the e-Golf charging loses it too.

Rollback: reset the Pulse (hold its button 5 seconds) and run the normal Pulse setup in the Tibber app, so it talks to Tibber directly again. Then revert the broker in Git. Scaling `mosquitto` to zero by hand does not stick, because `app-home-assistant` self-heals.

The Pulse certificate, key and topics come from the Pulse's setup page at `http://10.133.70.1`, reached on its own Wi-Fi after a reset. The upstream README of `mrhedstrom/ha-tibber-pulse-mqtt` describes the procedure. If Tibber rotates the device certificate, the bridge fails and the files must be extracted again.

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
