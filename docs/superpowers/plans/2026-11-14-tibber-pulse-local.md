# Tibber Pulse local implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read the Tibber Pulse in Home Assistant from our own Mosquitto broker, and forward its data to Tibber's cloud so the Tibber app and the e-Golf charging keep working.

**Architecture:** Mosquitto runs in namespace `home-assistant` on LoadBalancer `192.168.10.117:1883`. The Pulse publishes to it; a two-way bridge forwards the Pulse topics to Tibber's AWS IoT endpoint with the Pulse's own certificate. HA reads the broker through the custom integration `tibber_pulse_mqtt`, installed from a pinned GitHub release by the existing init container.

**Tech Stack:** Kustomize, KSOPS/SOPS, Argo CD, kube-vip, Reloader, Mosquitto 2.x, Home Assistant 2026.11, Prometheus blackbox exporter, promtool, Python `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-14-tibber-pulse-local-design.md`

## Where this runs

A homelab T3 thread. Branch `t3/tibber-home-assistant-integration`. Review-gate runs before the commit. Rebase onto `origin/main` before committing. No co-author lines. One PR for Tasks 1 to 4; it stays unmerged until Task 5 step 1.

## Global constraints

- Broker IP `192.168.10.117`, TCP 1883, `externalTrafficPolicy: Local`. Recheck it is free before the PR merges.
- Pulse IP `192.168.10.230/32`, reserved in the Archer AX90 DHCP server by Spyros.
- Image `eclipse-mosquitto`, latest 2.x release, pinned `tag@sha256:<digest>` in `kustomization.yaml` `images:`.
- Custom integration `mrhedstrom/ha-tibber-pulse-mqtt` v0.4.7, asset `tibber_pulse_mqtt.zip`, SHA-256 `a3b5daf5eaf14cdcf5b82699b549f3eea0c6470e68ea9c5977d65e283b3f8a55`. The zip holds the integration files at its root (`manifest.json`, `__init__.py`, ...).
- MQTT users `tibber_pulse` and `home_assistant`. Bridge state topic `homelab/tibber-bridge/state`.
- Plain Git never holds the Pulse device ID, certificates, key, passwords, password hashes or vault item names. All of those live in `secrets.enc.yaml`.
- No `hostNetwork`. Every Deployment `strategy: Recreate`, one replica.

## Review focus

1. Pulse traffic from a NATed source IP would miss the `192.168.10.230/32` rule. `test_mosquitto_service` pins `externalTrafficPolicy: Local`.
2. HA restart with no internet must still start. `test_custom_integration_install` asserts the script skips the download when the pinned version is installed and never fails on a failed download when a copy exists.
3. A stale SHA-256 after a Renovate bump. The script exits non-zero on mismatch (test asserts `sha256sum -c` without `|| true`), and the Renovate rule blocks automerge.
4. The default `tls-probe` job on a TCP target fires `TLSProbeFailed` forever. Task 4 relabels to `mosquitto-probe` and its test asserts the relabel rule exists.
5. Device ID leaking into plain Git through the bridge topic. `test_no_device_id_in_plain_files` asserts no `tibber-pulse-` string with a hex suffix in any non-`.enc.yaml` file under the folder.

---

### Task 1: Mosquitto broker manifests

**Files:**
- Create: `2-k3s/36.home-assistant/mosquitto.yaml` (Deployment and Service `mosquitto`)
- Modify: `2-k3s/36.home-assistant/network-policy.yaml` (NetworkPolicy `mosquitto`)
- Modify: `2-k3s/36.home-assistant/kustomization.yaml` (resource `mosquitto.yaml`, image pin)
- Modify: `2-k3s/36.home-assistant/README.md` (section "Tibber Pulse broker": what runs, owner steps, rollback, R1)
- Test: `2-k3s/36.home-assistant/tools/manifests.test.py`

**Interfaces:**
- Produces: Service `mosquitto.home-assistant.svc.cluster.local:1883`; Deployment mounts Secret `mosquitto-config` keys `mosquitto.conf`, `passwd`, `acl`, `ca.crt`, `client.crt`, `client.key`, all at `/mosquitto/config/`, read-only, `defaultMode: 0440`.

