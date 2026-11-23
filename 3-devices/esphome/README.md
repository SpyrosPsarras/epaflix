# ESPHome devices

Firmware for ESP32 boards around the house. The boards are not part of the cluster; only their compiler is. The ESPHome dashboard (`2-k3s/36.home-assistant`, https://esphome.epaflix.com) builds and flashes them, and Home Assistant talks to each board over the ESPHome API on port 6053.

## egolf-obd

ESP32-WROOM-32D in the garage, DHCP reservation `192.168.10.205` on the router. Shows in Home Assistant as device **Garage sensors** in area **Garage** (the area comes from the firmware). Home Assistant derives the entity IDs from the device and sensor names (`sensor.garage_sensors_e_golf_battery` and so on); after adding the device, rename them in Home Assistant to the IDs below, because the e-Golf automation in `2-k3s/36.home-assistant` uses them. Design: `docs/superpowers/specs/2026-11-15-egolf-soc-to-tibber-design.md`.

| Entity | Source |
|---|---|
| `sensor.egolf_battery` | e-Golf high-voltage battery %, from the Vgate vLinker MC+ in the car |
| `sensor.egolf_battery_read_at` | time of the last battery read |
| `sensor.egolf_12v` | car 12 V, measured by the vLinker itself every minute |
| `sensor.garage_temperature`, `sensor.garage_humidity`, `sensor.garage_pressure` | GY-BME280-3.3V on I2C, address `0x76` |
| `sensor.garage_probe_temperature` | DS18B20 probe on OneWire |

Wiring (38-pin DevKit): BME280 VCC 3V3, GND GND, SDA GPIO21, SCL GPIO22. DS18B20 adapter VCC 3V3, GND GND, DAT GPIO4; the adapter board carries the 4.7 kΩ pull-up.

### The car link

The vLinker MC+ is an ELM327 v2.2 adapter over Bluetooth LE: service `18F0`, notify `2AF0`, write `2AF1`. It takes one connection at a time, so a phone connected to it locks the ESP32 out. After connecting, the ESP32 sends `ATZ`, `ATE0`, `ATSP6`, `ATH1`, `ATCRA 7ED`, `ATSH 7E5`. Every minute it sends `ATRV` (the dongle's own 12 V reading, no traffic on the car's network). Only when that reads above 12.7 V (car on or charging; measured 13.0 V on, 12.3 V off) and the last read is older than 10 minutes does it send `22028C` to the battery controller. On arrival, when the dongle comes back after more than 10 minutes out of reach, it sends `22028C` once right after connecting, whatever the voltage, because the ESP32 often connects only after the car is switched off. Whether the car still answers then is not yet confirmed; if it does not, the dongle replies `NO DATA` and nothing is published. The reply `7ED 04 62 02 8C xx` gives the dashboard percentage as `xx / 2.5 * 51 / 46 - 6.4` (source: `dieserNiko/eGolfOBDBattery`; matched the car's display, 73.4 % vs 73 %, on 2026-11-15).

### Secrets

`!secret` values live in `/config/secrets.yaml` on the `esphome-config-pithari` PVC, not in Git: `wifi_ssid`, `wifi_password`, `egolf_api_key`, `egolf_ota_password`, `vlinker_mac`. The API key and OTA password are also in the homelab vault item for this device.

### Update

```sh
kubectl -n home-assistant exec -i deploy/esphome -- sh -c 'cat > /config/egolf-obd.yaml' < 3-devices/esphome/egolf-obd.yaml
kubectl -n home-assistant exec deploy/esphome -- sh -c 'cd /config && esphome run egolf-obd.yaml --device 192.168.10.205 --no-logs'
```

A new board needs one USB flash first: build `firmware.factory.bin` in the dashboard and write it with `esptool --baud 115200 write-flash 0x0 firmware.factory.bin` (460800 baud failed on this board).

### Known limits

- Inside the metal server rack the board loses Wi-Fi. It sits outside the rack, powered from a server's USB port.
- The board keeps its Bluetooth connection to the dongle while the car is parked, which likely keeps the dongle awake. `sensor.egolf_12v` shows whether that drains the car's 12 V battery.
