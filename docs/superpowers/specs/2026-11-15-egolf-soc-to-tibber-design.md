# e-Golf battery level to Tibber

## Intent

Tibber plans the e-Golf's charging on the Zaptec charger, but it does not know the car's real battery level. The e-Golf has no manufacturer API, so today Spyros sets the level by hand with a slider in the Tibber app. He wants Home Assistant (HA) to read the real level from the car and send it to Tibber automatically, so Tibber charges to 80% at the cheapest hours. Tibber stays the planner: Spyros chose it as the safer option, and if the automation fails Tibber falls back to the last value, which is today's behaviour.

## Goals

- HA has a sensor with the e-Golf's battery level in percent, read from the car through the Vgate vLinker MC+ OBD dongle.
- While the car is plugged in, HA sends that level to Tibber, so Tibber's charging plan uses the real value.
- The Zaptec charger is in HA for monitoring.
- Failures are visible: HA shows when the car level is stale or a Tibber update failed.
- The same ESP32 reports the garage climate (added 2026-11-15): a GY-BME280-3.3V on I2C (temperature, humidity, pressure; if the chip turns out to be a BMP280, no humidity) and a DS18B20 probe on OneWire, both inside the garage. Modules from Spyros's AliExpress order of 2026-01-17.

## Non-goals

- HA planning or controlling the charging. Tibber does that; nothing in HA starts, stops or limits the Zaptec.
- Changing Tibber's charge target. The car itself stops charging at 80%, and Tibber does not know that. The Tibber app only lets Spyros set the current level for this car, not a target, so the target stays at 100% and HA sends the real level plus (100 - 80) points, capped at 100: car at 50% -> Tibber 70% -> Tibber plans 30%, exactly what the car takes. This automates what Spyros did by hand. The Tibber app therefore shows the car 20 points higher than it is; HA shows the real level. (Decided 2026-11-15, replacing the earlier 80%-target plan.)
- Reading the car while it is not plugged in.
- A phone as the Bluetooth bridge.

## Anti-goals

- Draining the e-Golf's 12 V battery by waking it.
- Tibber credentials, vehicle or home IDs in plain Git. This repository is public.
- HA and Tibber both controlling the charger.

## Constraints