- [ ] **Step 1: Write the failing tests**
  - `test_mosquitto_deployment`: one replica, `strategy: Recreate`, image `eclipse-mosquitto`, no `hostNetwork`, `automountServiceAccountToken: false`, annotation `secret.reloader.stakater.com/reload: mosquitto-config`, volume from Secret `mosquitto-config`, container port 1883, `kustomization.yaml` images entry for `eclipse-mosquitto` matches `^2\.[0-9.]+@sha256:[0-9a-f]{64}$`.
  - `test_mosquitto_service`: `type: LoadBalancer`, `loadBalancerIP: 192.168.10.117`, `externalTrafficPolicy: Local`, ports exactly `[1883/TCP]`, selector `app: mosquitto`.
  - `test_mosquitto_network_policy`: policy types `Ingress` and `Egress`; ingress port 1883 only, from exactly `ipBlock 192.168.10.230/32`, pods `app: home-assistant`, and namespace `observability` pods `app.kubernetes.io/name: prometheus-blackbox-exporter`; egress to kube-dns UDP/TCP 53, and TCP 8883 to `0.0.0.0/0` with `except` `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`.
  - `test_no_device_id_in_plain_files`: no non-`.enc.yaml` file under `2-k3s/36.home-assistant` matches `tibber-pulse-[0-9a-f]{8,}`.

- [ ] **Step 2: Run** `python3 2-k3s/36.home-assistant/tools/manifests.test.py -v`. Expected: the four new tests fail, the rest pass.

- [ ] **Step 3: Write the manifests.** Pod `securityContext`: `runAsUser: 1883`, `runAsGroup: 1883`, `fsGroup: 1883`, `runAsNonRoot: true`. Command `mosquitto -c /mosquitto/config/mosquitto.conf`. Requests 10m CPU, 32Mi; limit 128Mi. TCP readiness probe on 1883.

- [ ] **Step 4: Run the tests.** Expected: all pass.

### Task 2: Secret `mosquitto-config`

Blocked on Spyros: the Pulse vault item from spec "Secrets and owner steps" step 1, and its name.

**Files:**
- Modify: `2-k3s/36.home-assistant/secrets.enc.yaml` (new Secret `mosquitto-config`, namespace `home-assistant`)

**Interfaces:**
- Consumes: the mount paths from Task 1.
- Produces: Secret keys `mosquitto.conf`, `passwd`, `acl`, `ca.crt`, `client.crt`, `client.key`. Two new homelab vault items holding the `tibber_pulse` and `home_assistant` passwords.

- [ ] **Step 1: Generate passwords** inside `vault create` commands (skill `vaultwarden-secrets`, write contract first). Never print them.
- [ ] **Step 2: Build the Secret** in one `vault run` that reads the Pulse item and both password items, writes a private temp file and runs `sops set` or `sops -e -i` on `secrets.enc.yaml`. Contents:
  - `passwd`: Mosquitto 2 `$7$` PBKDF2-SHA512 hashes for both users, made with Python `hashlib`.
  - `acl`: `user tibber_pulse` → `topic write tibber-pulse-<id>/publish`, `topic read tibber-pulse-<id>/receive`; `user home_assistant` → `topic read tibber-pulse-<id>/publish`, `topic read homelab/tibber-bridge/state`.
  - `mosquitto.conf`: `listener 1883`, `allow_anonymous false`, `password_file`, `acl_file`, `persistence false`, `log_dest stdout`, and the bridge block from the spec's "Broker" section, with `address` from the Pulse's `mqtt_url`, `bridge_cafile /mosquitto/config/ca.crt`, `bridge_certfile /mosquitto/config/client.crt`, `bridge_keyfile /mosquitto/config/client.key`.
  - The three certificate files from the Pulse item.
- [ ] **Step 3: Verify** `vault run --name sops-age-k3s-cluster ... kustomize build --enable-alpha-plugins --enable-exec 2-k3s/36.home-assistant` writes to a private temp file; check with Python that Secret `mosquitto-config` has exactly the six keys and that `mosquitto.conf` contains `notifications_local_only true` and `notification_topic homelab/tibber-bridge/state`. Print only pass or fail. Delete the temp file.

### Task 3: Home Assistant side

**Files:**
- Modify: `2-k3s/36.home-assistant/home-assistant.yaml` (init container `configuration`, ConfigMap `configuration.yaml`)
- Modify: `.github/renovate.json` (custom regex manager and packageRule for `mrhedstrom/ha-tibber-pulse-mqtt`)
- Test: `2-k3s/36.home-assistant/tools/manifests.test.py`

**Interfaces:**
- Produces: `/config/custom_components/tibber_pulse_mqtt/manifest.json` with `"version": "0.4.7"`; HA entity `binary_sensor.tibber_bridge`.

