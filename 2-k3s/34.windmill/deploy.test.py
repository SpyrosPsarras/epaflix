#!/usr/bin/env python3
import pathlib
import json
import re
import unittest

import yaml

HERE = pathlib.Path(__file__).resolve().parent
LABELS = {"app.kubernetes.io/name": "windmill-deploy"}
PORT_8000 = [{"protocol": "TCP", "port": 8000}]


def documents(name):
    return [doc for doc in yaml.safe_load_all((HERE / name).read_text()) if doc]


def resource(name, kind, metadata_name):
    return next(doc for doc in documents(name) if doc["kind"] == kind and doc["metadata"]["name"] == metadata_name)


def ports(rule):
    return [{"protocol": port["protocol"], "port": port["port"]} for port in rule["ports"]]


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.cronjob = resource("deploy-cronjob.yaml", "CronJob", "windmill-deploy")
        self.job = self.cronjob["spec"]["jobTemplate"]["spec"]
        self.pod = self.job["template"]["spec"]
        self.container = self.pod["containers"][0]
        self.script = (HERE / "deploy.mjs").read_text()

    def test_schedule_and_history(self):
        spec = self.cronjob["spec"]
        self.assertEqual(spec["schedule"], "*/10 * * * *")
        self.assertEqual(spec["concurrencyPolicy"], "Forbid")
        self.assertEqual(spec["successfulJobsHistoryLimit"], 1)
        self.assertEqual(spec["failedJobsHistoryLimit"], 3)
        self.assertLessEqual(self.job["activeDeadlineSeconds"], 300)

    def test_pod_is_locked_down(self):
        self.assertIs(self.pod["automountServiceAccountToken"], False)
        self.assertIs(self.pod["securityContext"]["runAsNonRoot"], True)
        self.assertNotEqual(self.pod["securityContext"]["runAsUser"], 0)
        security = self.container["securityContext"]
        self.assertIs(security["readOnlyRootFilesystem"], True)
        self.assertIs(security["allowPrivilegeEscalation"], False)
        self.assertEqual(security["capabilities"]["drop"], ["ALL"])
        self.assertIn("memory", self.container["resources"]["limits"])
        self.assertEqual(self.job["template"]["metadata"]["labels"], LABELS)

    def test_image_is_pinned_node_slim(self):
        self.assertRegex(self.container["image"], r"^docker\.io/library/node:\d+\.\d+\.\d+-trixie-slim@sha256:[0-9a-f]{64}$")

    def test_writable_paths_are_empty_dirs(self):
        volumes = {volume["name"]: volume for volume in self.pod["volumes"]}
        mounts = {mount["mountPath"]: mount["name"] for mount in self.container["volumeMounts"]}
        self.assertIn("emptyDir", volumes[mounts["/tmp"]])
        env = {entry["name"]: entry for entry in self.container["env"]}
        self.assertEqual(env["HOME"]["value"], "/tmp")
        self.assertEqual(volumes[mounts["/scripts"]]["configMap"]["name"], "windmill-deploy-script")
        generator = documents("kustomization.yaml")[0]["configMapGenerator"]
        self.assertIn({"name": "windmill-deploy-script", "namespace": "windmill", "files": ["deploy.mjs", "package.json=deploy-cli/package.json", "package-lock.json=deploy-cli/package-lock.json"]}, generator)

    def test_token_comes_only_from_the_secret(self):
        env = {entry["name"]: entry for entry in self.container["env"]}
        self.assertEqual(env["WM_TOKEN"], {"name": "WM_TOKEN", "valueFrom": {"secretKeyRef": {"name": "windmill-deploy", "key": "WINDMILL_TOKEN"}}})
        self.assertEqual(env["WM_WORKSPACE"]["value"], "screener")
        self.assertEqual(env["BASE_INTERNAL_URL"]["value"], "http://windmill-app.windmill.svc.cluster.local:8000")
        self.assertNotIn("--token", self.script)
        self.assertNotIn("--token", str(self.container.get("command")) + str(self.container.get("args")))

    def test_push_flags_and_cli_matches_server(self):
        for flag in ["--yes", "--auto-metadata", "--keep-deleted", "--ignore-scripts", "ci"]:
            self.assertIn(f'"{flag}"', self.script)
        lock = json.loads((HERE / "deploy-cli/package-lock.json").read_text())
        cli = lock["packages"]["node_modules/windmill-cli"]["version"]
        self.assertEqual(json.loads((HERE / "deploy-cli/package.json").read_text())["dependencies"]["windmill-cli"], cli)
        server = yaml.safe_load((HERE / "values.yaml").read_text())["windmill"]["tag"].split("@")[0]
        self.assertEqual(cli, server)

    def test_deploy_network_policy(self):
        policy = resource("network-policy.yaml", "NetworkPolicy", "windmill-deploy")["spec"]
        workers = resource("network-policy.yaml", "NetworkPolicy", "windmill-workers")["spec"]
        self.assertEqual(policy["podSelector"], {"matchLabels": LABELS})
        self.assertEqual(policy["policyTypes"], ["Ingress", "Egress"])
        self.assertEqual(policy["ingress"], [])
        public = next(rule["to"][0]["ipBlock"] for rule in workers["egress"] if "ipBlock" in rule["to"][0])
        dns, app, internet = policy["egress"]
        self.assertEqual(dns["to"][0]["podSelector"], {"matchLabels": {"k8s-app": "kube-dns"}})
        self.assertEqual(ports(dns), [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}])
        self.assertEqual(app["to"], [{"podSelector": {"matchLabels": {"app": "windmill-app"}}}])
        self.assertEqual(ports(app), PORT_8000)
        self.assertEqual(internet["to"], [{"ipBlock": public}])
        self.assertEqual(ports(internet), [{"protocol": "TCP", "port": 443}])

    def test_windmill_app_admits_traefik_and_the_deploy_job_only(self):
        rules = resource("network-policy.yaml", "NetworkPolicy", "windmill-app-ingress")["spec"]["ingress"]
        self.assertEqual([ports(rule) for rule in rules], [PORT_8000, PORT_8000])
        self.assertEqual(rules[0]["from"][0]["namespaceSelector"], {"matchLabels": {"kubernetes.io/metadata.name": "traefik-system"}})
        self.assertEqual(rules[1]["from"], [{"podSelector": {"matchLabels": LABELS}}])


if __name__ == "__main__":
    unittest.main()