- Tibber has no official way to write a vehicle's battery level. The public API has only the mutations `updateHome` and `sendPushNotification` (listed live on 2026-11-15). The Data API offers only read scopes. The only write path is the private app API that the Tibber app uses: `POST https://app.tibber.com/login.credentials` (form `email`, `password`, returns `token`) and then, on `https://app.tibber.com/v4/gql`, `mutation SetVehicleSettings($vehicleId: String!, $homeId: String!, $settings: [SettingsItemInput!]) { me { setVehicleSettings(id: $vehicleId, homeId: $homeId, settings: $settings) { __typename } } }` with `settings: [{key: "offline.vehicle.batteryLevel", value: <int>}]`. Source: the custom integration `Elibart-home/tibber_soc_updater` v2.2.0. Undocumented; it can change without notice, and its README says updates only take effect while the car is plugged in.
- The private API logs in as Spyros. HA stores his Tibber email and password.
- The dongle is a Vgate vLinker MC+ (Amazon order, April 2024), ELM327-compatible over Bluetooth.
- The e-Golf battery request, from `dieserNiko/eGolfOBDBattery` (one small source): ISO 15765-4 CAN 11-bit 500 kbit (`ATSP6`), request header `ATSH 7E5`, response filter `ATCRA 7ED`, request `22 02 8C`. The fourth response byte is the raw value; the dashboard percentage is `raw / 2.5 * 51 / 46 - 6.4`. Must be checked against the car's own display.
- ESP32 boards on hand: five ESP32-WROOM-32D development boards (BLE and classic Bluetooth).
- The car parks in the garage. A socket and Wi-Fi are next to the charger; the server rack is 2 m away.
- HA already shows the charger as `e_pavli` and the car as `princess_evi_4` through the official Tibber integration (read-only). `binary_sensor.e_pavli_plug` tells whether a car is plugged in.
- Tibber takes a new battery level at any time after plug-in: it queues the change and schedules the charging later with its own algorithm (Spyros's experience with the slider). So the push has no tight deadline after plug-in, and later pushes while charging also count.
- Spyros is moving house soon. The setup must be easy to move to another garage.

## Approaches

- O14, chosen: HA reads the car through an ESP32 and pushes the level to Tibber through the private app API. Tibber keeps planning. Cost: depends on an undocumented API.
- O15, rejected by Spyros: HA plans the charging itself with the Zaptec integration and Nord Pool prices. Official APIs only, but HA becomes the planner.
- Phone as the Bluetooth bridge, rejected: an app and Android power management to keep alive; an ESP32 runs unattended and is managed in the ESPHome dashboard that already runs here.
- Installing `tibber_soc_updater` as is, rejected (my call): it tries several login endpoints with the password, sends the password form-encoded without escaping (breaks on `&`, `+`, `=`), and the whole write path is two HTTP calls that HA's built-in `rest_command` can do in reviewable YAML.

## Build gates

The build starts with three checks on the real car, and stops for a decision if one fails:

1. G1. The vLinker MC+ advertises BLE (ESPHome BLE scan from an ESP32 in the garage). ESPHome has no classic Bluetooth serial client; if the dongle is classic only, the bridge needs another plan.
2. G2. The e-Golf answers `22028C` with the ignition on, while charging, and the scaled value matches the car's display within 1 point.
3. G3. The garage Wi-Fi is the same LAN as the cluster (`192.168.10.0/24`), so HA reaches the ESP32 on port 6053 and updates go over the air.

## Design

### Car to HA

- One ESP32-WROOM-32D by the charger socket, powered by USB, on the garage Wi-Fi, adopted in the ESPHome dashboard.
- ESPHome `ble_client` to the vLinker MC+. On connect it sends `ATZ`, `ATE0`, `ATSP6`, `ATH1`, `ATCRA 7ED`, `ATSH 7E5`; then `22028C` on each poll, and parses the reply into the sensor `e-Golf battery` (%, one decimal, `state_class: measurement`).
- Never wakes the car (my call). Every minute it sends only `ATRV`, which the dongle answers itself from the OBD port's 12 V pin without touching the car's network. It sends `22028C` only when that voltage shows the car awake (above 12.7 V: measured 13.0-13.1 V with the car on, 12.2-12.3 V a minute after switching off, on 2026-11-15) and the last good read is older than 10 minutes. So the car is read on arrival with the ignition on, and every 10 minutes while it charges; while it sleeps plugged in, nothing reaches its network.
- The sensor keeps its last value with the time of the read (`e-Golf battery read at`), so HA knows how fresh it is.
- The device YAML is kept in Git at `3-devices/esphome/egolf-obd.yaml` without secrets. `3-devices/` is a new top-level layer for firmware on home devices, next to `0-truenas`, `1-proxmox` and `2-k3s`; the ESP32 is not part of the cluster. Wi-Fi and API keys come from ESPHome's own `secrets.yaml` on the `esphome-config` PVC; the file is copied into the ESPHome dashboard for compiling. First flash over USB from the laptop; later updates over the air.
- The dongle's BLE service and characteristic UUIDs are read with an ESPHome BLE scan before the config is final.

### Garage climate

- GY-BME280-3.3V on I2C: VCC 3V3, SDA GPIO21, SCL GPIO22, address 0x76 or 0x77 (I2C scan at boot). ESPHome `bme280_i2c`, or `bmp280_i2c` if the chip ID read at boot is `0x58`. Mounted a few centimetres from the ESP32 so the board's own heat does not skew the reading.
- DS18B20 probe on OneWire: VCC 3V3, data GPIO4 (the adapter board carries the pull-up). ESPHome `one_wire` (gpio) + `dallas_temp`.
- Entities: `sensor.garage_temperature`, `sensor.garage_humidity`, `sensor.garage_pressure`, `sensor.garage_probe_temperature` (device **Garage sensors**, area **Garage**), updated every 60 s.

### HA to Tibber

- Secret `home-assistant-secrets` (SOPS) gains `tibber_login_body`, `tibber_vehicle_id`, `tibber_home_id` in `secrets.yaml`. The login comes from the existing homelab vault item for Spyros's Tibber login; Spyros approved storing it in HA on 2026-11-15. The IDs are read once from the app API (`me { homes { id vehicles { id } } }` or the equivalent) during the build.
- `configuration.yaml` (ConfigMap, Git) gains an HA package with:
  - `rest_command.tibber_app_login`: form POST to `login.credentials` with `payload: !secret tibber_login_body`, the form body stored already URL-encoded as one secret, because `!secret` cannot sit inside a template and script variables end up in traces.
  - `rest_command.tibber_set_battery_level`: GraphQL POST with `Authorization: Bearer <token>` and the mutation above.
  - `script.egolf_push_to_tibber`: logs in, sends the level rounded to an integer, records the result.
  - An automation that runs the script when `binary_sensor.e_pavli_plug` is on and a fresh read arrives (read time after the plug-in time, or within 30 minutes before it, which covers reading on arrival). A level read more than 30 minutes before the plug-in is never sent, so the previous session's value does not reach Tibber. (Unplugging and plugging back in within 30 minutes of a read can resend that read; the level has not changed meanwhile.)
- Logging in on each push is fine at this rate (at most every 10 minutes while charging); no token cache. The token appears in the script's `response_variable` and so in its saved trace on the config PVC; the script keeps `trace: stored_traces: 1`. Accepted: the token is short-lived (about 18 hours per the source) and the PVC already holds HA's own credentials.

### Zaptec

- Custom integration `custom-components/zaptec`, pinned release with SHA-256, installed by the existing `configuration` init container the same way as `tibber_pulse_mqtt`, with a Renovate rule that never automerges. Spyros adds it in the UI with his Zaptec login. No HA automation controls the charger. Its Python requirements (`azure-servicebus>=7.15.0,<8`, `pydantic>=2.11.7,<2.15`, `aiolimiter>=1.3.0,<2`) are already in the HA 2026.11.1 image (7.15.0, 2.14.1, 1.3.0, checked in the running pod), so HA downloads nothing.

### Observability

- `input_datetime.egolf_tibber_last_push` and `input_text.egolf_tibber_last_result`, set by the script.
- Home Assistant's `logger` filters drop `rest_command` log lines that contain the Tibber login URL or a bearer token, because `rest_command` logs the request body on HTTP errors.
- An HA persistent notification when a push fails, and when the car has been charging (`binary_sensor.e_pavli_charging` on) for 30 minutes without a fresh read. Sleeping while plugged in is normal and raises nothing.

## Risks

- R5. Tibber changes the private API or blocks the login. Pushes fail, the notification shows it, and Tibber keeps the last value.
- R6. Tibber's terms may not allow automated use of the app API. Not checked.
- R7. The battery request or scaling is wrong for this car. The build checks the value against the car's display before the automation is turned on.
- R8. Querying wakes the car and drains the 12 V battery. The `ATRV` gate keeps requests off the car's network while it sleeps; the dongle itself still draws a little from the OBD port, as it does today.
- R9. Bluetooth through the car body is weak. The ESP32 can move closer; it only needs a socket.

## Left to the builder

- ESPHome lambda details for the ELM327 exchange and reply parsing.
- Exact HA YAML for the package, templates and notification texts.
- Zaptec release pin and test additions in `2-k3s/36.home-assistant/tools/manifests.test.py`.
- README section.
