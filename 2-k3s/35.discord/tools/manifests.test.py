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
            ["node", str(ROOT / "images/openclaw-discord/render.mjs"), str(config), str(state)],
            env={"PATH": os.environ["PATH"], **env}, capture_output=True, text=True,
        )
        rendered = state / "openclaw.json"
        agents = state / "workspace/AGENTS.md"
        return run, rendered.read_text() if rendered.exists() else None, agents.read_text() if agents.exists() else None


class AgentChecks:
    def setUp(self):
        self.deployment = resource(f"{self.APP}/deployment.yaml", "Deployment", "openclaw")
        self.pod = self.deployment["spec"]["template"]["spec"]
        self.gateway = next(c for c in self.pod["containers"] if c["name"] == self.CONTAINER)
        self.containers = {c["name"]: c for c in self.pod["containers"]}
        self.inits = {c["name"]: c for c in self.pod["initContainers"]}
        self.config = json.loads((DISCORD / f"{self.APP}/openclaw.json").read_text())
        self.pod_labels = self.deployment["spec"]["template"]["metadata"]["labels"]

    def test_deployment_shape(self):
        self.assertEqual(documents(f"{self.APP}/kustomization.yaml")[0]["namespace"], self.NAMESPACE)
        self.assertEqual(self.deployment["spec"]["replicas"], 1)
        self.assertEqual(self.deployment["spec"]["strategy"]["type"], "Recreate")
        self.assertIs(self.pod["automountServiceAccountToken"], False)
        for container in self.pod["containers"] + self.pod["initContainers"]:
            self.assertTrue(pinned_image(self.APP, container["image"]).startswith(IMAGE + "@sha256:"))
            self.assertRegex(pinned_image(self.APP, container["image"]), r"@sha256:[0-9a-f]{64}$")
            self.assertIs(container["securityContext"]["readOnlyRootFilesystem"], True)
        self.assertEqual(self.gateway["resources"]["requests"], {"cpu": "250m", "memory": "1Gi"})
        self.assertEqual(self.gateway["resources"]["limits"], {"cpu": "2", "memory": "2Gi"})
        self.assertIn("/startupz", str(self.gateway["startupProbe"]))
        self.assertIn("/startupz", str(self.gateway["readinessProbe"]))
        self.assertIn("/healthz", str(self.gateway["livenessProbe"]))
        kinds = {doc.get("kind") for path in (DISCORD / self.APP).glob("*.yaml") if not path.name.endswith(".enc.yaml") for doc in documents(f"{self.APP}/{path.name}")}
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
        self.assertIn("node /opt/openclaw-discord/render.mjs /config /home/node/.openclaw", script)
        self.assertIn("openclaw config validate --json", script)
        self.assertIn(".valid===true", script)
        self.assertNotIn("grep", script)

    def test_reloader_watches_namespace(self):
        kustomization = documents(f"{self.APP}/kustomization.yaml")[0]
        release = next(chart for chart in kustomization["helmCharts"] if chart["releaseName"] == f"reloader-{self.NAMESPACE}")
        self.assertEqual(release["namespace"], self.NAMESPACE)
        values = documents(f"{self.APP}/{release['valuesFile']}")[0]["reloader"]
        self.assertIs(values["watchGlobally"], False)
        self.assertNotIn("custom_annotations", values)
        self.assertEqual(self.deployment["metadata"]["annotations"]["secret.reloader.stakater.com/reload"], self.RELOAD)
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
        pvc = resource(f"{self.APP}/pvc.yaml", "PersistentVolumeClaim", "openclaw-state")
        self.assertEqual(pvc["spec"]["storageClassName"], "truenas-iscsi")
        claims = [v["persistentVolumeClaim"]["claimName"] for v in self.pod["volumes"] if "persistentVolumeClaim" in v]
        self.assertEqual(claims, ["openclaw-state"])

    def test_network_policy(self):
        policy = resource(f"{self.APP}/network-policy.yaml", "NetworkPolicy", "openclaw")
        self.assertEqual(policy["spec"]["podSelector"], {"matchLabels": self.pod_labels})
        self.assertEqual(policy["spec"]["policyTypes"], ["Ingress", "Egress"])
        self.assertEqual(policy["spec"]["ingress"], [])
        windmill = yaml.safe_load_all((ROOT / "2-k3s/34.windmill/network-policy.yaml").read_text())
        public = next(doc for doc in windmill if doc["metadata"]["name"] == "windmill-workers")["spec"]["egress"][-1]["to"][0]["ipBlock"]
        expected = {
            ("kube-system", (("k8s-app", "kube-dns"),), (("TCP", 53), ("UDP", 53))),
            ("remote-pi", (("app.kubernetes.io/name", "cliproxy"),), (("TCP", 8317),)),
            ("searxng", (("app", "searxng"),), (("TCP", 8080),)),
            ("ipBlock", "0.0.0.0/0", tuple(public["except"]), (("TCP", 443),)),
        }
        self.assertEqual(peers(policy), expected | self.EXTRA_EGRESS)

    def test_reloader_network_policy(self):
        policy = resource(f"{self.APP}/network-policy.yaml", "NetworkPolicy", "reloader")
        self.assertEqual(policy["spec"]["podSelector"], {"matchLabels": {"app.kubernetes.io/instance": f"reloader-{self.NAMESPACE}"}})
        self.assertEqual(policy["spec"]["ingress"], [])
        dns, api = policy["spec"]["egress"]
        self.assertEqual(dns["to"][0]["podSelector"], {"matchLabels": {"k8s-app": "kube-dns"}})
        self.assertEqual(api, {"to": [{"ipBlock": {"cidr": f"10.0.0.{n}/32"}} for n in (51, 52, 53)], "ports": [{"protocol": "TCP", "port": 6443}]})

    def test_openclaw_config(self):
        config = self.config
        self.assertEqual(config["agents"]["defaults"]["sandbox"]["mode"], "off")
        self.assertEqual(config["agents"]["defaults"]["model"]["primary"], self.MODEL)
        self.assertIs(config["agents"]["defaults"]["skipBootstrap"], True)
        self.assertEqual(config["models"]["providers"]["cliproxy"]["baseUrl"], "http://cliproxy.remote-pi.svc.cluster.local:8317")
        self.assertEqual(config["models"]["providers"]["cliproxy"]["api"], "anthropic-messages")
        self.assertEqual(config["models"]["providers"]["cliproxy"]["apiKey"], "${CLIPROXY_API_KEY}")
        self.assertEqual([model["id"] for model in config["models"]["providers"]["cliproxy"]["models"]], [self.MODEL.removeprefix("cliproxy/")])
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
        generator = documents(f"{self.APP}/kustomization.yaml")[0]["configMapGenerator"][0]
        self.assertEqual(sorted(generator["files"]), self.CONFIG_FILES)



