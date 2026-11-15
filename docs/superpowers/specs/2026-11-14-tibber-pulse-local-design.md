# Tibber Pulse in Home Assistant, read locally

## Intent

Spyros wants his house's power consumption in Home Assistant (HA) from the Tibber Pulse on his meter, without depending on the internet or Tibber's servers. He also wants Tibber in HA for prices and cost. Tibber's smart charging of the e-Golf uses the Pulse today, so Tibber must keep receiving the Pulse data.

## Goals

- HA shows live Pulse data (power, today's energy, per-phase current and voltage, meter reading) read from our own MQTT broker. It keeps working while the internet or Tibber is down.
- The Pulse data still reaches Tibber's cloud, so the Tibber app and the e-Golf charging keep working when the internet is up.
- HA has the official Tibber integration for prices, cost and the hourly Energy dashboard statistics.
- Broker and bridge health are visible: a failed broker raises an alert, and HA shows whether the bridge to Tibber is connected.

## Non-goals

- Replacing the Pulse with other HAN reader hardware.
- HACS. The one custom integration is installed from a pinned GitHub release.
- MQTT for other devices. The broker serves the Pulse only, though nothing stops later use.
- Running the broker outside k3s.

## Anti-goals

- Breaking the e-Golf charging for longer than the switchover itself.
- Device certificates, the private key, MQTT passwords, the Pulse device ID or Tibber tokens in Git outside SOPS, in chat or in logs. This repository is public.

## Constraints

- Pulse model is HAN or KM; Spyros has not confirmed which. The custom integration is tested mostly on P1 and HAN. Forwarding to Tibber works for both, because the bridge passes messages through unchanged.
- The Pulse is a Wi-Fi client on the main LAN (an Espressif device in the `192.168.10.0/24` range, found through its Pi-hole lookups of the Tibber AWS IoT endpoint and `fw.tibber.com` on 2026-11-14). It reaches the broker IP on the same subnet. In normal mode it has no open TCP port (80, 443, 8080 and 1883 checked), so it cannot be read without pointing it at our broker.
- The Pulse accepts one MQTT broker. Once it points to ours, our broker is in the path to Tibber (risk R1).
- AWS IoT allows one connection per client ID. The bridge and the Pulse must not both connect to AWS with the Pulse's client ID at the same time, or AWS disconnects them in turn.
- HA's MQTT broker settings and the Tibber integration are UI-only. Those are owner steps, as are the Pulse reset, its setup page and anything that needs the Tibber login.

## Approaches

- B1. Mosquitto in k3s with a two-way bridge to Tibber's AWS IoT endpoint, plus the custom integration `mrhedstrom/ha-tibber-pulse-mqtt`. Chosen by Spyros: reuses the cluster patterns (LoadBalancer IP, Argo CD, SOPS) and needs no hardware. Cost: the cluster becomes part of the charging chain.
- B2. Keep the Pulse on Tibber and add a second, open HAN reader for HA through a splitter. Rejected: new hardware, and whether the meter powers two readers was not verified.
- B3. Mosquitto on a separate always-on box. Rejected: a second place to run and monitor, for an uptime gain that is unproven.
- Rejected earlier: `marq24/ha-tibber-pulse-local` (Pulse IR with Bridge only), and the Tibber cloud integration alone (not local).

## Design

### Broker

- Deployment `mosquitto` in namespace `home-assistant`, image `eclipse-mosquitto` pinned by tag and digest in `kustomization.yaml` `images:`, one replica, `strategy: Recreate`. No persistence: Pulse messages are live readings, nothing to keep.
- Service `mosquitto`, type LoadBalancer, `loadBalancerIP: 192.168.10.117` (free in the live cluster on 2026-11-14; confirm again before merge, else next free in `range-global`), `externalTrafficPolicy: Local`, TCP 1883. The Pulse uses the IP, so no DNS record.
- Plain MQTT on 1883 with username and password, not TLS. Our call: the Pulse's TLS support with our own CA is untested, and the Wi-Fi link is already WPA-encrypted. `shortcut: plain MQTT on the LAN, move to 8883 with a cert-manager certificate if the Pulse accepts a custom CA.`
- Users in a Mosquitto password file: `tibber_pulse` (the Pulse) and `home_assistant` (HA). ACLs use the exact device topics, because `+` must fill a whole topic level and `tibber-pulse-+/publish` would match only that literal name: `tibber_pulse` may write `tibber-pulse-<device id>/publish` and read `tibber-pulse-<device id>/receive`; `home_assistant` may read `tibber-pulse-<device id>/publish` and `homelab/tibber-bridge/state`. The ACL file lives in the SOPS Secret, so the device ID stays out of plain Git. No anonymous access.
- Bridge `connection tibber` to the endpoint from the Pulse's `mqtt_url`, port 8883, TLS 1.2, with the Pulse's CA, certificate and key, client ID `tibber-pulse-<device id>`, `cleansession true`. The Pulse's own `mqtt_topic` is the AWS IoT basic-ingest topic `$aws/rules/push_to_kinesis/tibber-pulse-<device id>/publish` (seen on its setup page, firmware 1.2.5). Locally the Pulse publishes to `tibber-pulse-<device id>/publish`, and the bridge renames the topic on the way out with `topic publish out 1 tibber-pulse-<device id>/ $aws/rules/push_to_kinesis/tibber-pulse-<device id>/`, so `$` topics never sit on the local broker and HA uses the plain topic. Commands come back unchanged with `topic tibber-pulse-<device id>/receive in 1`. Use `notifications true`, `notifications_local_only true` and `notification_topic homelab/tibber-bridge/state`, so Mosquitto publishes the bridge state (1 connected, 0 not) on a fixed local topic that does not contain the device ID, and never sends it to AWS.
- Secret `mosquitto-config` (SOPS, in `secrets.enc.yaml`) holds `mosquitto.conf` (with the bridge block, which names the device ID), the password file, the ACL file and the three certificate files. Mounted read-only. Reloader restarts Mosquitto when it changes.
- NetworkPolicy `mosquitto`: ingress TCP 1883 only from the Pulse at `192.168.10.230/32` (Spyros reserves that address for the Pulse in the Archer AX90's DHCP server before merge), from pods `app: home-assistant`, and from the blackbox exporter (namespace `observability`, label `app.kubernetes.io/name: prometheus-blackbox-exporter`). `externalTrafficPolicy: Local` keeps the Pulse's source IP, so the `ipBlock` rule matches it. Egress: DNS to kube-dns, TCP 8883 to `0.0.0.0/0` except RFC 1918 ranges.
- Resources: small requests (for example 10m CPU, 32Mi memory). Builder's call.

### Home Assistant

- Custom integration `tibber_pulse_mqtt`, pinned to a release version (v0.4.7 now) in Git. The existing `configuration` init container installs it into `/config/custom_components/tibber_pulse_mqtt` when the installed version differs from the pinned one, from the GitHub release archive with a pinned SHA-256. If the download fails and a copy is already installed, HA starts with the old copy; offline restarts must work. A Renovate custom manager bumps the version with `automerge: false` and the `needs-checksum` label, as in the existing #997 rule, because Renovate cannot compute the archive hash. A mismatched hash must fail the init container loudly, not keep the old copy silently. The integration's Python requirements (`protobuf>=4.25.0`, `paho-mqtt>=1.6.1`) are already in the HA 2026.11.1 image (protobuf 7.36.0, paho-mqtt 2.1.0, checked in the running pod), so HA installs nothing at start.
- HA reaches Mosquitto by the in-cluster Service DNS name on 1883. Add an egress rule only if HA gets an egress NetworkPolicy; today it has none.
- `configuration.yaml` gains an MQTT binary sensor on `homelab/tibber-bridge/state` (`payload_on: "1"`, `payload_off: "0"`), named "Tibber bridge", device class `connectivity`. MQTT entities in YAML are supported; the broker connection itself is UI-only.
- Owner adds in the UI: the MQTT integration (broker `mosquitto.home-assistant.svc.cluster.local`, user `home_assistant`), then "Tibber Pulse MQTT" with the exact topic `tibber-pulse-<device id>/publish`, since the ACL refuses the integration's wider default subscription.
- Official Tibber integration: owner creates a Data API client at `data-api.tibber.com/clients/manage` with redirect URI `https://my.home-assistant.io/redirect/oauth`, adds it under Application credentials, then adds Tibber with the personal access token Spyros keeps in the homelab vault. The cloud integration also creates Pulse sensors; the local ones are the ones to use in dashboards and automations. Energy dashboard: grid consumption `tibber:energy_consumption_<HOMEID>`, cost `tibber:energy_totalcost_<HOMEID>`.

### Observability

- Blackbox TCP probe `mosquitto` on `192.168.10.117:1883` in `blackbox-values.yaml` with a new `tcp_connect` module and its own job label `mosquitto-probe`, following the `vaultwarden-probe` relabel pattern. It must not keep the default `tls-probe` job: `TLSProbeFailed` fires on any successful probe without a certificate. A new rule `MosquittoProbeFailed` in `custom-alerts.yaml`, with tests in `custom-alerts-tests.yaml`.
- HA's "Tibber bridge" binary sensor shows whether Tibber gets the data. An HA notification automation on it is left to Spyros.
- Pulse data freshness in HA is visible on the sensors' last-updated time. No extra alert in this change.

### Secrets and owner steps

1. Spyros resets the Pulse, disconnects the Pulse Power-up in the Tibber app, starts setup with a wrong Wi-Fi password, joins the Pulse's own Wi-Fi and opens `http://10.133.70.1`. He saves `ca_cert`, `certificate`, `private_key`, `mqtt_url`, `mqtt_topic`, `mqtt_topic_sub` (firmware 1.2.5 has no `update_url` field) into one new homelab vault item (certificates as attachments, the rest as fields) and tells the agent its name. Vault item names stay out of tracked files. He then fixes the Wi-Fi password and applies, so the Pulse goes back to Tibber directly. Procedure: the upstream README, "Extract Tibber Pulse Certificates".
2. The agent generates both MQTT passwords inside the command that consumes them, stores each as a homelab vault item, and builds Secret `mosquitto-config` from the vault into `secrets.enc.yaml` without printing any value.
3. After merge and sync (step 4 below), Spyros resets the Pulse again without disconnecting the Power-up, and on its setup page sets `mqtt_url` to `tibber_pulse:<password>@192.168.10.117`, `mqtt_port` 1883, `mqtt_topic` to `tibber-pulse-<device id>/publish` (not the saved `$aws/...` topic), and `mqtt_topic_sub` to the saved `tibber-pulse-<device id>/receive`.

### Rollout order

1. Owner step 1 (certificates), and the DHCP reservation for the Pulse. The Pulse keeps talking to Tibber.
2. Build and review the manifests with the bridge configured. Hold the merge until Spyros is at the Pulse: `app-home-assistant` syncs automatically, so the merge starts the switchover.
3. Merge and let Argo CD sync. From here until step 4, the bridge and the Pulse share the client ID and AWS keeps dropping one of them. Keep this gap to minutes.
4. Owner step 3 (point the Pulse at our broker). Verify: Mosquitto logs a connected bridge, the Tibber app shows live data, HA gets sensors after the integration is added.
5. Owner adds the MQTT, Tibber Pulse MQTT and Tibber integrations.

### Rollback

Reset the Pulse and run the normal setup in the Tibber app. The Pulse talks to Tibber directly again. Then revert the change in Git. Scaling Mosquitto to zero by hand does not stick, because `app-home-assistant` has `selfHeal: true`.

## Risks

- R1. The cluster, Mosquitto or the LAN path down means Tibber gets no Pulse data. What Tibber then does with the e-Golf charging is unknown.
- R2. The custom integration is unofficial and small (13 stars). If a Tibber firmware change breaks decoding, HA loses local data while the bridge keeps Tibber working.
- R3. Tibber could revoke or rotate the device certificate or move the AWS endpoint. The bridge then fails, and the fix is to extract the certificates again.
- R4. Firmware 1.2.5 shows no `update_url`. Whether the Pulse still gets firmware updates while it points at our broker is not verified.

## Left to the builder

- Mosquitto image tag and digest, resource numbers, container security context.
- Exact init container script for the custom integration and the Renovate manager regex.
- Test additions in `2-k3s/36.home-assistant/tools/manifests.test.py` for the new Deployment, Service, NetworkPolicy and the ConfigMap change.
- README section for the broker, the bridge and the rollback.
