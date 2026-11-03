#!/usr/bin/env python3
import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]


def documents(path):
    return list(yaml.safe_load_all((ROOT / path).read_text()))


def resource(path, kind, name):
    return next(doc for doc in documents(path) if doc["kind"] == kind and doc["metadata"]["name"] == name)


class GpuAppsTests(unittest.TestCase):
    def test_gpu_scheduling(self):
        for path, name in [("2-k3s/08.servarr/jellyfin/jellyfin.yaml", "jellyfin"), ("2-k3s/32.ai/ollama.yaml", "ollama")]:
            deployment = resource(path, "Deployment", name)
            self.assertEqual(deployment["spec"]["replicas"], 1)
            self.assertEqual(deployment["spec"]["strategy"]["type"], "Recreate")
            pod = deployment["spec"]["template"]["spec"]
            self.assertEqual(pod["runtimeClassName"], "nvidia")
            self.assertEqual(pod["nodeSelector"], {"gpu": "nvidia"})
            self.assertIn({"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}, pod["tolerations"])
            container = pod["containers"][0]
            for bound in ["requests", "limits"]:
                self.assertEqual(container["resources"][bound]["nvidia.com/gpu"], 1)
            self.assertIn({"name": "NVIDIA_DRIVER_CAPABILITIES", "value": "all"}, container["env"])

    def test_local_data_is_prebound_and_retained(self):
        for path, namespace, claims in [
            ("2-k3s/08.servarr/jellyfin/storage.yaml", "servarr", {"jellyfin-config": "/srv/jellyfin/config"}),
            ("2-k3s/32.ai/storage.yaml", "ai", {"ollama-data": "/srv/ollama", "open-webui-data": "/srv/open-webui"}),
        ]:
            for name, local_path in claims.items():
                pv = resource(path, "PersistentVolume", name + "-pv")["spec"]
                pvc = resource(path, "PersistentVolumeClaim", name)["spec"]
                self.assertEqual(pv["local"]["path"], local_path)
                self.assertEqual(pv["persistentVolumeReclaimPolicy"], "Retain")
                self.assertEqual(pvc["volumeName"], name + "-pv")
                self.assertEqual(pv["claimRef"], {"namespace": namespace, "name": name})
                affinity = pv["nodeAffinity"]["required"]["nodeSelectorTerms"][0]["matchExpressions"][0]
                self.assertEqual(affinity, {"key": "kubernetes.io/hostname", "operator": "In", "values": ["k3s-worker-64"]})

    def test_jellyfin_media_and_scratch_paths(self):
        pod = resource("2-k3s/08.servarr/jellyfin/jellyfin.yaml", "Deployment", "jellyfin")["spec"]["template"]["spec"]
        volumes = {volume["name"]: volume for volume in pod["volumes"]}
        self.assertEqual(volumes["media"]["persistentVolumeClaim"]["claimName"], "servarr-media")
        mounts = {mount["mountPath"]: mount for mount in pod["containers"][0]["volumeMounts"]}
        for folder in ["animes", "movies", "tvshows"]:
            self.assertEqual(mounts["/" + folder]["subPath"], folder)
        for path in ["/cache", "/cache/transcodes", "/config/transcodes"]:
            self.assertIn("emptyDir", volumes[mounts[path]["name"]])
        self.assertEqual(mounts["/config/transcodes"]["name"], mounts["/cache/transcodes"]["name"])
        self.assertNotIn("tdarr", str(mounts))

    def test_jellyfin_routes(self):
        path = "2-k3s/08.servarr/jellyfin/jellyfin.yaml"
        service = resource(path, "Service", "jellyfin")["spec"]
        self.assertEqual(service["selector"], {"app": "jellyfin"})
        self.assertEqual(service["ports"][0]["port"], 8096)
        self.assertEqual([d["metadata"]["name"] for d in documents(path) if d["kind"] == "Service"], ["jellyfin"])
        for route in documents("2-k3s/08.servarr/jellyfin/ingressroute.yaml"):
            self.assertEqual(route["spec"]["routes"][0]["match"], "Host(`jellyfin.epaflix.com`)")
            self.assertEqual(route["spec"]["routes"][0]["services"], [{"name": "jellyfin", "port": 8096}])

    def test_open_webui_uses_external_ollama_and_local_redis(self):
        pod = resource("2-k3s/32.ai/open-webui.yaml", "Deployment", "open-webui")["spec"]["template"]["spec"]
        self.assertNotIn("runtimeClassName", pod)
        webui, redis = pod["containers"]
        env = {entry["name"]: entry for entry in webui["env"]}
        self.assertEqual(env["USE_OLLAMA_DOCKER"]["value"], "false")
        self.assertEqual(env["OLLAMA_BASE_URL"]["value"], "http://ollama.ai.svc.cluster.local:30068")
        self.assertIn("127.0.0.1:6379", env["REDIS_URL"]["value"])
        self.assertEqual(env["REDIS_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"], "open-webui-redis-env")
        self.assertEqual(webui["envFrom"], [{"secretRef": {"name": "open-webui-env"}}])
        self.assertEqual(redis["envFrom"], [{"secretRef": {"name": "open-webui-redis-env"}}])
        for container in pod["containers"]:
            self.assertNotIn("nvidia.com/gpu", container["resources"]["limits"])
            self.assertIn("readinessProbe", container)
            self.assertIn("livenessProbe", container)

    def test_open_webui_is_lan_only(self):
        routes = documents("2-k3s/32.ai/ingressroute.yaml")
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["spec"]["entryPoints"], ["internal"])

    def test_memory_limits_leave_node_headroom(self):
        total_mib = 0
        for path, name in [("2-k3s/08.servarr/jellyfin/jellyfin.yaml", "jellyfin"), ("2-k3s/32.ai/ollama.yaml", "ollama"), ("2-k3s/32.ai/open-webui.yaml", "open-webui")]:
            containers = resource(path, "Deployment", name)["spec"]["template"]["spec"]["containers"]
            for container in containers:
                memory = container["resources"]["limits"]["memory"]
                total_mib += int(memory[:-2]) * (1024 if memory.endswith("Gi") else 1)
        self.assertLessEqual(total_mib, 17 * 1024)

    def test_cliproxy_migrates_existing_config_before_seed_guard(self):
        source = (ROOT / "2-k3s/17.remote-pi/cliproxy/files/reconcile-config.psql").read_text()
        replacement = "c := replace(c, 'http://192.168.10.200:30068/v1', 'http://ollama.ai.svc.cluster.local:30068/v1');"
        self.assertTrue(replacement in source)
        self.assertLess(source.index(replacement), source.index("RAISE NOTICE 'openai-compatibility already present"))
        self.assertIn('base-url: "http://ollama.ai.svc.cluster.local:30068/v1"', source)

    def test_secret_envelopes_and_app_wiring(self):
        for name in ["open-webui-env", "open-webui-redis-env"]:
            secret = documents("2-k3s/32.ai/" + name + ".enc.yaml")[0]
            self.assertEqual(secret["kind"], "Secret")
            self.assertEqual(secret["metadata"]["namespace"], "ai")
            self.assertIn("sops", secret)
            self.assertTrue(all(value.startswith("ENC[AES256_GCM,") for value in secret["data"].values()))
        apps = documents("2-k3s/11.argocd/apps/kustomization.yaml")[0]
        self.assertIn("app-ai.yaml", apps["resources"])
        servarr = documents("2-k3s/08.servarr/kustomization.yaml")[0]
        self.assertIn("jellyfin/jellyfin.yaml", servarr["resources"])
        self.assertNotIn("jellyfin/jellyfin-truenas-redirect.yaml", servarr["resources"])


if __name__ == "__main__":
    unittest.main()