- [ ] **Step 1: Write the failing tests**
  - `test_custom_integration_install`: init container `configuration` env `TIBBER_PULSE_MQTT_VERSION` = `0.4.7`, with `# renovate: datasource=github-releases depName=mrhedstrom/ha-tibber-pulse-mqtt` on the line above its value in the file, env `TIBBER_PULSE_MQTT_SHA256` = the pinned hash; the script references `releases/download/v${TIBBER_PULSE_MQTT_VERSION}/tibber_pulse_mqtt.zip`, checks the installed `manifest.json` version before downloading, runs `sha256sum -c` not followed by `|| true`, and keeps its existing lines for `configuration.yaml`, `.storage/http` and the three `[ -e ... ] ||` files.
  - `test_bridge_binary_sensor`: `configuration.yaml` (parsed with `HomeAssistantLoader`) has `mqtt.binary_sensor` with one entry: `name: Tibber bridge`, `state_topic: homelab/tibber-bridge/state`, `payload_on: "1"`, `payload_off: "0"`, `device_class: connectivity`.
  - `test_renovate_tibber_pulse_mqtt`: `.github/renovate.json` has a `customManagers` entry whose `managerFilePatterns` matches `2-k3s/36.home-assistant/home-assistant.yaml`, and a `packageRules` entry for `mrhedstrom/ha-tibber-pulse-mqtt` with `automerge: false` and labels `["renovate", "needs-checksum"]`.
- [ ] **Step 2: Run the tests.** Expected: the three new tests fail.
- [ ] **Step 3: Implement.** Script logic, in the existing `sh -c` block after the current lines, with `set -e` kept:
  1. If `/config/custom_components/tibber_pulse_mqtt/manifest.json` contains `"version": "$TIBBER_PULSE_MQTT_VERSION"`, skip.
  2. Else `wget -T 30` the zip to `/tmp`. On download failure, print a warning and continue: HA starts with the old copy, or without the integration on a fresh volume.
  3. `sha256sum -c` against the pinned hash; mismatch exits non-zero.
  4. Unzip into a temp dir under `/config/custom_components`, then replace `tibber_pulse_mqtt` by `mv`.
  The renovate manager follows the `#997` cliproxy shape; the packageRule copies its `automerge: false` and labels.
- [ ] **Step 4: Run the tests and** `python3 -m json.tool .github/renovate.json > /dev/null`. Expected: all pass.

### Task 4: Broker probe and alert

**Files:**
- Modify: `2-k3s/10.observability/blackbox-values.yaml`
- Modify: `2-k3s/10.observability/alertmanager-config/custom-alerts.yaml`
- Modify: `2-k3s/10.observability/alertmanager-config/custom-alerts-tests.yaml`

- [ ] **Step 1: Write the failing promtool tests** in `custom-alerts-tests.yaml`: `probe_success{job="mosquitto-probe",instance="192.168.10.117:1883"}` at 0 for 6m fires `MosquittoProbeFailed` with `severity: critical`, `component: home-assistant`; at 1 it stays silent; `up{job="mosquitto-probe"} == 0` for 6m fires it.
- [ ] **Step 2: Run** the CI promtool steps from `.github/workflows/ci.yml` lines 296-314 locally (`bash .github/scripts/install-ci-tools.sh promtool` first). Expected: the new tests fail, the rest pass.
- [ ] **Step 3: Implement.** Module `tcp_connect` (`prober: tcp`, `timeout: 5s`, `tcp.preferred_ip_protocol: ip4`); target `{name: mosquitto, url: "192.168.10.117:1883", module: tcp_connect}`; relabel `__param_target` regex `192\.168\.10\.117:1883` to job `mosquitto-probe`, after the `tls-probe` default as the `vaultwarden-probe` rule does. Rule `MosquittoProbeFailed` in a new group `home-assistant`: `probe_success{job="mosquitto-probe"} == 0 or up{job="mosquitto-probe"} == 0`, `for: 5m`. Description names the Tibber bridge and the e-Golf charging impact, and the rollback in `2-k3s/36.home-assistant/README.md`.
- [ ] **Step 4: Run the promtool steps again.** Expected: all pass.

### Task 5: Rollout (live, after review-gate and PR)

- [ ] **Step 1:** Spyros confirms he is at the Pulse and the DHCP reservation exists. Recheck `192.168.10.117` is free. Merge.
- [ ] **Step 2:** Argo CD syncs `app-home-assistant` and `observability`. Expected: `kubectl -n home-assistant get deploy mosquitto` ready; `kubectl -n home-assistant logs deploy/mosquitto` shows the bridge `tibber` connecting.
- [ ] **Step 3:** Spyros does spec owner step 3 (point the Pulse at `192.168.10.117`). Expected: Tibber app shows live data within minutes; broker log shows client `tibber_pulse` connected.
- [ ] **Step 4:** Check auth with `kubectl exec deploy/mosquitto -- mosquitto_sub -u home_assistant -t homelab/tibber-bridge/state -C 1 -W 10` inside `vault run` with the password from its vault item. Write the payload to a private temp file. Expected: `1`.
- [ ] **Step 5:** Spyros adds the MQTT, Tibber Pulse MQTT (exact topic) and Tibber integrations in the HA UI. Expected: `binary_sensor.tibber_bridge` is `on`; a power sensor from `tibber_pulse_mqtt` updates every few seconds; Prometheus `probe_success{job="mosquitto-probe"}` is 1.
