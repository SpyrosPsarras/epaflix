#!/usr/bin/env python3
import re
import unittest
from pathlib import Path

import yaml

SERVARR = Path(__file__).resolve().parents[1]
DTS_PROFILES = {
    "radarr": ("UHD Bluray + WEB", {
        "DTS": "1c1a4c5e823891c75bc50380a6866f73",
        "DTS-ES": "f9f847ac70a0af62ea4a08280b859636",
        "DTS-HD HRA": "8e109e50e0a0b83a5098b056e13bf6db",
        "DTS-HD MA": "dcf3ec6938fa32445f590a4da84256cd",
        "DTS X": "2f22d89048b01681dde8afe203bf2e95",
    }),
    "sonarr": ("WEB-2160p", {
        "DTS": "5964f2a8b3be407d083498e4459d05d0",
        "DTS-ES": "c1a25cd67b5d2e08287c957b1eb903ec",
        "DTS-HD HRA": "cfa5fbd8f02a86fc55d8d223d06a5e1f",
        "DTS-HD MA": "c429417a57ea8c41d57e6990a8b0033f",
        "DTS X": "9d00418ba386a083fbf4d58235fc37ef",
    }),
}


class ConfigarrLoader(yaml.SafeLoader):
    pass


ConfigarrLoader.add_constructor("!env", ConfigarrLoader.construct_scalar)


def documents(path):
    return [doc for doc in yaml.safe_load_all((SERVARR / path).read_text()) if doc]


def resource(path, kind, name):
    return next(doc for doc in documents(path) if doc["kind"] == kind and doc["metadata"]["name"] == name)


def configarr():
    config_map = resource("configarr/configarr.yaml", "ConfigMap", "configarr-config")
    return yaml.load(config_map["data"]["config.yml"], Loader=ConfigarrLoader)


def dts_scores(config, app):
    profile, format_ids = DTS_PROFILES[app]
    return {
        name: [
            assignment.get("score")
            for entry in config[app]["main"]["custom_formats"] if trash_id in entry["trash_ids"]
            for assignment in entry["assign_scores_to"] if assignment["name"] == profile
        ]
        for name, trash_id in format_ids.items()
    }


def min_format_score(config, app):
    profile = DTS_PROFILES[app][0]
    return next(p for p in config[app]["main"]["quality_profiles"] if p["name"] == profile)["min_format_score"]


class ConfigarrTests(unittest.TestCase):
    def test_radarr_ranks_dts_last_but_accepts_it(self):
        config = configarr()
        self.assertEqual(dts_scores(config, "radarr"), dict.fromkeys(DTS_PROFILES["radarr"][1], [-1]))
        self.assertGreaterEqual(-1, min_format_score(config, "radarr"))

    def test_sonarr_leaves_dts_unscored(self):
        # The profile floor is +1 and TRaSH gives DTS 0 there, so any penalty rejects DTS-only releases; Tdarr fixes them instead.
        config = configarr()
        self.assertEqual(dts_scores(config, "sonarr"), dict.fromkeys(DTS_PROFILES["sonarr"][1], []))

    def test_lidarr_accepts_flac_but_ranks_it_last(self):
        profile, = configarr()["lidarr"]["main"]["quality_profiles"]
        self.assertEqual([q["name"] for q in profile["qualities"]][-2:], ["FLAC", "FLAC 24bit"])
        self.assertEqual(profile["upgrade"]["until_quality"], "320")


