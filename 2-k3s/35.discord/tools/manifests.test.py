#!/usr/bin/env python3
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

DISCORD = Path(__file__).resolve().parents[1]
ROOT = DISCORD.parents[1]
IMAGE = "ghcr.io/spyrospsarras/openclaw-discord"
PLUGIN_PATHS = [
    "/opt/openclaw-plugins/node_modules/@openclaw/discord",
    "/opt/openclaw-plugins/node_modules/@openclaw/searxng-plugin",
]
DISCORD_KEYS = ["DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID", "DISCORD_CHANNEL_ID", "DISCORD_ROLE_ID"]
GUILD_ID = str(10**17 + 1)
CHANNEL_ID = str(10**17 + 2)


def documents(path):
    return [doc for doc in yaml.safe_load_all((DISCORD / path).read_text()) if doc]


def resource(path, kind, name):
    return next(doc for doc in documents(path) if doc["kind"] == kind and doc["metadata"]["name"] == name)


def pinned_image(app, image):
    kustomization = documents(f"{app}/kustomization.yaml")[0]
    pin = next(entry for entry in kustomization.get("images", []) if entry["name"] == image)
    return f"{pin.get('newName', image)}@{pin['digest']}"


def env_value(container, name):
    return next(entry for entry in container.get("env", []) if entry["name"] == name)


def peers(policy):
    result = set()
    for rule in policy["spec"]["egress"]:
        ports = tuple(sorted((port["protocol"], port["port"]) for port in rule["ports"]))
        for peer in rule["to"]:
            if "ipBlock" in peer:
                result.add(("ipBlock", peer["ipBlock"]["cidr"], tuple(peer["ipBlock"]["except"]), ports))
            else:
                namespace = peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
                result.add((namespace, tuple(sorted(peer["podSelector"]["matchLabels"].items())), ports))
    return result


