# e-Golf battery level to Tibber implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** HA reads the e-Golf's battery level from the vLinker MC+ through an ESP32 and pushes it to Tibber's private app API while the car is plugged in; the Zaptec charger appears in HA for monitoring.

**Architecture:** An ESP32 (ESPHome `ble_client`) by the charger talks ELM327 to the dongle, reads `ATRV` every minute and `22028C` only when the car is awake, and exposes battery %, 12 V and read time to HA. An HA package (`rest_command` login + GraphQL mutation, a script, an automation, two helpers) sends fresh reads to Tibber. Zaptec is a pinned custom integration installed by the existing init container.

**Tech Stack:** ESPHome (ESP32-WROOM-32D), Home Assistant 2026.11 YAML packages, Kustomize/KSOPS/SOPS, Argo CD, Renovate, Python `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-15-egolf-soc-to-tibber-design.md`

## Global constraints

- No Tibber login, vehicle ID or home ID in plain Git; they live in `home-assistant-secrets` `secrets.yaml` as `tibber_login_body`, `tibber_vehicle_id`, `tibber_home_id`.
- Nothing in HA starts, stops or limits the Zaptec.
- The ESP32 sends `22028C` only when `ATRV` reads above 12.7 V and the last good read is older than 10 minutes.
- A level read before the last unplug is never sent to Tibber.
- Zaptec `custom-components/zaptec` v0.8.8, asset `zaptec.zip`, SHA-256 `965646f226358707954e611cc0b4b0bc9db7ab21189b3b6c650c7e1e5addcb29`, flat zip with `"version": "0.8.8"`.
- Tibber app API: login `POST https://app.tibber.com/login.credentials` (form body), GraphQL `https://app.tibber.com/v4/gql`, mutation text as in the spec.
- Gates G1-G3 must pass before Tasks 2-3 merge. On a failed gate, stop and ask Spyros.

## Review focus

1. Plugging in sends last session's 80% to Tibber. `test_egolf_automation_freshness` asserts the condition compares read time with the plug-in time minus 30 minutes.
2. Login password with `&`, `+`, `=` breaks the form. The body is a pre-encoded secret; Task 2 step 4 builds it with `urllib.parse.urlencode`.
3. Car sleeping plugged in all night triggers a stale alert. `test_egolf_stale_alert` asserts the alert keys on `binary_sensor.e_pavli_charging`, not the plug.
4. A second custom integration copy-pastes the install block. Task 3 turns the block into one shell function used by both; the existing install test runs against both.
5. HA restart without internet with Zaptec installed. The existing offline-restart assertions in `test_custom_integration_install` cover the shared function.

---

### Task 1: Gates G1-G3 on the real car (spike, throwaway)

Owner steps: plug one ESP32 into the laptop by USB, later carry it to the charger socket; read the car display during G2.

- [ ] **Step 1 (G3):** From the laptop in the garage, `ip -4 addr` on the Wi-Fi it uses there and ping a cluster node. Expected: `192.168.10.x` and replies. Same network as `e-pavli`.
- [ ] **Step 2:** On the laptop, `python3 -m venv` + `pip install esphome`; flash a throwaway config: Wi-Fi from ESPHome's `secrets.yaml`, `api`, `ota`, `logger`, `esp32_ble_tracker` with `on_ble_advertise` logging name, address, service UUIDs. Expected: device online in HA's ESPHome integration.
- [ ] **Step 3 (G1):** With the ESP32 by the charger and the car in the garage, read the log. Expected: an advertisement from the vLinker (name contains `vLinker` or `IOS-Vlink`) with its service UUIDs. Record address and UUIDs in the ledger. Fail: no BLE advertisement -> stop.
- [ ] **Step 4 (G2):** Add a throwaway `ble_client` to that address; on connect write `ATZ`, `ATE0`, `ATSP6`, `ATH1`, `ATCRA 7ED`, `ATSH 7E5` (each `\r`-terminated, wait for `>`), then `ATRV` and `22028C`; log raw notifications. Run with the ignition on, and while charging. Expected: `ATRV` > 13.0 V in both; a `62 02 8C xx` reply; `xx / 2.5 * 51 / 46 - 6.4` within 1 point of the car's display. Also with the car asleep and unplugged: `ATRV` < 13.0 V. Record the values. Fail -> stop.

### Task 2: ESPHome device config

**Files:**
- Create: `3-devices/esphome/egolf-obd.yaml`, `3-devices/esphome/README.md` (what runs where, how to compile and update), `3-devices/esphome/tools/devices.test.py`
- Modify: `.github/workflows/ci.yml` (run `devices.test.py` next to the other unit tests), `2-k3s/36.home-assistant/README.md` (one line pointing to `3-devices/esphome`)

**Interfaces:**
- Consumes: G1 address and UUIDs (from the ledger; the MAC goes into ESPHome `secrets.yaml` as `vlinker_mac`, not Git).
- Produces: HA entities `sensor.egolf_battery` (%), `sensor.egolf_12v` (V), `sensor.egolf_battery_read_at` (timestamp).

