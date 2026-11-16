#!/usr/bin/env python3
import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

import yaml

HA = Path(__file__).resolve().parents[1]
ROOT = HA.parents[1]


def documents(path):
    return [doc for doc in yaml.safe_load_all((HA / path).read_text()) if doc]


def resource(path, kind, name):
    return next(doc for doc in documents(path) if doc["kind"] == kind and doc["metadata"]["name"] == name)


def manifests():
    return [doc for path in sorted(HA.glob("*.yaml")) if not path.name.endswith(".enc.yaml") for doc in documents(path.name)]


class Tagged(tuple):
    pass


class HomeAssistantLoader(yaml.SafeLoader):
    pass


HomeAssistantLoader.add_multi_constructor("!", lambda loader, suffix, node: Tagged((suffix, loader.construct_scalar(node))))


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.deployment = resource("home-assistant.yaml", "Deployment", "home-assistant")
        self.pod = self.deployment["spec"]["template"]["spec"]

    def test_argo_app(self):
        app = yaml.safe_load((ROOT / "2-k3s/11.argocd/apps/app-home-assistant.yaml").read_text())
        self.assertEqual(app["spec"]["source"]["path"], "2-k3s/36.home-assistant")
        self.assertEqual(app["spec"]["destination"]["namespace"], "home-assistant")
        self.assertEqual(app["spec"]["syncPolicy"]["automated"], {"selfHeal": True, "prune": True})
        for kind in ("Database", "DatabaseRole"):
            self.assertIn({"group": "postgresql.cnpg.io", "kind": kind, "jsonPointers": ["/status"]}, app["spec"]["ignoreDifferences"])
        apps = yaml.safe_load((ROOT / "2-k3s/11.argocd/apps/kustomization.yaml").read_text())
        self.assertIn("app-home-assistant.yaml", apps["resources"])

    def test_database(self):
        role = resource("database.yaml", "DatabaseRole", "homeassistant")
        database = resource("database.yaml", "Database", "homeassistant")
        for doc in (role, database):
            self.assertEqual(doc["metadata"]["namespace"], "postgres-system")
            self.assertEqual(doc["spec"]["cluster"], {"name": "postgres-cluster"})
            self.assertEqual(doc["spec"]["name"], "homeassistant")
        self.assertEqual(role["spec"]["passwordSecret"], {"name": "homeassistant-db-role"})
        self.assertIs(role["spec"]["login"], True)
        self.assertEqual(role["spec"]["databaseRoleReclaimPolicy"], "retain")
        self.assertEqual(database["spec"]["owner"], "homeassistant")
        self.assertEqual(database["spec"]["databaseReclaimPolicy"], "retain")

    def test_images_pinned(self):
        images = documents("kustomization.yaml")[0]["images"]
        names = [image["name"] for image in images]
        self.assertIn("ghcr.io/home-assistant/home-assistant", names)
        self.assertIn("ghcr.io/stakater/reloader", names)
        self.assertIn("ghcr.io/esphome/esphome", names)
        self.assertIn({"name": "ghcr.io/python-roborock/local_roborock_server", "newTag": "1.2.1@sha256:0f3d0dd05f14315273df5622d43008b2665c24b2fed79277822d4c9184eab158"}, images)
        for image in images:
            self.assertRegex(image["newTag"], r"^[^@]+@sha256:[0-9a-f]{64}$")

    def test_routes_internal_only(self):
        docs = manifests()
        self.assertNotIn("Ingress", [doc.get("kind") for doc in docs])
        routes = [doc for doc in docs if doc.get("kind", "").startswith("IngressRoute")]
        self.assertTrue(routes)
        for route in routes:
            self.assertEqual(route["spec"]["entryPoints"], ["internal"])
            for rule in route["spec"]["routes"]:
                self.assertNotIn("authentik-forwardauth", [m["name"] for m in rule.get("middlewares", [])])

    def test_home_assistant_route(self):
        route = resource("ingressroute.yaml", "IngressRoute", "home-assistant")
        [rule] = route["spec"]["routes"]
        self.assertEqual(rule["match"], "Host(`ha.epaflix.com`)")
        self.assertEqual(rule["services"], [{"name": "home-assistant", "port": 8123}])
        self.assertEqual(rule["middlewares"], [{"name": "security-headers", "namespace": "traefik-system"}])
        self.assertEqual(route["spec"]["tls"], {})
        service = resource("home-assistant.yaml", "Service", "home-assistant")
        self.assertEqual(service["spec"]["selector"], {"app": "home-assistant"})
        self.assertEqual([port["port"] for port in service["spec"]["ports"]], [8123])

    def test_esphome_route(self):
        route = resource("ingressroute.yaml", "IngressRoute", "esphome")
        [rule] = route["spec"]["routes"]
        self.assertEqual(rule["match"], "Host(`esphome.epaflix.com`)")
        self.assertEqual(rule["services"], [{"name": "esphome", "port": 6052}])
        self.assertEqual(rule["middlewares"], [{"name": "security-headers", "namespace": "traefik-system"}])
        self.assertEqual(route["spec"]["tls"], {})
        service = resource("esphome.yaml", "Service", "esphome")
        self.assertEqual(service["spec"]["selector"], {"app": "esphome"})
        self.assertEqual([port["port"] for port in service["spec"]["ports"]], [6052])

    def test_esphome_deployment(self):
        deployment = resource("esphome.yaml", "Deployment", "esphome")
        pod = deployment["spec"]["template"]["spec"]
        [container] = pod["containers"]
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["template"]["metadata"]["labels"], {"app": "esphome"})
        self.assertIs(pod["automountServiceAccountToken"], False)
        self.assertIs(pod["enableServiceLinks"], False)
        self.assertEqual(deployment["metadata"]["annotations"]["secret.reloader.stakater.com/reload"], "esphome-auth")
        self.assertEqual(container["image"], "ghcr.io/esphome/esphome")
        env = {e["name"]: e for e in container["env"]}
        self.assertNotIn("PASSWORD", env)
        self.assertNotIn("USERNAME", env)
        for name in ("ESPHOME_USERNAME", "ESPHOME_PASSWORD"):
            self.assertEqual(env[name]["valueFrom"], {"secretKeyRef": {"name": "esphome-auth", "key": name}})
        volumes = {v["name"]: v for v in pod["volumes"]}
        mount = next(m for m in container["volumeMounts"] if m["mountPath"] == "/config")
        self.assertEqual(volumes[mount["name"]]["persistentVolumeClaim"]["claimName"], "esphome-config")
        pvc = resource("esphome.yaml", "PersistentVolumeClaim", "esphome-config")
        self.assertEqual(pvc["spec"]["storageClassName"], "truenas-iscsi")
        self.assertEqual(pvc["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(pvc["spec"]["resources"]["requests"]["storage"], "5Gi")
        probe = container["readinessProbe"]["httpGet"]
        self.assertEqual((probe["path"], probe["port"]), ("/version", 6052))

    def test_esphome_network_policy(self):
        policy = next(d for d in documents("network-policy.yaml") if d["spec"]["podSelector"] == {"matchLabels": {"app": "esphome"}})
        self.assertEqual(policy["spec"]["policyTypes"], ["Ingress"])
        self.assertEqual(policy["spec"]["ingress"], [{
            "from": [{
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "traefik-system"}},
                "podSelector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}},
            }],
            "ports": [{"port": 6052, "protocol": "TCP"}],
        }])

    def test_roborock_service(self):
        service = resource("roborock.yaml", "Service", "roborock")
        self.assertEqual(service["spec"]["type"], "LoadBalancer")
        self.assertEqual(service["spec"]["loadBalancerIP"], "192.168.10.116")
        self.assertEqual(service["spec"]["externalTrafficPolicy"], "Local")
        self.assertEqual(service["spec"]["selector"], {"app": "roborock"})
        self.assertEqual([(p["port"], p["targetPort"], p["protocol"]) for p in service["spec"]["ports"]], [(555, 555, "TCP"), (8881, 8881, "TCP")])

    def test_roborock_certificate(self):
        certificate = resource("roborock.yaml", "Certificate", "api-rr")
        self.assertEqual(certificate["metadata"]["namespace"], "home-assistant")
        self.assertEqual(certificate["spec"], {
            "secretName": "api-rr-tls",
            "issuerRef": {"name": "letsencrypt-dns", "kind": "ClusterIssuer"},
            "dnsNames": ["api-rr.epaflix.com"],
        })

    def test_roborock_deployment(self):
        deployment = resource("roborock.yaml", "Deployment", "roborock")
        pod = deployment["spec"]["template"]["spec"]
        [container] = pod["containers"]
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})
        self.assertEqual(deployment["spec"]["template"]["metadata"]["labels"], {"app": "roborock"})
        self.assertIs(pod["automountServiceAccountToken"], False)
        self.assertIs(pod["enableServiceLinks"], False)
        self.assertEqual(deployment["metadata"]["annotations"]["secret.reloader.stakater.com/reload"], "api-rr-tls,roborock-config")
        self.assertEqual(container["image"], "ghcr.io/python-roborock/local_roborock_server")
        volumes = {v["name"]: v for v in pod["volumes"]}
        mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
        self.assertEqual(set(mounts), {"/app/config.toml", "/run/certs", "/data"})
        config = mounts["/app/config.toml"]
        self.assertEqual((config["subPath"], config["readOnly"]), ("config.toml", True))
        self.assertEqual(volumes[config["name"]]["secret"], {"secretName": "roborock-config", "items": [{"key": "config.toml", "path": "config.toml"}]})
        self.assertIs(mounts["/run/certs"]["readOnly"], True)
        self.assertNotIn("subPath", mounts["/run/certs"])
        self.assertIn(mounts["/data"].get("readOnly"), (None, False))
        service = resource("roborock.yaml", "Service", "roborock")
        self.assertEqual(
            sorted((p["name"], p["containerPort"]) for p in container["ports"]),
            sorted((p["name"], p["targetPort"]) for p in service["spec"]["ports"]),
        )
        self.assertEqual(volumes[mounts["/run/certs"]["name"]]["secret"], {"secretName": "api-rr-tls"})
        self.assertEqual(volumes[mounts["/data"]["name"]]["persistentVolumeClaim"], {"claimName": "roborock-data"})
        pvc = resource("roborock.yaml", "PersistentVolumeClaim", "roborock-data")
        self.assertEqual(pvc["metadata"]["annotations"], {"argocd.argoproj.io/sync-options": "Prune=false,Delete=false"})
        self.assertEqual(pvc["spec"]["storageClassName"], "truenas-iscsi")
        self.assertEqual(pvc["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(pvc["spec"]["resources"]["requests"]["storage"], "2Gi")
        self.assertEqual(container["resources"]["requests"], {"cpu": "1", "memory": "1Gi"})
        self.assertEqual(container["readinessProbe"]["httpGet"], {"path": "/admin", "port": 555, "scheme": "HTTPS"})

    def test_roborock_network_policy(self):
        policy = resource("network-policy.yaml", "NetworkPolicy", "roborock-ingress")
        self.assertEqual(policy["spec"], {
            "podSelector": {"matchLabels": {"app": "roborock"}},
            "policyTypes": ["Ingress"],
            "ingress": [{"ports": [{"port": 555, "protocol": "TCP"}, {"port": 8881, "protocol": "TCP"}]}],
        })

    def test_mosquitto_deployment(self):
        deployment = resource("mosquitto.yaml", "Deployment", "mosquitto")
        pod = deployment["spec"]["template"]["spec"]
        [container] = pod["containers"]
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["template"]["metadata"]["labels"], {"app": "mosquitto"})
        self.assertIs(pod["automountServiceAccountToken"], False)
        self.assertIs(pod["enableServiceLinks"], False)
        self.assertEqual(pod["securityContext"], {"runAsUser": 1883, "runAsGroup": 1883, "fsGroup": 1883, "runAsNonRoot": True})
        self.assertEqual(deployment["metadata"]["annotations"]["secret.reloader.stakater.com/reload"], "mosquitto-config")
        self.assertEqual(container["image"], "eclipse-mosquitto")
        self.assertEqual(container["command"], ["mosquitto", "-c", "/mosquitto/config/mosquitto.conf"])
        self.assertEqual([(p["containerPort"], p["protocol"]) for p in container["ports"]], [(1883, "TCP")])
        self.assertEqual(container["readinessProbe"]["tcpSocket"], {"port": 1883})
        volumes = {v["name"]: v for v in pod["volumes"]}
        [mount] = container["volumeMounts"]
        self.assertEqual((mount["mountPath"], mount["readOnly"]), ("/mosquitto/config", True))
        self.assertEqual(volumes[mount["name"]]["emptyDir"], {"medium": "Memory"})
        [init] = pod["initContainers"]
        self.assertEqual(init["image"], "eclipse-mosquitto")
        self.assertEqual(init["command"], ["sh", "-c", "cp -L /secret/* /mosquitto/config/ && chmod 0400 /mosquitto/config/*"])
        init_mounts = {m["mountPath"]: m for m in init["volumeMounts"]}
        self.assertEqual(init_mounts["/mosquitto/config"]["name"], mount["name"])
        self.assertIs(init_mounts["/secret"]["readOnly"], True)
        self.assertEqual(volumes[init_mounts["/secret"]["name"]]["secret"], {"secretName": "mosquitto-config"})
        image = next(i for i in documents("kustomization.yaml")[0]["images"] if i["name"] == "eclipse-mosquitto")
        self.assertRegex(image["newTag"], r"^2\.[0-9.]+-alpine@sha256:[0-9a-f]{64}$")
        self.assertIn("mosquitto.yaml", documents("kustomization.yaml")[0]["resources"])

    def test_mosquitto_service(self):
        service = resource("mosquitto.yaml", "Service", "mosquitto")
        self.assertEqual(service["spec"]["type"], "LoadBalancer")
        self.assertEqual(service["spec"]["loadBalancerIP"], "192.168.10.117")
        self.assertEqual(service["spec"]["externalTrafficPolicy"], "Local")
        self.assertEqual(service["spec"]["selector"], {"app": "mosquitto"})
        self.assertEqual([(p["port"], p["targetPort"], p["protocol"]) for p in service["spec"]["ports"]], [(1883, 1883, "TCP")])

    def test_mosquitto_network_policy(self):
        policy = resource("network-policy.yaml", "NetworkPolicy", "mosquitto")
        self.assertEqual(policy["spec"], {
            "podSelector": {"matchLabels": {"app": "mosquitto"}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [{
                "from": [
                    {"ipBlock": {"cidr": "192.168.10.230/32"}},
                    {"podSelector": {"matchLabels": {"app": "home-assistant"}}},
                    {
                        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "observability"}},
                        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "prometheus-blackbox-exporter"}},
                    },
                ],
                "ports": [{"port": 1883, "protocol": "TCP"}],
            }],
            "egress": [
                {
                    "to": [{
                        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                        "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                    }],
                    "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
                },
                {
                    "to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]}}],
                    "ports": [{"protocol": "TCP", "port": 8883}],
                },
            ],
        })

    def test_no_device_id_in_plain_files(self):
        plain = [path for path in HA.rglob("*") if path.is_file() and not path.name.endswith(".enc.yaml")]
        self.assertTrue(plain)
        for path in plain:
            self.assertNotRegex(path.read_text(errors="ignore"), r"tibber-pulse-[0-9a-f]{8,}", path)

    def test_no_host_network_and_recreate(self):
        deployments = [doc for doc in manifests() if doc.get("kind") == "Deployment"]
        self.assertTrue(deployments)
        for deployment in deployments:
            self.assertIn(deployment["spec"]["template"]["spec"].get("hostNetwork"), (None, False))
            self.assertEqual(deployment["spec"]["strategy"]["type"], "Recreate")

    def test_ha_configuration(self):
        configmap = resource("home-assistant.yaml", "ConfigMap", "home-assistant-configuration")
        config = yaml.load(configmap["data"]["configuration.yaml"], Loader=HomeAssistantLoader)
        self.assertIn("default_config", config)
        self.assertNotIn("http", config)
        self.assertEqual(config["recorder"]["db_url"], ("secret", "recorder_db_url"))
        self.assertIsInstance(config["recorder"]["db_url"], Tagged)
        self.assertEqual(config["recorder"]["db_max_retries"], 100)
        for key, name in {"automation": "automations.yaml", "script": "scripts.yaml", "scene": "scenes.yaml"}.items():
            self.assertEqual(config[key], ("include", name))
        self.assertEqual(config["homeassistant"]["packages"], {"egolf": ("include", "egolf.yaml")})

    def egolf(self):
        configmap = resource("home-assistant.yaml", "ConfigMap", "home-assistant-configuration")
        return yaml.load(configmap["data"]["egolf.yaml"], Loader=HomeAssistantLoader)

    def test_egolf_rest_commands(self):
        rest = self.egolf()["rest_command"]
        login = rest["tibber_app_login"]
        self.assertEqual((login["url"], login["method"], login["content_type"]), ("https://app.tibber.com/login.credentials", "post", "application/x-www-form-urlencoded"))
        self.assertEqual(login["payload"], ("secret", "tibber_login_body"))
        push = rest["tibber_set_battery_level"]
        self.assertEqual((push["url"], push["method"], push["content_type"]), ("https://app.tibber.com/v4/gql", "post", "application/json"))
        self.assertEqual(push["headers"]["Authorization"], "Bearer {{ token }}")
        self.assertIn("mutation SetVehicleSettings($vehicleId: String!, $homeId: String!, $settings: [SettingsItemInput!]) { me { setVehicleSettings(id: $vehicleId, homeId: $homeId, settings: $settings) { __typename } } }", push["payload"])
        self.assertIn("offline.vehicle.batteryLevel", push["payload"])

    def test_egolf_script(self):
        script = self.egolf()["script"]["egolf_push_to_tibber"]
        self.assertEqual(script["trace"], {"stored_traces": 1})
        self.assertEqual(script["variables"]["vehicle_id"], ("secret", "tibber_vehicle_id"))
        self.assertEqual(script["variables"]["home_id"], ("secret", "tibber_home_id"))
        self.assertEqual(script["variables"]["car_charge_limit"], 80)
        level = script["variables"]["level"]
        self.assertIn("sensor.egolf_battery", level)
        self.assertIn("100 - car_charge_limit", level)
        self.assertIn("-1 if real < 0", level)
        self.assertIn("| min", level)
        actions = [step.get("action") for step in script["sequence"]]
        self.assertLess(actions.index("rest_command.tibber_app_login"), actions.index("rest_command.tibber_set_battery_level"))
        steps = {step.get("action"): step for step in script["sequence"]}
        self.assertEqual(steps["rest_command.tibber_app_login"]["response_variable"], "login")
        self.assertEqual(steps["rest_command.tibber_set_battery_level"]["response_variable"], "result")
        text = str(script)
        self.assertIn("input_text.egolf_tibber_last_result", text)
        self.assertIn("persistent_notification.create", text)
        self.assertIn("errors", text)

    def test_egolf_login_never_logged(self):
        patterns = [re.compile(p) for p in self.egolf()["logger"]["filters"]["homeassistant.components.rest_command"]]
        dropped = [
            "Error. Url: https://app.tibber.com/login.credentials. Status code 401. Payload: email=a%40b.c&password=x",
            "Calling post https://app.tibber.com/login.credentials with headers: {} and payload: email=a%40b.c&password=x",
            "Calling post https://app.tibber.com/v4/gql with headers: {'Authorization': 'Bearer abc'} and payload: {}",
        ]
        kept = "Error. Url: https://app.tibber.com/v4/gql. Status code 400. Payload: {}"
        for message in dropped:
            self.assertTrue(any(p.search(message) for p in patterns), message)
        self.assertFalse(any(p.search(kept) for p in patterns))

    def test_egolf_automation_freshness(self):
        automation = next(a for a in self.egolf()["automation"] if a["id"] == "egolf_push_to_tibber")
        entities = {t["entity_id"] for t in automation["triggers"]}
        self.assertEqual(entities, {"sensor.egolf_battery_read_at", "binary_sensor.e_pavli_plug"})
        read_trigger = next(t for t in automation["triggers"] if t["entity_id"] == "sensor.egolf_battery_read_at")
        self.assertEqual(read_trigger["not_from"], ["unavailable"])
        conditions = str(automation["conditions"])
        self.assertEqual(conditions, str([{"condition": "template", "value_template": "{{ states('sensor.egolf_battery_read_at') | as_datetime is not none }}"}]))
        self.assertEqual(automation["actions"], [{"action": "script.egolf_push_to_tibber"}])

    def test_egolf_alerts(self):
        automations = {a["id"]: a for a in self.egolf()["automation"]}
        stale = automations["egolf_battery_stale"]
        self.assertEqual(stale["triggers"], [{"trigger": "state", "entity_id": "binary_sensor.e_pavli_charging", "to": "on", "for": "00:30:00"}])
        self.assertIn("sensor.egolf_battery_read_at", str(stale["conditions"]))
        low = automations["egolf_12v_low"]
        self.assertEqual(low["triggers"], [{"trigger": "numeric_state", "entity_id": "sensor.egolf_12v", "below": 12.0, "for": "00:10:00"}])
        for alert in (stale, low):
            self.assertIn("persistent_notification.create", str(alert["actions"]))
        helpers = self.egolf()
        self.assertIn("egolf_tibber_last_push", helpers["input_datetime"])
        self.assertIn("egolf_tibber_last_result", helpers["input_text"])

    def test_ha_http_storage(self):
        configmap = resource("home-assistant.yaml", "ConfigMap", "home-assistant-configuration")
        store = json.loads(configmap["data"]["http.json"])
        self.assertEqual((store["version"], store["minor_version"], store["key"]), (3, 1, "http"))
        self.assertIsNone(store["data"]["pending"])
        self.assertIs(store["data"]["yaml_migration_done"], True)
        self.assertEqual(store["data"]["stable"], {
            "server_port": 8123,
            "use_x_forwarded_for": True,
            "trusted_proxies": ["10.42.0.0/16"],
            "cors_allowed_origins": ["https://cast.home-assistant.io"],
            "login_attempts_threshold": -1,
            "ip_ban_enabled": True,
            "ssl_profile": "intermediate_v6",
            "use_x_frame_options": True,
            "created_at": "2026-11-13T00:00:00+00:00",
            "error": None,
            "error_message": None,
        })

    def test_ha_deployment(self):
        self.assertEqual(self.deployment["spec"]["replicas"], 1)
        self.assertIs(self.pod["enableServiceLinks"], False)
        self.assertEqual(self.pod["containers"][0]["image"], "ghcr.io/home-assistant/home-assistant")
        self.assertEqual(self.pod["initContainers"][0]["image"], "ghcr.io/home-assistant/home-assistant")
        annotations = self.deployment["metadata"]["annotations"]
        self.assertEqual(annotations["secret.reloader.stakater.com/reload"], "home-assistant-secrets")
        self.assertEqual(annotations["configmap.reloader.stakater.com/reload"], "home-assistant-configuration")
        volumes = {volume["name"]: volume for volume in self.pod["volumes"]}
        mount = next(m for m in self.pod["containers"][0]["volumeMounts"] if m["mountPath"] == "/config/secrets.yaml")
        self.assertEqual(mount["subPath"], "secrets.yaml")
        self.assertEqual(volumes[mount["name"]]["secret"]["secretName"], "home-assistant-secrets")
        claim = next(v for v in volumes.values() if "persistentVolumeClaim" in v)["persistentVolumeClaim"]["claimName"]
        pvc = resource("home-assistant.yaml", "PersistentVolumeClaim", claim)
        self.assertEqual(claim, "home-assistant-config")
        self.assertEqual(pvc["spec"]["storageClassName"], "truenas-iscsi")
        self.assertEqual(pvc["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(pvc["spec"]["resources"]["requests"]["storage"], "5Gi")
        probe = self.pod["containers"][0]["readinessProbe"]["httpGet"]
        self.assertEqual((probe["path"], probe["port"]), ("/", 8123))

    def test_reloader(self):
        chart = next(c for c in documents("kustomization.yaml")[0]["helmCharts"] if c["name"] == "reloader")
        self.assertEqual(chart["releaseName"], "reloader-home-assistant")
        self.assertEqual(chart["namespace"], "home-assistant")
        self.assertEqual((chart["repo"], chart["version"]), ("https://stakater.github.io/stakater-charts", "2.2.20"))
        self.assertIs(documents(chart["valuesFile"])[0]["reloader"]["watchGlobally"], False)
        policy = resource("network-policy.yaml", "NetworkPolicy", "reloader")
        self.assertEqual(policy["spec"], {
            "podSelector": {"matchLabels": {"app.kubernetes.io/instance": "reloader-home-assistant"}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [
                {
                    "to": [{
                        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                        "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                    }],
                    "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
                },
                {
                    "to": [{"ipBlock": {"cidr": f"10.0.0.{n}/32"}} for n in (51, 52, 53)],
                    "ports": [{"protocol": "TCP", "port": 6443}],
                },
            ],
        })

    def test_ha_init_keeps_ui_files(self):
        init = self.pod["initContainers"][0]
        self.assertEqual(init["command"][:2], ["sh", "-c"])
        self.assertTrue(init["command"][2].startswith("\n".join([
            "set -e",
            "cp /defaults/configuration.yaml /config/configuration.yaml",
            "cp /defaults/egolf.yaml /config/egolf.yaml",
            "mkdir -p /config/.storage",
            "[ -e /config/.storage/http ] || cp /defaults/http.json /config/.storage/http",
            "[ -e /config/automations.yaml ] || echo '[]' > /config/automations.yaml",
            "[ -e /config/scripts.yaml ] || echo '{}' > /config/scripts.yaml",
            "[ -e /config/scenes.yaml ] || echo '[]' > /config/scenes.yaml",
        ]) + "\n"))
        mounts = {m["mountPath"]: m for m in init["volumeMounts"]}
        self.assertEqual(mounts["/config"]["name"], "config")
        volumes = {v["name"]: v for v in self.pod["volumes"]}
        self.assertEqual(volumes[mounts["/defaults"]["name"]]["configMap"]["name"], "home-assistant-configuration")

    COMPONENTS = (
        ("tibber_pulse_mqtt", "TIBBER_PULSE_MQTT", "mrhedstrom/ha-tibber-pulse-mqtt", "tibber_pulse_mqtt.zip"),
        ("zaptec", "ZAPTEC", "custom-components/zaptec", "zaptec.zip"),
    )

    def test_custom_integration_pin(self):
        init = self.pod["initContainers"][0]
        env = {e["name"]: e["value"] for e in init["env"]}
        lines = (HA / "home-assistant.yaml").read_text().splitlines()
        script = init["command"][2]
        for domain, prefix, repo, asset in self.COMPONENTS:
            with self.subTest(domain):
                self.assertRegex(env[f"{prefix}_VERSION"], r"^[0-9]+\.[0-9]+\.[0-9]+$")
                self.assertRegex(env[f"{prefix}_SHA256"], r"^[0-9a-f]{64}$")
                name = lines.index(f"            - name: {prefix}_VERSION")
                self.assertEqual(lines[name + 1].strip(), f"# renovate: datasource=github-releases depName={repo}")
                self.assertEqual(lines[name + 2].strip(), f'value: "{env[f"{prefix}_VERSION"]}"')
                self.assertIn(f'install_component {domain} {repo} {asset} "${prefix}_VERSION" "${prefix}_SHA256"', script)
        self.assertIn("https://github.com/$2/releases/download/v$4/$3", script)
        self.assertNotRegex(script, r"sha256sum -c[^\n]*\|\|")

    def run_init(self, root, shas, wget_fails=False):
        script = re.sub(r"(?<![\w/])/(config|defaults|tmp)\b", lambda m: f"{root}/{m[1]}", self.pod["initContainers"][0]["command"][2])
        wget = root / "bin/wget"
        wget.write_text(f"#!/bin/sh\ntouch {root}/wget-called\n" + ("exit 4\n" if wget_fails else f'while [ "$1" != -O ]; do shift; done\ncp {root}/fixtures/$(basename "$2") "$2"\n'))
        wget.chmod(0o755)
        env = {**os.environ, "PATH": f"{root}/bin:{os.environ['PATH']}"}
        for domain, prefix, _, _ in self.COMPONENTS:
            env[f"{prefix}_VERSION"] = "9.9.9"
            env[f"{prefix}_SHA256"] = shas[domain]
        return subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True).returncode

    def test_custom_integration_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for directory in ("bin", "defaults", "tmp", "config", "fixtures"):
                (root / directory).mkdir()
            (root / "defaults/configuration.yaml").write_text("default_config:\n")
            (root / "defaults/http.json").write_text("{}")
            (root / "defaults/egolf.yaml").write_text("{}\n")
            shas = {}
            for domain, _, _, asset in self.COMPONENTS:
                with zipfile.ZipFile(root / "fixtures" / asset, "w") as archive:
                    archive.writestr("manifest.json", '{\n  "domain": "%s",\n  "version": "9.9.9"\n}\n' % domain)
                    archive.writestr("__init__.py", "")
                shas[domain] = hashlib.sha256((root / "fixtures" / asset).read_bytes()).hexdigest()
            targets = {domain: root / "config/custom_components" / domain for domain, *_ in self.COMPONENTS}

            def installed_old():
                for target in targets.values():
                    target.mkdir(parents=True, exist_ok=True)
                    (target / "manifest.json").write_text('{"version": "9.9.8"}')

            for domain in targets:
                installed_old()
                with self.subTest(f"{domain} checksum mismatch fails and keeps the old copy"):
                    self.assertNotEqual(self.run_init(root, {**shas, domain: "0" * 64}), 0)
                    self.assertIn('"9.9.8"', (targets[domain] / "manifest.json").read_text())

            installed_old()

            self.assertEqual(self.run_init(root, shas, wget_fails=True), 0)
            for target in targets.values():
                self.assertIn('"9.9.8"', (target / "manifest.json").read_text())

            self.assertEqual(self.run_init(root, shas), 0)
            for target in targets.values():
                self.assertIn('"version": "9.9.9"', (target / "manifest.json").read_text())
                self.assertTrue((target / "__init__.py").exists())
            self.assertEqual(sorted(p.name for p in (root / "config/custom_components").iterdir()), sorted(targets))

            (root / "wget-called").unlink()
            self.assertEqual(self.run_init(root, shas, wget_fails=True), 0)
            self.assertFalse((root / "wget-called").exists())

            for path in (root / "config/custom_components", root / "config/configuration.yaml"):
                subprocess.run(["rm", "-rf", str(path)], check=True)
            self.assertEqual(self.run_init(root, shas, wget_fails=True), 0)
            self.assertTrue((root / "config/configuration.yaml").exists())

    def test_bridge_binary_sensor(self):
        configmap = resource("home-assistant.yaml", "ConfigMap", "home-assistant-configuration")
        config = yaml.load(configmap["data"]["configuration.yaml"], Loader=HomeAssistantLoader)
        self.assertEqual(config["mqtt"], {"binary_sensor": [{
            "name": "Tibber bridge",
            "unique_id": "tibber_bridge",
            "state_topic": "homelab/tibber-bridge/state",
            "payload_on": "1",
            "payload_off": "0",
            "device_class": "connectivity",
        }]})

    def test_renovate_custom_integrations(self):
        renovate = json.loads((ROOT / ".github/renovate.json").read_text())
        manager = next(m for m in renovate["customManagers"] if any("36\\.home-assistant/home-assistant" in p for p in m["managerFilePatterns"]))
        self.assertEqual(manager["customType"], "regex")
        for repo in ("mrhedstrom/ha-tibber-pulse-mqtt", "custom-components/zaptec"):
            rule = next(r for r in renovate["packageRules"] if repo in r.get("matchPackageNames", []))
            self.assertIs(rule["automerge"], False)
            self.assertEqual(rule["labels"], ["renovate", "needs-checksum"])

    def test_ha_network_policy(self):
        policy = next(d for d in documents("network-policy.yaml") if d["spec"]["podSelector"] == {"matchLabels": {"app": "home-assistant"}})
        self.assertEqual(policy["spec"]["policyTypes"], ["Ingress"])
        self.assertEqual(policy["spec"]["ingress"], [{
            "from": [{
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "traefik-system"}},
                "podSelector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}},
            }],
            "ports": [{"port": 8123, "protocol": "TCP"}],
        }])

    def test_secrets_generator(self):
        generator = documents("ksops-generator.yaml")[0]
        self.assertEqual(generator["metadata"]["name"], "home-assistant-secrets")
        self.assertEqual(generator["files"], ["secrets.enc.yaml"])
        self.assertIn("ksops-generator.yaml", documents("kustomization.yaml")[0]["generators"])
        self.assertNotIn("namespace", documents("kustomization.yaml")[0])

    def test_secrets_encrypted(self):
        secrets = {(doc["metadata"]["namespace"], doc["metadata"]["name"]): doc for doc in documents("secrets.enc.yaml")}
        self.assertLessEqual({("postgres-system", "homeassistant-db-role"), ("home-assistant", "home-assistant-secrets"), ("home-assistant", "esphome-auth"), ("home-assistant", "roborock-config"), ("home-assistant", "mosquitto-config")}, set(secrets))
        self.assertEqual(set(secrets[("home-assistant", "mosquitto-config")]["stringData"]), {"mosquitto.conf", "passwd", "acl", "ca.crt", "client.crt", "client.key"})
        self.assertEqual(secrets[("home-assistant", "mosquitto-config")]["metadata"]["labels"], {"app": "mosquitto"})
        self.assertEqual(secrets[("home-assistant", "mosquitto-config")]["metadata"]["annotations"], {"argocd.argoproj.io/sync-wave": "-2"})
        self.assertEqual(set(secrets[("home-assistant", "roborock-config")]["stringData"]), {"config.toml"})
        self.assertEqual(set(secrets[("home-assistant", "esphome-auth")]["stringData"]), {"ESPHOME_USERNAME", "ESPHOME_PASSWORD"})
        self.assertEqual(secrets[("postgres-system", "homeassistant-db-role")]["type"], "kubernetes.io/basic-auth")
        self.assertEqual(set(secrets[("postgres-system", "homeassistant-db-role")]["stringData"]), {"username", "password"})
        self.assertEqual(set(secrets[("home-assistant", "home-assistant-secrets")]["stringData"]), {"secrets.yaml"})
        for secret in secrets.values():
            self.assertIn("sops", secret)
            for value in {**secret.get("data", {}), **secret.get("stringData", {})}.values():
                self.assertRegex(value, r"^ENC\[")

    def test_mosquitto_probe(self):
        values = yaml.safe_load((ROOT / "2-k3s/10.observability/blackbox-values.yaml").read_text())
        self.assertEqual(values["config"]["modules"]["tcp_connect"]["prober"], "tcp")
        self.assertIn({"name": "mosquitto", "url": "192.168.10.117:1883", "module": "tcp_connect"}, values["serviceMonitor"]["targets"])
        relabels = values["serviceMonitor"]["defaults"]["additionalRelabeling"]
        rule = {"sourceLabels": ["__param_target"], "regex": r"192\.168\.10\.117:1883", "action": "replace", "targetLabel": "job", "replacement": "mosquitto-probe"}
        self.assertIn(rule, relabels)
        self.assertGreater(relabels.index(rule), relabels.index({"targetLabel": "job", "action": "replace", "replacement": "tls-probe"}))

    def test_probes_and_dns(self):
        targets = yaml.safe_load((ROOT / "2-k3s/10.observability/blackbox-values.yaml").read_text())["serviceMonitor"]["targets"]
        for target in (
            {"name": "home-assistant", "url": "https://192.168.10.102", "hostname": "ha.epaflix.com"},
            {"name": "esphome", "url": "https://192.168.10.102", "hostname": "esphome.epaflix.com"},
            {"name": "roborock", "url": "https://192.168.10.116:555/admin", "hostname": "api-rr.epaflix.com"},
        ):
            self.assertIn(target, targets)
        pihole = ROOT / "1-proxmox/pihole"
        dnsmasq = (pihole / "dnsmasq.d/10-epaflix.conf").read_text().splitlines()
        unbound = [line.strip() for line in (pihole / "unbound-no-aaaa-leak.conf").read_text().splitlines()]
        for host, ip in (("ha.epaflix.com", "192.168.10.102"), ("esphome.epaflix.com", "192.168.10.102"), ("api-rr.epaflix.com", "192.168.10.116")):
            self.assertIn(f"address=/{host}/{ip}", dnsmasq)
            self.assertIn(f'local-zone: "{host}." static', unbound)
        rules = yaml.safe_load((ROOT / "2-k3s/10.observability/alertmanager-config/tls-alerts.yaml").read_text())["spec"]["groups"][0]["rules"]
        self.assertEqual(len(rules), 3)
        for rule in rules:
            self.assertIn("{{ $labels.target }}", rule["annotations"]["description"])


if __name__ == "__main__":
    unittest.main()
