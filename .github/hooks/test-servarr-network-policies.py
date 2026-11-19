#!/usr/bin/env python3
import pathlib
import unittest

import yaml

TIER = pathlib.Path(__file__).resolve().parents[2] / "2-k3s/08.servarr"


def deployed(directory):
    for entry in yaml.safe_load((directory / "kustomization.yaml").read_text())["resources"]:
        path = directory / entry
        if path.is_dir():
            yield from deployed(path)
        else:
            yield from (doc for doc in yaml.safe_load_all(path.read_text()) if doc)


DOCS = list(deployed(TIER))
SAME_NAMESPACE = {"podSelector": {}}
TRAEFIK = {
    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "traefik-system"}},
    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}},
}
PROMETHEUS = {
    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "observability"}},
    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "prometheus"}},
}
# vpn-picker's only port is its LoadBalancer port, which arrives from a node address.
OPEN = {"vpn-picker"}
# These policies name each caller instead of admitting the whole namespace.
NAMED_CALLERS = {"lidarr", "navidrome", "digarr", "tdarr"}
# qBittorrent's peers arrive inside the VPN tunnel in the pod, not over the pod network.
CLOSED = {("qbittorrent", "bt-tcp"), ("qbittorrent", "bt-udp")}


def kind(name):
    return [doc for doc in DOCS if doc["kind"] == name]


SERVICES = {doc["metadata"]["name"]: doc["spec"] for doc in kind("Service")}
APPS = sorted({service["selector"]["app"] for service in SERVICES.values()} - OPEN)
SCRAPED = {
    (monitor["spec"]["selector"]["matchLabels"]["app"], endpoint["port"])
    for monitor in kind("ServiceMonitor")
    for endpoint in monitor["spec"]["endpoints"]
}


def pod_port(service, reference):
    spec = SERVICES[service]
    port = next(port for port in spec["ports"] if reference in (port["port"], port["name"]))
    app = spec["selector"]["app"]
    target = port.get("targetPort", port["port"])
    if isinstance(target, str):
        deployment = next(doc for doc in kind("Deployment") if doc["metadata"]["name"] == app)
        target = next(
            port["containerPort"]
            for container in deployment["spec"]["template"]["spec"]["containers"]
            for port in container.get("ports", [])
            if port.get("name") == target
        )
    return app, {"port": target, "protocol": port.get("protocol", "TCP")}


def admitted(app, port):
    return [
        peer
        for policy in kind("NetworkPolicy")
        if policy["metadata"]["name"] == f"{app}-ingress"
        for rule in policy["spec"].get("ingress") or []
        if port in rule.get("ports", [])
        for peer in rule.get("from", [])
    ]


class ServarrNetworkPolicyTests(unittest.TestCase):
    def test_each_app_has_exactly_one_ingress_only_policy(self):
        policies = kind("NetworkPolicy")
        expected = [f"{app}-ingress" for app in APPS]
        if "tdarr" in APPS:
            expected.append("tdarr-node-egress")
        self.assertCountEqual([policy["metadata"]["name"] for policy in policies], expected)
        for policy in policies:
            name, spec = policy["metadata"]["name"], policy["spec"]
            with self.subTest(policy=name):
                if name == "tdarr-node-egress":
                    self.assertEqual(spec["podSelector"], {"matchLabels": {"app": "tdarr-node"}})
                    self.assertEqual(spec["policyTypes"], ["Egress"])
                    self.assertNotIn("ingress", spec)
                    continue
                self.assertEqual(spec["podSelector"], {"matchLabels": {"app": name.removesuffix("-ingress")}})
                self.assertEqual(spec["policyTypes"], ["Ingress"])
                self.assertNotIn("egress", spec)
                self.assertTrue(spec.get("ingress"))
                for rule in spec.get("ingress") or []:
                    self.assertTrue(rule.get("from"))
                    self.assertTrue(rule.get("ports"))

    def test_every_service_port_admits_its_callers(self):
        for service, spec in SERVICES.items():
            for port in spec["ports"]:
                app, target = pod_port(service, port["name"])
                if app in OPEN:
                    continue
                with self.subTest(app=app, port=port["name"]):
                    peers = admitted(app, target)
                    if (app, port["name"]) in CLOSED:
                        self.assertEqual(peers, [])
                        continue
                    self.assertTrue(peers)
                    if app not in NAMED_CALLERS and (app, port["name"]) not in SCRAPED:
                        self.assertIn(SAME_NAMESPACE, peers)

    def test_routed_apps_admit_traefik(self):
        for route in kind("IngressRoute"):
            for rule in route["spec"]["routes"]:
                for service in rule["services"]:
                    if "namespace" in service:
                        continue
                    with self.subTest(route=route["metadata"]["name"]):
                        self.assertIn(TRAEFIK, admitted(*pod_port(service["name"], service["port"])))

    def test_scraped_apps_admit_prometheus(self):
        for app, port in sorted(SCRAPED):
            if app in OPEN:
                continue
            with self.subTest(app=app, port=port):
                self.assertIn(PROMETHEUS, admitted(*pod_port(app, port)))


if __name__ == "__main__":
    unittest.main()
