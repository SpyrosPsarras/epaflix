#!/usr/bin/env python3
import unittest
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
        for image in images:
            self.assertRegex(image["newTag"], r"^[^@]+@sha256:[0-9a-f]{64}$")

    def test_routes_internal_only(self):
        routes = [doc for doc in documents("ingressroute.yaml") if doc["kind"] == "IngressRoute"]
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
        self.assertIs(config["http"]["use_x_forwarded_for"], True)
        self.assertEqual(config["http"]["trusted_proxies"], ["10.42.0.0/16"])
        self.assertEqual(config["recorder"]["db_url"], ("secret", "recorder_db_url"))
        self.assertIsInstance(config["recorder"]["db_url"], Tagged)
        self.assertEqual(config["recorder"]["db_max_retries"], 100)
        for key, name in {"automation": "automations.yaml", "script": "scripts.yaml", "scene": "scenes.yaml"}.items():
            self.assertEqual(config[key], ("include", name))

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
        self.assertEqual(init["command"], ["sh", "-c", "\n".join([
            "set -e",
            "cp /defaults/configuration.yaml /config/configuration.yaml",
            "[ -e /config/automations.yaml ] || echo '[]' > /config/automations.yaml",
            "[ -e /config/scripts.yaml ] || echo '{}' > /config/scripts.yaml",
            "[ -e /config/scenes.yaml ] || echo '[]' > /config/scenes.yaml",
        ]) + "\n"])
        mounts = {m["mountPath"]: m for m in init["volumeMounts"]}
        self.assertEqual(mounts["/config"]["name"], "config")
        volumes = {v["name"]: v for v in self.pod["volumes"]}
        self.assertEqual(volumes[mounts["/defaults"]["name"]]["configMap"]["name"], "home-assistant-configuration")

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
        self.assertLessEqual({("postgres-system", "homeassistant-db-role"), ("home-assistant", "home-assistant-secrets"), ("home-assistant", "esphome-auth")}, set(secrets))
        self.assertEqual(set(secrets[("home-assistant", "esphome-auth")]["stringData"]), {"ESPHOME_USERNAME", "ESPHOME_PASSWORD"})
        self.assertEqual(secrets[("postgres-system", "homeassistant-db-role")]["type"], "kubernetes.io/basic-auth")
        self.assertEqual(set(secrets[("postgres-system", "homeassistant-db-role")]["stringData"]), {"username", "password"})
        self.assertEqual(set(secrets[("home-assistant", "home-assistant-secrets")]["stringData"]), {"secrets.yaml"})
        for secret in secrets.values():
            self.assertIn("sops", secret)
            for value in {**secret.get("data", {}), **secret.get("stringData", {})}.values():
                self.assertRegex(value, r"^ENC\[")


if __name__ == "__main__":
    unittest.main()
