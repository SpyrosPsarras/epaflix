# Home Assistant

Home Assistant for the home devices. Design: `docs/superpowers/specs/2026-11-13-home-assistant-design.md`.

Hostnames https://ha.epaflix.com and https://esphome.epaflix.com on the Traefik `internal` entrypoint at `192.168.10.102`, so they answer on the LAN and over Tailscale only. No route uses `websecure`, `web` or Authentik forward auth; Home Assistant and ESPHome have their own logins.

## What runs

- Deployment `home-assistant`, image `ghcr.io/home-assistant/home-assistant`, one replica with `strategy: Recreate`, because a second pod cannot attach the RWO iSCSI volume during a rollout. No `hostNetwork`.
- PVC `home-assistant-config`, 5Gi `truenas-iscsi`, mounted at `/config`. It holds `.storage`, the UI-made automations, scripts and scenes, and the integration settings.
- ConfigMap `home-assistant-configuration` holds `configuration.yaml` and `http.json`. Init container `configuration` copies `configuration.yaml` into `/config` on every start. It creates `/config/.storage/http` from `http.json`, and `automations.yaml`, `scripts.yaml` and `scenes.yaml`, only when each is missing, so a restart keeps what was made in the UI.
- Traefik reaches the pod through the pod network, so the trusted proxy is the pod CIDR `10.42.0.0/16`. Home Assistant 2026.11 keeps its HTTP settings in `.storage/http` and ignores `http:` in `configuration.yaml` after a one-time import that must be confirmed in the UI within 5 minutes. `http.json` seeds that file on a fresh volume, so Home Assistant starts with the proxy settings and no trial. After that Home Assistant owns the file: change HTTP settings in the UI, not in Git. NetworkPolicy `home-assistant-ingress` lets only Traefik pods reach port 8123.
- Deployment `esphome`, image `ghcr.io/esphome/esphome`, one replica with `strategy: Recreate`, serves the ESPHome dashboard on port 6052 through Service `esphome`. PVC `esphome-config`, 5Gi `truenas-iscsi`, at `/config` holds the device YAML and build caches. The login comes from `ESPHOME_USERNAME` and `ESPHOME_PASSWORD` in Secret `esphome-auth`. NetworkPolicy `esphome-ingress` lets only Traefik pods reach port 6052.
- The ESPHome device configs themselves live in `3-devices/esphome/` (the boards are not part of the cluster); that folder's README lists the devices and how to update them.
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

Design: `docs/superpowers/specs/2026-11-14-tibber-pulse-local-design.md`. The Tibber Pulse on the meter publishes to our own Mosquitto instead of Tibber's cloud. Home Assistant reads it locally, and a bridge forwards the Pulse topics to Tibber, renaming the local `tibber-pulse-<id>/publish` to the Pulse's AWS IoT topic `$aws/rules/push_to_kinesis/tibber-pulse-<id>/publish`, so the Tibber app and the e-Golf smart charging keep working.

