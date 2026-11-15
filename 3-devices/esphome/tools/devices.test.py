#!/usr/bin/env python3
import ctypes
import unittest
from pathlib import Path

import yaml

ESPHOME = Path(__file__).resolve().parents[1]


class Tagged(tuple):
    pass


class ESPHomeLoader(yaml.SafeLoader):
    pass


ESPHomeLoader.add_multi_constructor("!", lambda loader, suffix, node: Tagged((suffix, loader.construct_scalar(node))))


def device(name):
    return yaml.load((ESPHOME / name).read_text(), Loader=ESPHomeLoader)


class EgolfObdTests(unittest.TestCase):
    def setUp(self):
        self.text = (ESPHOME / "egolf-obd.yaml").read_text()
        self.config = device("egolf-obd.yaml")

    def test_secrets_stay_out_of_git(self):
        self.assertEqual(self.config["wifi"]["ssid"], ("secret", "wifi_ssid"))
        self.assertEqual(self.config["wifi"]["password"], ("secret", "wifi_password"))
        self.assertEqual(self.config["api"]["encryption"]["key"], ("secret", "egolf_api_key"))
        self.assertEqual(self.config["ota"][0]["password"], ("secret", "egolf_ota_password"))
        self.assertEqual(self.config["ble_client"][0]["mac_address"], ("secret", "vlinker_mac"))
        self.assertNotIn("tibber", self.text.lower())

    def test_device(self):
        self.assertEqual(self.config["esphome"]["name"], "egolf-obd")
        self.assertEqual(self.config["esphome"]["friendly_name"], "Garage sensors")
        self.assertEqual(self.config["esp32"]["board"], "esp32dev")
        self.assertEqual(self.config["wifi"]["use_address"], "192.168.10.205")
        self.assertEqual(self.config["logger"]["level"], "INFO")
        self.assertEqual(self.config["time"][0]["platform"], "homeassistant")

    def test_vlinker_serial_bridge(self):
        rx = next(t for t in self.config["text_sensor"] if t.get("platform") == "ble_client")
        self.assertEqual((rx["service_uuid"], rx["characteristic_uuid"], rx["notify"], rx["internal"]), ("18F0", "2AF0", True, True))
        self.assertIn('characteristic_uuid: "2AF1"', self.text)
        send = next(s for s in self.config["script"] if s["id"] == "obd_send")
        self.assertEqual((send["mode"], send["max_runs"]), ("queued", 20))
        for command in ("ATZ", "ATE0", "ATSP6", "ATH1", "ATCRA 7ED", "ATSH 7E5"):
            self.assertIn(f'cmd: "{command}"', self.text)

    def test_reads_battery_only_while_awake(self):
        [poll] = self.config["interval"]
        self.assertEqual(poll["interval"], "60s")
        self.assertIn('"ATRV"', self.text)
        self.assertIn('"22028C"', self.text)
        self.assertRegex(self.text, r"id\(vbat\) > 12\.7")
        self.assertRegex(self.text, r"600000")
        self.assertEqual(((self.config["esp32_ble_tracker"] or {}).get("scan_parameters") or {}).get("window", "30ms"), "30ms")

    def test_voltage_only_from_atrv_replies(self):
        self.assertIn('sscanf(x.c_str(), "%f%c", &volts, &unit) == 2 && unit == \'V\'', self.text)
        libc = ctypes.CDLL(None)

        def parsed(reply):
            volts, unit = ctypes.c_float(), ctypes.c_char()
            ok = libc.sscanf(reply.encode(), b"%f%c", ctypes.byref(volts), ctypes.byref(unit)) == 2 and unit.value == b"V"
            return round(volts.value, 1) if ok else None

        self.assertEqual(parsed("12.5V\r\r>"), 12.5)
        self.assertEqual(parsed("13.0V>"), 13.0)
        self.assertEqual(parsed("13.1V"), 13.1)
        for reply in ("7ED 04 62 02 8C B4 ", ">", "OK>", "ATE0OK>", "ELM327 v2.2>", "NO DATA>", "\r>"):
            self.assertIsNone(parsed(reply), reply)

    def test_area(self):
        self.assertEqual(self.config["esphome"]["area"], "Garage")

    def test_battery_formula(self):
        self.assertIn("/ 2.5f * 51.0f / 46.0f - 6.4f", self.text)

    def test_entities(self):
        names = {t.get("name") for t in self.config["text_sensor"]}
        for s in self.config["sensor"]:
            names |= {s.get("name")} | {v["name"] for v in s.values() if isinstance(v, dict) and "name" in v}
        self.assertLessEqual({"e-Golf battery", "e-Golf 12V", "e-Golf battery read at", "Temperature", "Humidity", "Pressure", "Probe temperature"}, names)
        read_at = next(t for t in self.config["text_sensor"] if t.get("name") == "e-Golf battery read at")
        self.assertEqual(read_at["device_class"], "timestamp")
        bme = next(s for s in self.config["sensor"] if s.get("platform") == "bme280_i2c")
        self.assertEqual(bme["address"], 0x76)
        self.assertEqual((self.config["i2c"]["sda"], self.config["i2c"]["scl"]), ("GPIO21", "GPIO22"))
        self.assertEqual(self.config["one_wire"][0]["pin"], "GPIO4")


if __name__ == "__main__":
    unittest.main()