- [ ] **Step 1: Write the failing test** `test_egolf_esphome_config` (YAML loaded with a loader that turns `!secret`/`!lambda` into tagged tuples): `ble_client[0].mac_address == ("secret", "vlinker_mac")`; `wifi.ssid` and `wifi.password` are `!secret`; an `interval` of `60s`; the lambda text contains `"ATRV"`, `"22028C"`, `12.7` and `600` (seconds); sensors named `Battery`, `12V`, `Battery read at` exist; no `tibber` string in the file; `time` platform `homeassistant` present.
- [ ] **Step 2: Run** `python3 3-devices/esphome/tools/devices.test.py`. Expected: fails (file missing).
- [ ] **Step 3: Write the config.** `esphome: name: egolf-obd`, `esp32: board: esp32dev`, `api` with `!secret` key, `ota`, `wifi` with `use_address: <reserved IP>` (Spyros reserves one in the Archer AX90; the IP is not secret), `time: homeassistant`, `ble_client`, a `text_sensor`/buffer for notifications, the 60 s `interval` with the ELM327 exchange in a lambda following the G2 sequence, `sensor` template outputs. Copy into the ESPHome dashboard and install over the air.
- [ ] **Step 4: Run the tests.** Expected: pass. Live: the three entities exist in HA; `12V` updates every minute; `Battery` updates on a car start.

### Task 3: Shared custom-integration installer + Zaptec

**Files:**
- Modify: `2-k3s/36.home-assistant/home-assistant.yaml` (init script, env), `.github/renovate.json`, `2-k3s/36.home-assistant/tools/manifests.test.py`, `README.md`

- [ ] **Step 1: Write the failing tests.** Extend `test_custom_integration_pin` to require env `ZAPTEC_VERSION` `0.8.8` with the renovate marker `depName=custom-components/zaptec` above it and `ZAPTEC_SHA256` as pinned. Turn `test_custom_integration_install` into a loop over `(tibber_pulse_mqtt, TIBBER_PULSE_MQTT_*)` and `(zaptec, ZAPTEC_*)` with the same five cases. `test_renovate_zaptec`: a packageRule for `custom-components/zaptec` with `automerge: false` and `needs-checksum`.
- [ ] **Step 2: Run.** Expected: new assertions fail.
- [ ] **Step 3: Implement** a shell function `install_component <domain> <owner/repo> <asset> <version> <sha256>` in the init script with the current block's behaviour (skip when version matches, fail on checksum mismatch, keep the old copy on download failure), called twice. Renovate: extend the existing custom manager's file match if it already covers the file; add the packageRule.
- [ ] **Step 4: Run tests.** Expected: pass. Mutation check: `|| true` after `sha256sum` fails the install test for both.

### Task 4: HA package and Tibber secrets

**Files:**
- Modify: `2-k3s/36.home-assistant/home-assistant.yaml` (ConfigMap: `configuration.yaml` gains `homeassistant: packages: egolf: !include egolf.yaml`; new key `egolf.yaml`; init copies it like `configuration.yaml`), `secrets.enc.yaml`, `tools/manifests.test.py`, `README.md`

**Interfaces:**
- Consumes: Task 2 entities; `binary_sensor.e_pavli_plug`, `binary_sensor.e_pavli_charging`.
- Produces: `script.egolf_push_to_tibber`, `input_datetime.egolf_tibber_last_push`, `input_text.egolf_tibber_last_result`.

- [ ] **Step 1: Write the failing tests**
  - `test_egolf_rest_commands`: `rest_command.tibber_app_login` has `method: post`, url `https://app.tibber.com/login.credentials`, `payload == ("secret", "tibber_login_body")`, content type form; `rest_command.tibber_set_battery_level` posts to `https://app.tibber.com/v4/gql` with the exact mutation text from the spec and a header template using `token`.
  - `test_egolf_script`: `variables` use `!secret tibber_vehicle_id` / `tibber_home_id`; `trace.stored_traces == 1`; calls login then set with `response_variable`; on non-200 or GraphQL `errors` it writes `input_text` and creates a persistent notification.
  - `test_egolf_automation_freshness`: triggers on `sensor.egolf_battery_read_at` and on `binary_sensor.e_pavli_plug` to `on`; condition plug `on` and read time `>=` plug `last_changed` minus 30 minutes.
  - `test_egolf_stale_alert`: an automation keyed on `binary_sensor.e_pavli_charging` on for 30 minutes without a newer read creates a notification.
  - `test_ha_init_keeps_ui_files`: init also copies `egolf.yaml`.
- [ ] **Step 2: Run.** Expected: new tests fail.
- [ ] **Step 3: Implement** the package YAML.
- [ ] **Step 4: Secrets.** In one `vault run` per value: build `tibber_login_body = urlencode({"email": ..., "password": ...})` from the vault item for Spyros's Tibber login; log in once with it and query the app API for `me { homes { id } }` and the vehicle ID (query shape from `tibber_soc_updater` `QUERY_GET_VEHICLE`); write the three keys into `secrets.yaml` inside `home-assistant-secrets` via SOPS; print only success or failure.
- [ ] **Step 5: Run tests and the SOPS hook.** Expected: pass, hook rc 0.

### Task 5: Review, merge, live check

- [ ] **Step 1:** Review-gate on the branch; PR; CI green; merge.
- [ ] **Step 2:** After sync, with the car plugged in and charging: `input_text.egolf_tibber_last_result` reads success, and the Tibber app shows `sensor.egolf_battery` rounded + 20, capped at 100.
- [ ] **Step 3:** Spyros adds the Zaptec integration in the UI with his Zaptec login; its entities appear; no automation references them.
- [ ] **Step 4:** Tibber's target stays 100%; the script sends the real level + (100 - 80), capped at 100 (the car stops at 80% by itself).