- Deployment `mosquitto`, image `eclipse-mosquitto`, one replica with `strategy: Recreate`, no persistent volume: Pulse messages are live readings.
- Service `mosquitto` is a LoadBalancer on `192.168.10.117:1883` with `externalTrafficPolicy: Local`, so the Pulse's own IP reaches the pod. Plain MQTT with a login, no TLS (shortcut: plain MQTT on the LAN, move to 8883 with a cert-manager certificate if the Pulse accepts a custom CA).
- Init container `config` copies Secret `mosquitto-config` into an in-memory `/mosquitto/config` as regular files owned by `mosquitto`, mode 0400, because Mosquitto 2.1 refuses a symlinked password file and Secret volume files are symlinks.
- Secret `mosquitto-config` (SOPS) holds `mosquitto.conf` with the bridge, the password file, the ACL and the Pulse's certificate, key and CA. It also holds the Pulse device ID, which stays out of plain Git. Users: `tibber_pulse` (the Pulse) and `home_assistant`. Their passwords are login items in the homelab vault.
- The bridge `tibber` connects to Tibber's AWS IoT endpoint on 8883 with the Pulse's certificate and reports its state on `homelab/tibber-bridge/state` (1 connected, 0 not). Home Assistant shows it as `binary_sensor.tibber_bridge`.
- NetworkPolicy `mosquitto` allows 1883 only from the Pulse (`192.168.10.230`, a DHCP reservation in the router), Home Assistant and the blackbox exporter, and egress only to DNS and TCP 8883 on public addresses.
- Custom integrations: `tibber_pulse_mqtt` ([mrhedstrom/ha-tibber-pulse-mqtt](https://github.com/mrhedstrom/ha-tibber-pulse-mqtt)) reads the Pulse; `zaptec` ([custom-components/zaptec](https://github.com/custom-components/zaptec)) shows the charger, for monitoring only: no automation controls it, Tibber plans the charging. The `configuration` init container runs `install_component` for each, installing the release pinned in `<NAME>_VERSION` into `/config/custom_components` when the installed version differs, and checking it against `<NAME>_SHA256`. A failed download keeps the installed copy, so Home Assistant starts without internet. A wrong checksum stops the init container. Renovate bumps the version but cannot update the checksum, so its PR waits with the `needs-checksum` label until someone copies the release asset's sha256. Zaptec's Python requirements are already in the Home Assistant image.
- Zaptec's cloud has no coordinates for the installation, so `binary_sensor.garage_e_pavli_installation` reports latitude and longitude 0 and the Map shows it off West Africa. `homeassistant.customize` in `configuration.yaml` overrides both with `!secret home_latitude` and `!secret home_longitude` from `home-assistant-secrets` `secrets.yaml`, kept encrypted because this repository is public.
- Blackbox probe job `mosquitto-probe` raises `MosquittoProbeFailed` when the broker stops answering.

If the broker or the cluster is down, Tibber gets no Pulse data and the e-Golf charging loses it too.

Rollback: reset the Pulse (hold its button 5 seconds) and run the normal Pulse setup in the Tibber app, so it talks to Tibber directly again. Then revert the broker in Git. Scaling `mosquitto` to zero by hand does not stick, because `app-home-assistant` self-heals.

The Pulse certificate, key and topics come from the Pulse's setup page at `http://10.133.70.1`, reached on its own Wi-Fi after a reset. The upstream README of `mrhedstrom/ha-tibber-pulse-mqtt` describes the procedure. If Tibber rotates the device certificate, the bridge fails and the files must be extracted again.

## e-Golf battery to Tibber

Design: `docs/superpowers/specs/2026-11-15-egolf-soc-to-tibber-design.md`. The garage ESP32 (`3-devices/esphome/`) reads the e-Golf's battery level into `sensor.egolf_battery`. Tibber plans the charging on the Zaptec but has no official way to receive the level, so the package `egolf.yaml` (ConfigMap, copied to `/config` on every start) sends it through Tibber's private app API, the same calls the Tibber app makes.

- `rest_command.tibber_app_login` posts the URL-encoded login from `!secret tibber_login_body` to `https://app.tibber.com/login.credentials`; `rest_command.tibber_set_battery_level` sends `setVehicleSettings` with `offline.vehicle.batteryLevel` to `https://app.tibber.com/v4/gql`. The car is a Tibber "offline vehicle"; its ID is the UUID inside the HA Tibber device identifier `offline vehicle:<uuid>`.
- `script.egolf_push_to_tibber` logs in, sends the level, and records the result in `input_datetime.egolf_tibber_last_push` and `input_text.egolf_tibber_last_result`. It keeps one trace, which holds the short-lived Tibber token.
- Automation `egolf_push_to_tibber` runs it on every fresh reading, plugged in or not, so the level read on arrival reaches Tibber. At plug-in it resends the last reading, in case Tibber ignores a level sent while unplugged.
- Notifications: a failed push (`egolf_tibber_push`), 30 minutes of charging without a fresh reading (`egolf_battery_stale`), and the car's 12 V below 12.0 V for 10 minutes (`egolf_12v_low`).
- Secrets in `home-assistant-secrets` `secrets.yaml`: `tibber_login_body`, `tibber_vehicle_id`, `tibber_home_id`. The login comes from the homelab vault item for the Tibber account; to change it, rebuild `tibber_login_body` with `urllib.parse.urlencode({"email": ..., "password": ...})`, re-encrypt and let Argo CD sync.
- The car stops charging at 80% by itself, and the Tibber app lets you set only the car's current level, not a target. So Tibber's target stays at 100% and the script sends the real level plus `100 - car_charge_limit` (80), capped at 100: car at 50% -> Tibber 70% -> Tibber plans 30%. The Tibber app shows the car 20 points high; `sensor.egolf_battery` shows the real level. If the car's limit changes, change `car_charge_limit` in `egolf.yaml`. Nothing in Home Assistant controls the charger.

If Tibber changes its app API, pushes fail, the notification shows it, and Tibber keeps the last level.

## Database

History goes to database `homeassistant`, owned by role `homeassistant`, in `postgres-cluster` (CNPG `Database` and `DatabaseRole` in `postgres-system`, both with reclaim policy `retain`). The role password is in Secret `homeassistant-db-role` in `postgres-system`.

`recorder.db_url` comes from `!secret recorder_db_url` in `/config/secrets.yaml`, mounted from Secret `home-assistant-secrets`. `recorder.db_max_retries: 100` retries a failed connection for about 5 minutes (3 seconds apart), so a Postgres failover or node drain at start does not leave Home Assistant without history until the next restart.

## Secrets

`secrets.enc.yaml` (SOPS) holds the five Secrets. The role password, the ESPHome login (user `admin`) and the Roborock admin password, protocol PIN and session secret are also stored as login items in the homelab Vaultwarden collection. To change a Roborock value, update its vault item, hash it with upstream's `roborock_local_server.security.hash_password`, re-encrypt `roborock-config` and let Argo CD sync; Reloader restarts the server. To change the role password, update its vault item, re-encrypt both Secrets that hold it with the same password and let Argo CD sync; CNPG sets the new password and Reloader restarts Home Assistant. To change the ESPHome password, update its vault item, re-encrypt `esphome-auth` and let Argo CD sync; Reloader restarts ESPHome. To change a Mosquitto password, update its vault item, regenerate its line in `passwd` inside `mosquitto-config` (Mosquitto `$7$` PBKDF2-SHA512), re-encrypt and let Argo CD sync; Reloader restarts Mosquitto. The Pulse password must then also be set on the Pulse's setup page.

## Recovery

- History: restore `postgres-cluster` from its CNPG backup.
- Configuration and UI-made objects: the `home-assistant-config` PVC. `configuration.yaml` comes back from Git on every start.
- Home Assistant's own backups (Settings, System, Backups) land in `/config/backups` on the same PVC. They do not include the Postgres history.
- ESPHome device YAML: the `esphome-config` PVC.
- Roborock device keys and state: the `roborock-data` PVC.

## Known limits

- Samsung TV power-on uses a Wake-on-LAN broadcast. The broadcast does not leave the pod network, so Home Assistant cannot turn the TV on.
- The ESPHome dashboard finds devices through mDNS, which does not reach a pod, so device online status may show unknown. Flashing over the air works by IP: each device YAML sets `wifi: use_address: <reserved IP>`.