def render(template, env):
    with tempfile.TemporaryDirectory() as work:
        config, state = Path(work, "config"), Path(work, "state")
        config.mkdir()
        (config / "openclaw.json").write_text(template)
        (config / "AGENTS.md").write_text("agent rules")
        run = subprocess.run(
            ["node", str(DISCORD / "media/render.mjs"), str(config), str(state)],
            env={"PATH": os.environ["PATH"], **env}, capture_output=True, text=True,
        )
        rendered = state / "openclaw.json"
        agents = state / "workspace/AGENTS.md"
        return run, rendered.read_text() if rendered.exists() else None, agents.read_text() if agents.exists() else None


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.deployment = resource("media/deployment.yaml", "Deployment", "openclaw")
        self.pod = self.deployment["spec"]["template"]["spec"]
        self.gateway = next(c for c in self.pod["containers"] if c["name"] == "gateway")
        self.inits = {c["name"]: c for c in self.pod["initContainers"]}
        self.config = json.loads((DISCORD / "media/openclaw.json").read_text())
        self.pod_labels = self.deployment["spec"]["template"]["metadata"]["labels"]

    def test_deployment_shape(self):
        self.assertEqual(documents("media/kustomization.yaml")[0]["namespace"], "discord-media")
        self.assertEqual(self.deployment["spec"]["replicas"], 1)
        self.assertEqual(self.deployment["spec"]["strategy"]["type"], "Recreate")
        self.assertIs(self.pod["automountServiceAccountToken"], False)
        for container in self.pod["containers"] + self.pod["initContainers"]:
            self.assertTrue(pinned_image("media", container["image"]).startswith(IMAGE + "@sha256:"))
            self.assertRegex(pinned_image("media", container["image"]), r"@sha256:[0-9a-f]{64}$")
        self.assertEqual(self.gateway["resources"]["requests"], {"cpu": "250m", "memory": "1Gi"})
        self.assertEqual(self.gateway["resources"]["limits"], {"cpu": "2", "memory": "2Gi"})
        self.assertIn("/startupz", str(self.gateway["startupProbe"]))
        self.assertIn("/startupz", str(self.gateway["readinessProbe"]))
        self.assertIn("/healthz", str(self.gateway["livenessProbe"]))
        kinds = {doc.get("kind") for path in (DISCORD / "media").glob("*.yaml") if not path.name.endswith(".enc.yaml") for doc in documents(f"media/{path.name}")}
        self.assertNotIn("Service", kinds)

    def test_secret_env_and_readonly_config(self):
        for container in [self.gateway, self.inits["render-config"]]:
            self.assertEqual(container["envFrom"], [{"secretRef": {"name": "openclaw"}}])
            self.assertEqual(env_value(container, "OPENCLAW_CONFIG_READONLY")["value"], "1")
        render_env = self.inits["render-config"]["env"]
        for key in DISCORD_KEYS:
            ref = next(entry for entry in render_env if entry["name"] == key)["valueFrom"]["secretKeyRef"]
            self.assertEqual(ref, {"name": "openclaw", "key": key})
        script = " ".join(self.inits["render-config"]["command"])
        self.assertIn("render.mjs", script)
        self.assertIn("openclaw config validate --json", script)
        self.assertIn(".valid===true", script)
        self.assertNotIn("grep", script)

    def test_reloader_watches_namespace(self):
        kustomization = documents("media/kustomization.yaml")[0]
        release = next(chart for chart in kustomization["helmCharts"] if chart["releaseName"] == "reloader-discord-media")
        self.assertEqual(release["namespace"], "discord-media")
        values = documents(f"media/{release['valuesFile']}")[0]["reloader"]
        self.assertIs(values["watchGlobally"], False)
        self.assertNotIn("custom_annotations", values)
        self.assertEqual(self.deployment["metadata"]["annotations"]["secret.reloader.stakater.com/reload"], "openclaw")
        servarr = next(chart for chart in documents("../16.reloader/kustomization.yaml")[0]["helmCharts"] if chart["releaseName"] == "reloader-servarr")
        self.assertEqual({k: release[k] for k in ("name", "repo", "version")}, {k: servarr[k] for k in ("name", "repo", "version")})

    def test_sticky_tmp(self):
        volumes = {volume["name"]: volume for volume in self.pod["volumes"]}
        tmp = next(mount for mount in self.gateway["volumeMounts"] if mount["mountPath"] == "/tmp")
        self.assertEqual(tmp["subPath"], "gateway-tmp")
        self.assertEqual(volumes[tmp["name"]]["emptyDir"], {})
        prepare = self.inits["prepare-tmp"]
        self.assertIn("chmod 1777", " ".join(prepare["command"]))
        self.assertIn(tmp["name"], [mount["name"] for mount in prepare["volumeMounts"]])

    def test_state_pvc(self):
        pvc = resource("media/pvc.yaml", "PersistentVolumeClaim", "openclaw-state")
        self.assertEqual(pvc["spec"]["storageClassName"], "truenas-iscsi")
        claims = [v["persistentVolumeClaim"]["claimName"] for v in self.pod["volumes"] if "persistentVolumeClaim" in v]
        self.assertEqual(claims, ["openclaw-state"])

    def test_network_policy(self):
        policy = resource("media/network-policy.yaml", "NetworkPolicy", "openclaw")
        self.assertEqual(policy["spec"]["podSelector"], {"matchLabels": self.pod_labels})
        self.assertEqual(policy["spec"]["policyTypes"], ["Ingress", "Egress"])
        self.assertEqual(policy["spec"]["ingress"], [])
        windmill = yaml.safe_load_all((ROOT / "2-k3s/34.windmill/network-policy.yaml").read_text())
        public = next(doc for doc in windmill if doc["metadata"]["name"] == "windmill-workers")["spec"]["egress"][-1]["to"][0]["ipBlock"]
        tcp = lambda port: (("TCP", port),)
        servarr = {"seerr": 5055, "radarr": 7878, "sonarr": 8989, "lidarr": 8686, "bazarr": 6767, "jellyfin": 8096}
        expected = {
            ("kube-system", (("k8s-app", "kube-dns"),), (("TCP", 53), ("UDP", 53))),
            ("remote-pi", (("app.kubernetes.io/name", "cliproxy"),), tcp(8317)),
            ("searxng", (("app", "searxng"),), tcp(8080)),
            ("ipBlock", "0.0.0.0/0", tuple(public["except"]), tcp(443)),
        } | {("servarr", (("app", app),), tcp(port)) for app, port in servarr.items()}
        self.assertEqual(peers(policy), expected)

    def test_reloader_network_policy(self):
        policy = resource("media/network-policy.yaml", "NetworkPolicy", "reloader")
        self.assertEqual(policy["spec"]["podSelector"], {"matchLabels": {"app.kubernetes.io/instance": "reloader-discord-media"}})
        self.assertEqual(policy["spec"]["ingress"], [])
        dns, api = policy["spec"]["egress"]
        self.assertEqual(dns["to"][0]["podSelector"], {"matchLabels": {"k8s-app": "kube-dns"}})
        self.assertEqual(api, {"to": [{"ipBlock": {"cidr": f"10.0.0.{n}/32"}} for n in (51, 52, 53)], "ports": [{"protocol": "TCP", "port": 6443}]})

    def test_lidarr_admits_media_agent(self):
        policy = yaml.safe_load((ROOT / "2-k3s/08.servarr/lidarr/network-policy.yaml").read_text())
        rule = {"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "discord-media"}}, "podSelector": {"matchLabels": self.pod_labels}}], "ports": [{"port": 8686, "protocol": "TCP"}]}
        self.assertIn(rule, policy["spec"]["ingress"])

    def test_openclaw_config(self):
        config = self.config
        self.assertEqual(config["agents"]["defaults"]["sandbox"]["mode"], "off")
        self.assertEqual(config["agents"]["defaults"]["model"]["primary"], "cliproxy/claude/claude-haiku-5-5")
        self.assertIs(config["agents"]["defaults"]["skipBootstrap"], True)
        self.assertEqual(config["models"]["providers"]["cliproxy"]["baseUrl"], "http://cliproxy.remote-pi.svc.cluster.local:8317")
        self.assertEqual(config["models"]["providers"]["cliproxy"]["api"], "anthropic-messages")
        self.assertEqual(config["models"]["providers"]["cliproxy"]["apiKey"], "${CLIPROXY_API_KEY}")
        self.assertEqual(config["plugins"]["allow"], ["discord", "searxng"])
        self.assertEqual(config["plugins"]["load"]["paths"], PLUGIN_PATHS)
        self.assertIs(config["gateway"]["controlUi"]["enabled"], False)
        self.assertEqual(config["logging"]["audit"]["messages"], "all")
        self.assertEqual(config["logging"]["consoleStyle"], "json")
        discord = config["channels"]["discord"]
        self.assertEqual(discord["token"], "${DISCORD_BOT_TOKEN}")
        self.assertEqual(discord["dmPolicy"], "disabled")
        self.assertEqual(discord["groupPolicy"], "allowlist")
        self.assertEqual(discord["actions"], {"roles": False, "moderation": False, "presence": False})
        self.assertEqual(list(discord["guilds"]), ["__DISCORD_GUILD_ID__"])
        channels = discord["guilds"]["__DISCORD_GUILD_ID__"]["channels"]
        self.assertEqual(list(channels), ["__DISCORD_CHANNEL_ID__"])
        self.assertEqual(channels["__DISCORD_CHANNEL_ID__"]["roles"], ["${DISCORD_ROLE_ID}"])
        generator = documents("media/kustomization.yaml")[0]["configMapGenerator"][0]
        self.assertEqual(sorted(generator["files"]), ["AGENTS.md", "openclaw.json", "render.mjs"])

    def test_agents_md_order(self):
        text = (DISCORD / "media/AGENTS.md").read_text()
        markers = [
            "#media",
            "http://seerr.servarr.svc.cluster.local:5055/api/v1",
            "http://radarr.servarr.svc.cluster.local:7878/api/v3",
            "http://sonarr.servarr.svc.cluster.local:8989/api/v3",
            "http://lidarr.servarr.svc.cluster.local:8686/api/v1",
            "http://bazarr.servarr.svc.cluster.local:6767/api",
            "http://jellyfin.servarr.svc.cluster.local:8096",
            "Never print, echo or repeat",
            "If more than one item matches",
            "recycling bin for 7 days",
            "Refuse",
        ]
        positions = [text.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))
        for key in ["SEERR", "RADARR", "SONARR", "LIDARR", "BAZARR", "JELLYFIN"]:
            self.assertIn(f"${key}_API_KEY", text)

    def test_no_discord_ids_in_plaintext(self):
        for path in DISCORD.rglob("*"):
            if path.is_file() and not path.name.endswith(".enc.yaml") and "__pycache__" not in path.parts:
                self.assertIsNone(re.search(r"(?<!\d)\d{17,20}(?!\d)", path.read_text()), path)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class RenderTests(unittest.TestCase):
    env = {"DISCORD_GUILD_ID": GUILD_ID, "DISCORD_CHANNEL_ID": CHANNEL_ID, "DISCORD_BOT_TOKEN": "t", "DISCORD_ROLE_ID": "r", "CLIPROXY_API_KEY": "k"}

    def test_renders_ids_and_copies_agents(self):
        run, rendered, agents = render((DISCORD / "media/openclaw.json").read_text(), self.env)
        self.assertEqual(run.returncode, 0, run.stderr)
        guild = json.loads(rendered)["channels"]["discord"]["guilds"][GUILD_ID]
        self.assertEqual(list(guild["channels"]), [CHANNEL_ID])
        self.assertIn("${DISCORD_BOT_TOKEN}", rendered)
        self.assertEqual(agents, "agent rules")

    def test_fails_on_empty_variable(self):
        for name in ["DISCORD_CHANNEL_ID", "DISCORD_ROLE_ID"]:
            run, rendered, _ = render((DISCORD / "media/openclaw.json").read_text(), {**self.env, name: ""})
            self.assertNotEqual(run.returncode, 0)
            self.assertIn(name, run.stderr)
            self.assertIsNone(rendered)

    def test_fails_on_non_numeric_id(self):
        run, rendered, _ = render((DISCORD / "media/openclaw.json").read_text(), {**self.env, "DISCORD_GUILD_ID": '1", "x": "'})
        self.assertNotEqual(run.returncode, 0)
        self.assertIsNone(rendered)

    def test_fails_on_leftover_placeholder(self):
        run, rendered, _ = render('{"a": "__DISCORD_GUILD_ID__", "b": "__OTHER__"}', self.env)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("__OTHER__", run.stderr)
        self.assertIsNone(rendered)


if __name__ == "__main__":
    unittest.main()