class MediaTests(AgentChecks, unittest.TestCase):
    APP, NAMESPACE, CONTAINER, RELOAD = "media", "discord-media", "gateway", "openclaw"
    MODEL = "cliproxy/claude/claude-haiku-5-5"
    CONFIG_FILES = ["AGENTS.md", "openclaw.json"]
    EXTRA_EGRESS = {
        ("servarr", (("app", app),), (("TCP", port),))
        for app, port in {"seerr": 5055, "radarr": 7878, "sonarr": 8989, "lidarr": 8686, "bazarr": 6767, "jellyfin": 8096}.items()
    }

    def test_lidarr_admits_media_agent(self):
        policy = yaml.safe_load((ROOT / "2-k3s/08.servarr/lidarr/network-policy.yaml").read_text())
        rule = {"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "discord-media"}}, "podSelector": {"matchLabels": self.pod_labels}}], "ports": [{"port": 8686, "protocol": "TCP"}]}
        self.assertIn(rule, policy["spec"]["ingress"])

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


class WindmillTests(AgentChecks, unittest.TestCase):
    APP, NAMESPACE, CONTAINER, RELOAD = "windmill", "discord-windmill", "openclaw", "openclaw,github-app"
    MODEL = "cliproxy/claude/claude-opus-5-5"
    CONFIG_FILES = ["AGENTS.md", "gh", "gh-token.mjs", "git-credential-gh-token", "gitconfig", "openclaw.json"]
    EXTRA_EGRESS = set()

    def mounts(self, volume):
        return [(name, mount) for name, c in {**self.containers, **self.inits}.items() for mount in c.get("volumeMounts", []) if mount["name"] == volume]

    def test_containers(self):
        self.assertEqual(sorted(self.containers), ["gh-token", "openclaw"])
        self.assertEqual(sorted(self.inits), ["clone-repo", "prepare-tmp", "render-config"])
        self.assertIn(self.pod.get("shareProcessNamespace"), (None, False))

    def test_gh_token_sidecar(self):
        sidecar = self.containers["gh-token"]
        self.assertEqual(sidecar["image"], self.gateway["image"])
        self.assertEqual(sidecar["command"], ["node", "/scripts/gh-token.mjs"])
        self.assertNotIn("envFrom", sidecar)
        self.assertEqual(env_value(sidecar, "GITHUB_APP_ID")["valueFrom"]["secretKeyRef"], {"name": "github-app", "key": "app-id"})
        self.assertEqual(env_value(sidecar, "GITHUB_INSTALLATION_ID")["valueFrom"]["secretKeyRef"], {"name": "github-app", "key": "installation-id"})
        self.assertEqual(env_value(sidecar, "GITHUB_APP_KEY_FILE")["value"], "/var/run/secrets/github-app/private-key.pem")
        self.assertNotIn("gh-token", [name for name, _ in self.mounts("state")])

    def test_app_key_reaches_only_the_sidecar(self):
        volumes = {volume["name"]: volume for volume in self.pod["volumes"]}
        secret_volumes = [name for name, volume in volumes.items() if "secret" in volume]
        self.assertEqual([name for name, volume in volumes.items() if "projected" in volume], [])
        self.assertEqual(secret_volumes, ["github-app"])
        self.assertEqual(volumes["github-app"]["secret"]["secretName"], "github-app")
        self.assertEqual(volumes["github-app"]["secret"]["items"], [{"key": "private-key", "path": "private-key.pem"}])
        self.assertEqual([(name, mount["mountPath"]) for name, mount in self.mounts("github-app")], [("gh-token", "/var/run/secrets/github-app")])
        for name, container in {**self.containers, **self.inits}.items():
            refs = [entry["valueFrom"]["secretKeyRef"]["name"] for entry in container.get("env", []) if "secretKeyRef" in entry.get("valueFrom", {})]
            refs += [source["secretRef"]["name"] for source in container.get("envFrom", [])]
            if name != "gh-token":
                self.assertNotIn("github-app", refs, name)

    def test_token_volume(self):
        volumes = {volume["name"]: volume for volume in self.pod["volumes"]}
        self.assertEqual(volumes["gh-token"]["emptyDir"]["medium"], "Memory")
        mounts = dict(self.mounts("gh-token"))
        self.assertEqual(sorted(mounts), ["gh-token", "openclaw"])
        self.assertEqual(len(self.mounts("gh-token")), 2)
        self.assertEqual(mounts["gh-token"]["mountPath"], "/run/gh")
        self.assertFalse(mounts["gh-token"].get("readOnly", False))
        self.assertEqual(mounts["openclaw"]["mountPath"], "/run/gh")
        self.assertIs(mounts["openclaw"]["readOnly"], True)

    def test_gh_wrapper_and_credential_helper(self):
        image_path = "/home/node/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        self.assertEqual(env_value(self.gateway, "PATH")["value"], f"/scripts:{image_path}")
        self.assertEqual(env_value(self.gateway, "GIT_CONFIG_GLOBAL")["value"], "/scripts/gitconfig")
        self.assertEqual(dict(self.mounts("scripts"))["openclaw"]["mountPath"], "/scripts")
        wrapper = (DISCORD / "windmill/gh").read_text()
        self.assertIn('GH_TOKEN="$t" exec /usr/local/bin/gh "$@"', wrapper)
        self.assertIn('t=$(cat /run/gh/token) || exit 1', wrapper)
        helper = (DISCORD / "windmill/git-credential-gh-token").read_text()
        self.assertIn("username=x-access-token", helper)
        self.assertIn("t=$(cat /run/gh/token) || exit 1", helper)
        self.assertIn('echo "password=$t"', helper)
        gitconfig = (DISCORD / "windmill/gitconfig").read_text()
        self.assertIn("helper = /scripts/git-credential-gh-token", gitconfig)
        self.assertIn("name = homelab-windmill-bot[bot]", gitconfig)

    def test_clone_repo(self):
        clone = self.inits["clone-repo"]
        script = " ".join(clone["command"])
        self.assertIn("https://github.com/SpyrosPsarras/epaflix.git", script)
        self.assertIn("/home/node/work/epaflix", script)
        self.assertIn("users.noreply.github.com", script)
        self.assertNotIn("envFrom", clone)
        self.assertEqual([entry for entry in clone.get("env", []) if "valueFrom" in entry], [])
        work = [(name, mount["mountPath"]) for name, mount in self.mounts("state") if mount.get("subPath") == "work"]
        self.assertEqual(sorted(work), [("clone-repo", "/home/node/work"), ("openclaw", "/home/node/work")])

    def test_agents_md(self):
        text = (DISCORD / "windmill/AGENTS.md").read_text()
        for marker in [
            "#windmill",
            "/home/node/work/epaflix",
            "2-k3s/34.windmill/workspace/",
            "bots/windmill/<short-name>",
            "-m pytest -q 2-k3s/34.windmill/workspace/tests",
            "gh pr create",
            "gh pr merge --auto --merge",
            "git fetch && git rebase origin/main && git push --force-with-lease",
            "Never merge `main`",
            "gh pr merge --disable-auto",
            "Never put anyone's name",
            "Never print, echo",
            "/run/gh/token",
            "Never edit `.github/`",
        ]:
            self.assertIn(marker, text)


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