class TdarrTests(unittest.TestCase):
    def pod(self, kind, name):
        self.assertTrue((SERVARR / "tdarr/tdarr.yaml").is_file(), "Tdarr workloads are missing")
        return resource("tdarr/tdarr.yaml", kind, name)["spec"]["template"]["spec"]

    def pods(self):
        return [self.pod("Deployment", "tdarr"), self.pod("DaemonSet", "tdarr-node")]

    def media_mounts(self, pod):
        media = {
            volume["name"] for volume in pod["volumes"]
            if volume.get("persistentVolumeClaim", {}).get("claimName") == "servarr-media"
        }
        return {
            mount["mountPath"]: mount.get("subPath")
            for mount in pod["containers"][0]["volumeMounts"] if mount["name"] in media
        }

    def test_tdarr_images_pinned(self):
        kustomization, = documents("kustomization.yaml")
        self.assertTrue({"tdarr/tdarr.yaml", "tdarr/network-policy.yaml", "tdarr/ingressroute.yaml"}
                        <= set(kustomization["resources"]))
        images = {image["name"]: image.get("newTag", "") for image in kustomization["images"]}
        tags = [images.get(f"ghcr.io/haveagitgat/{name}", "") for name in ("tdarr", "tdarr_node")]
        for tag in tags:
            self.assertRegex(tag, r"^\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")
        self.assertEqual(tags[0].split("@")[0], tags[1].split("@")[0])

    def test_node_daemonset_per_worker(self):
        pod = self.pod("DaemonSet", "tdarr-node")
        terms = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
        self.assertEqual(terms, [{"matchExpressions": [{
            "key": "kubernetes.io/hostname", "operator": "In",
            "values": [f"k3s-worker-{number}" for number in range(61, 66)],
        }]}])
        self.assertIn({"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}, pod["tolerations"])
        env = {item["name"]: item for item in pod["containers"][0]["env"]}
        self.assertEqual(env["nodeName"]["valueFrom"]["fieldRef"]["fieldPath"], "spec.nodeName")
        for count, value in {"transcodecpuWorkers": "1", "healthcheckcpuWorkers": "1",
                             "transcodegpuWorkers": "0", "healthcheckgpuWorkers": "0"}.items():
            self.assertEqual(env[count]["value"], value)
        self.assertEqual(env["serverURL"]["value"], "http://tdarr.servarr.svc.cluster.local:8266")

    def test_resources_bounded(self):
        for pod in self.pods():
            for container in pod["containers"] + pod.get("initContainers", []):
                self.assertTrue(container["resources"]["limits"]["memory"], container["name"])
        node = self.pod("DaemonSet", "tdarr-node")["containers"][0]["resources"]["limits"]
        self.assertEqual(node, {"memory": "4Gi", "cpu": "2"})

    def test_only_library_subpaths_are_mounted(self):
        for pod in self.pods():
            self.assertEqual(self.media_mounts(pod), {
                "/media/movies": "movies", "/media/tvshows": "tvshows", "/media/animes": "animes",
            })
            for volume in pod["volumes"]:
                self.assertNotIn("emptyDir", volume, volume["name"])

    def test_no_sa_token(self):
        for pod in self.pods():
            self.assertIs(pod.get("automountServiceAccountToken"), False)

    def test_plugin_fetch_pinned(self):
        pod = self.pod("Deployment", "tdarr")
        init = next(container for container in pod["initContainers"] if container["name"] == "fetch-plugins")
        script = init["args"][-1]
        sources = re.findall(
            r"https://raw\.githubusercontent\.com/nichols89ben/Tdarr_DoVi_Processing/"
            r"([0-9a-f]{40})/FlowPlugins/LocalFlowPlugins/DoVI/(\w+)/1\.0\.0/index\.js", script,
        )
        self.assertCountEqual([plugin for _, plugin in sources], ["checkHDRType", "checkDoViProfile"])
        self.assertEqual(len({revision for revision, _ in sources}), 1)
        mount = next(mount for mount in init["volumeMounts"] if mount["name"] == "plugin-checksums")
        volume = next(volume for volume in pod["volumes"] if volume["name"] == mount["name"])
        self.assertIn(f"sha256sum -c {mount['mountPath']}/plugins.sha256", script)
        config = volume["configMap"]["name"]
        kustomization, = documents("kustomization.yaml")
        generator = next(entry for entry in kustomization["configMapGenerator"] if entry["name"] == config)
        self.assertIn("tdarr/plugins.sha256", generator["files"])
        checksums = (SERVARR / "tdarr/plugins.sha256").read_text().splitlines()
        self.assertCountEqual([line.split()[1] for line in checksums],
                              [f"{name}/1.0.0/index.js" for _, name in sources])
        for line in checksums:
            self.assertRegex(line, r"^[0-9a-f]{64}  ")

    def test_media_proxy_service_targets_sidecar(self):
        pod = self.pod("Deployment", "tdarr")
        sidecar = next((container for container in pod["containers"] if container["name"] == "media-proxy"), None)
        self.assertIsNotNone(sidecar, "Tdarr media proxy sidecar is missing")
        notify = next(container for container in self.pod("DaemonSet", "tdarr-node")["containers"]
                      if container["name"] == "notify-proxy")
        self.assertEqual(sidecar["image"], notify["image"])
        self.assertEqual(sidecar["securityContext"], notify["securityContext"])
        self.assertEqual(sidecar["resources"], notify["resources"])
        self.assertEqual(sidecar["ports"], [{"name": "media-proxy", "containerPort": 8267}])
        self.assertEqual(sidecar["command"], ["python3", "/proxy/media-proxy.py"])
        self.assertEqual(sidecar["volumeMounts"], [{"name": "media-proxy", "mountPath": "/proxy", "readOnly": True}])
        self.assertNotIn("env", sidecar)
        self.assertNotIn("envFrom", sidecar)
        self.assertIn({"name": "media-proxy", "configMap": {"name": "tdarr-media-proxy"}}, pod["volumes"])
        kustomization, = documents("kustomization.yaml")
        self.assertIn({"name": "tdarr-media-proxy", "files": ["tdarr/media-proxy.py"]},
                      kustomization["configMapGenerator"])
        ports = resource("tdarr/tdarr.yaml", "Service", "tdarr")["spec"]["ports"]
        self.assertIn({"name": "media-proxy", "port": 8267, "targetPort": "media-proxy"}, ports)

    def test_network_policy(self):
        path = "tdarr/network-policy.yaml"
        self.assertTrue((SERVARR / path).is_file(), "Tdarr network policies are missing")

        def peer(app_labels, namespace=None):
            selector = {"podSelector": {"matchLabels": app_labels}}
            if namespace:
                selector["namespaceSelector"] = {"matchLabels": {"kubernetes.io/metadata.name": namespace}}
            return selector

        def tcp(port):
            return {"port": port, "protocol": "TCP"}

        server = resource(path, "NetworkPolicy", "tdarr-ingress")["spec"]
        self.assertEqual(server["podSelector"], peer({"app": "tdarr"})["podSelector"])
        self.assertEqual(server["policyTypes"], ["Ingress"])
        self.assertCountEqual(server["ingress"], [
            {"from": [peer({"app.kubernetes.io/name": "traefik"}, "traefik-system")], "ports": [tcp(8265)]},
            {"from": [peer({"app": "tdarr-node"})], "ports": [tcp(8266)]},
            {"from": [peer({"app.kubernetes.io/name": "openclaw"}, "discord-media")], "ports": [tcp(8267)]},
        ])
        node = resource(path, "NetworkPolicy", "tdarr-node-egress")["spec"]
        self.assertEqual(node["podSelector"], peer({"app": "tdarr-node"})["podSelector"])
        self.assertEqual(node["policyTypes"], ["Egress"])
        self.assertCountEqual(node["egress"], [
            {"to": [peer({"app": "tdarr"})], "ports": [tcp(8266)]},
            {"to": [peer({"app": "radarr"})], "ports": [tcp(7878)]},
            {"to": [peer({"app": "sonarr"})], "ports": [tcp(8989)]},
            {"to": [peer({"k8s-app": "kube-dns"}, "kube-system")],
             "ports": [{"port": 53, "protocol": "UDP"}, tcp(53)]},
        ])


def peer(app_labels, namespace=None):
    selector = {"podSelector": {"matchLabels": app_labels}}
    if namespace:
        selector["namespaceSelector"] = {"matchLabels": {"kubernetes.io/metadata.name": namespace}}
    return selector


def env_of(container):
    return {item["name"]: item.get("value", item.get("valueFrom")) for item in container.get("env", [])}


def pinned(name):
    kustomization, = documents("kustomization.yaml")
    return next(image["newTag"] for image in kustomization["images"] if image["name"] == name)


class SlskdTests(unittest.TestCase):
    def pod(self):
        return resource("slskd/slskd.yaml", "Deployment", "slskd")["spec"]["template"]["spec"]

    def container(self, name):
        pod = self.pod()
        return next(c for c in pod["containers"] + pod.get("initContainers", []) if c["name"] == name)

    def test_vpn_sidecar_gates_slskd(self):
        gluetun = self.container("gluetun")
        self.assertIn(gluetun, self.pod()["initContainers"])
        self.assertEqual(gluetun["restartPolicy"], "Always")
        self.assertIn("startupProbe", gluetun)
        self.assertLessEqual({
            "VPN_SERVICE_PROVIDER": "airvpn", "VPN_TYPE": "wireguard",
            "SERVER_COUNTRIES": "Netherlands,Germany,Sweden", "FIREWALL_VPN_INPUT_PORTS": "46327",
            "FIREWALL_INPUT_PORTS": "5030,9999", "FIREWALL_OUTBOUND_SUBNETS": "10.42.0.0/16",
        }.items(), env_of(gluetun).items())
        self.assertLessEqual({
            "SLSKD_VPN": "true", "SLSKD_VPN_GLUETUN_URL": "http://127.0.0.1:8000",
            "SLSKD_SLSK_LISTEN_PORT": "46327",
        }.items(), env_of(self.container("slskd")).items())

    def test_download_dirs_created_before_slskd(self):
        init = self.pod()["initContainers"]
        self.assertEqual(init[0]["name"], "download-dirs")
        self.assertIn("mkdir -p /media/downloads/soulseek /media/downloads/soulseek-incomplete",
                      " ".join(init[0]["command"]))
        self.assertEqual(init[0]["securityContext"]["runAsUser"], 568)

    def test_no_service_links(self):
        pod = self.pod()
        self.assertIs(pod["enableServiceLinks"], False)
        self.assertEqual(pod["dnsConfig"]["nameservers"], ["127.0.0.1"])

    def test_library_read_only(self):
        media = {v["name"] for v in self.pod()["volumes"]
                 if v.get("persistentVolumeClaim", {}).get("claimName") == "servarr-media"}
        mounts = {m["mountPath"]: (m.get("subPath"), m.get("readOnly", False))
                  for m in self.container("slskd")["volumeMounts"] if m["name"] in media}
        self.assertEqual(mounts, {"/media/music": ("music", True), "/media/downloads": ("downloads", False)})

    def test_paths_and_retention(self):
        env = env_of(self.container("slskd"))
        self.assertEqual(env["SLSKD_SHARED_DIR"], "/media/music")
        self.assertEqual(env["SLSKD_DOWNLOADS_DIR"], "/media/downloads/soulseek")
        self.assertEqual(env["SLSKD_INCOMPLETE_DIR"], "/media/downloads/soulseek-incomplete")
        self.assertEqual(env["SLSKD_SHARE_CACHE_RETENTION"], "1440")
        mount = next(m for m in self.container("slskd")["volumeMounts"] if m["name"] == "slskd-yml")
        self.assertEqual(env["SLSKD_CONFIG"], mount["mountPath"] + "/slskd.yml")
        config = yaml.safe_load(resource("slskd/slskd.yaml", "ConfigMap", "slskd-config")["data"]["slskd.yml"])
        self.assertEqual(config["retention"], {"search": 1440, "files": {"complete": 43200, "incomplete": 43200}})

    def test_ingress_sources(self):
        rules = resource("slskd/network-policy.yaml", "NetworkPolicy", "slskd-ingress")["spec"]["ingress"]
        tcp = lambda port: [{"port": port, "protocol": "TCP"}]
        self.assertCountEqual(rules, [
            {"from": [peer({"app.kubernetes.io/name": "traefik"}, "traefik-system"), peer({"app": "soularr"}),
                      peer({"app.kubernetes.io/name": "prometheus"}, "observability")], "ports": tcp(5030)},
        ])

    def test_images_pinned(self):
        for name in ("slskd/slskd", "qmcgaw/gluetun"):
            self.assertRegex(pinned(name), r"^v?\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")


class SoularrTests(unittest.TestCase):
    def config(self):
        import configparser
        parser = configparser.RawConfigParser()
        parser.read_string(resource("soularr/soularr.yaml", "ConfigMap", "soularr-config")["data"]["config.ini"])
        return parser

    def test_config_points_at_shared_downloads(self):
        config = self.config()
        self.assertEqual(config["Lidarr"]["download_dir"], "/media/downloads/soulseek")
        self.assertEqual(config["Slskd"]["download_dir"], "/media/downloads/soulseek")
        self.assertEqual(config["Lidarr"]["api_key"], "${LIDARR_API_KEY}")
        self.assertEqual(config["Lidarr"]["host_url"], "http://lidarr:8686")
        self.assertEqual(config["Slskd"]["host_url"], "http://slskd:5030")

    def test_search_settings(self):
        config = self.config()
        self.assertEqual(config["Search Settings"]["allowed_filetypes"],
                         "mp3 320,flac,flac 24/44.1,flac 24/48,flac 24/96,flac 24/192")
        self.assertEqual(config["Search Settings"]["search_source"], "missing")
        self.assertEqual(config["Search Settings"]["failed_import_denylist"], "True")
        self.assertEqual(config["Logging"]["log_to_file"], "False")

    def test_runtime(self):
        pod = resource("soularr/soularr.yaml", "Deployment", "soularr")["spec"]["template"]["spec"]
        container, = pod["containers"]
        env = env_of(container)
        self.assertEqual(env["LIDARR_API_KEY"], {"secretKeyRef": {"name": "music-stack", "key": "lidarr-api-key"}})
        self.assertEqual(env["SCRIPT_INTERVAL"], "300")
        self.assertEqual(env["WEBUI_ENABLED"], "false")
        self.assertEqual(container["args"], ["--config-dir", "/config", "--var-dir", "/data"])
        data = next(m for m in container["volumeMounts"] if m["mountPath"] == "/data")
        volume = next(v for v in pod["volumes"] if v["name"] == data["name"])
        self.assertEqual(volume["persistentVolumeClaim"]["claimName"], "soularr-data-pithari")
        self.assertRegex(pinned("mrusse08/soularr"), r"^v\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")

    def test_lidarr_admits_soularr(self):
        rules = resource("lidarr/network-policy.yaml", "NetworkPolicy", "lidarr-ingress")["spec"]["ingress"]
        self.assertTrue(any(peer({"app": "soularr"}) in rule["from"] and {"port": 8686, "protocol": "TCP"} in rule["ports"]
                            for rule in rules))


if __name__ == "__main__":
    unittest.main()
