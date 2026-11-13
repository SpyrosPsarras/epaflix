# Home Assistant and local Roborock server implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Home Assistant, an ESPHome dashboard and local_roborock_server in k3s under Argo CD, and move the Roborock S7 MaxV off Roborock's cloud.

**Architecture:** One folder `2-k3s/36.home-assistant`, namespace `home-assistant`, laid out like `33.paperless`. Home Assistant and ESPHome sit behind Traefik's `internal` entrypoint (`192.168.10.102`). The Roborock server has its own LoadBalancer IP with `externalTrafficPolicy: Local` and handles TLS with a cert-manager certificate. Home Assistant history goes to `postgres-cluster`.

**Tech Stack:** Kustomize, KSOPS/SOPS, Argo CD, Traefik IngressRoute, cert-manager, CloudNativePG, kube-vip, Reloader, Prometheus blackbox exporter, Python `unittest` for manifest tests.

**Spec:** `docs/superpowers/specs/2026-11-13-home-assistant-design.md`

## Where this runs

Run every task from a **homelab** T3 thread (`t3code-homelab`), not the manager pod. Tracked-file changes follow the homelab pod's own `~/.pi/agent/AGENTS.md` and review-gate: a subagent writes, a fresh subagent reviews, and both models are picked from `orchestrator_capabilities` (no fixed model). One PR per task group below. Rebase onto `origin/main` before each commit. No co-author lines.

## Global constraints

- Hostnames: `ha.epaflix.com`, `esphome.epaflix.com` on `192.168.10.102`; `api-rr.epaflix.com` on `192.168.10.116` (after Task 2 confirms it is free).
- Roborock image `ghcr.io/python-roborock/local_roborock_server`, release `v1.2.1` or newer (image tag `1.2.1`, no `v`), ports 555 (HTTPS) and 8881 (MQTT/TLS).
- Reloader only watches its own namespace (`watchGlobally: false`), so `36.home-assistant` runs its own Reloader release, as `35.discord/media` does.
- Vacuum: `roborock.vacuum.a27`, firmware `02.59.36`. Do not update its firmware before onboarding.
- Roborock config: `region = "eu"`, `timezone = "Europe/Oslo"`, `tls.mode = "provided"`, `broker.mode = "embedded"`.
- Every image pinned as `tag@sha256:<digest>` in `kustomization.yaml` `images:`.
- No route on `websecure` or `web`. No `hostNetwork`. No Authentik middleware.
- No secret, serial number, DID or UID in Git as plain text. Raw passwords and the PIN go to Vaultwarden (skill `vaultwarden-secrets`); Git holds only SOPS `*.enc.yaml`. Never name vault items in tracked files; describe them instead.
- Postgres role and database name: `homeassistant` (no hyphen, so no SQL quoting).
- Alerts reach Discord `#notifications` through the existing Alertmanager route. Do not add ntfy anywhere.

## Review focus

1. Onboarding with a NATed source IP: the vacuum's HTTP and MQTT logins must arrive from its own IP. Pinned by the Service test in Task 5 and the reschedule check in Task 7.
2. Two pods on one RWO iSCSI volume during a rollout deadlocks the new pod. Every Deployment uses `strategy: Recreate`; asserted in Tasks 3, 4 and 5.
3. A renewed certificate the Roborock server never loads: it reads the cert at start. The namespace's own Reloader is asserted in Task 3, the annotation in Task 5.
4. Restarting Home Assistant wiping UI-made automations, scripts or scenes: the init step may only create those files if missing. Asserted in Task 3.
5. A route that leaks to the public Traefik IP: every IngressRoute must use only `internal`. Asserted in Task 3 for all routes in the folder.

---

### Task 1: Design and plan in the repo

**Files:**
- Create: `docs/superpowers/specs/2026-11-13-home-assistant-design.md` (copy of the approved design)
- Create: `docs/superpowers/plans/2026-11-13-home-assistant.md` (this plan)

- [ ] **Step 1:** Copy both files from the manager pod's handoff (the thread that starts this work carries their content), with the corrections Spyros approved after checking the plan against the repo, the cluster and upstream sources.
- [ ] **Step 2:** `git diff --stat origin/main` shows exactly those two files.
- [ ] **Step 3:** Commit `docs: Home Assistant and local Roborock server design and plan`, open the PR, review-gate, merge.

### Task 2: Pre-flight checks (read-only, no PR)

Report each result to Spyros before Task 3. Stop on any failure listed here.

- [ ] **Step 1: Free IP.** `kubectl get svc -A -o jsonpath='{range .items[*]}{.status.loadBalancer.ingress[0].ip}{"\n"}{end}' | sort -u`. Expected: `192.168.10.116` absent. If taken, pick the next free address in `.110`-`.199` and use it everywhere this plan says `.116`.
- [ ] **Step 2: Postgres space.** `kubectl -n postgres-system exec <instance> -c postgres -- df -h /var/lib/postgresql/data` for each instance (the `cnpg` kubectl plugin is not installed). `local-path` does not enforce the 20Gi PVC size, so the node disk is the limit. Expected: at least 8Gi free per instance. Less: report, Spyros decides first.
- [ ] **Step 3: Archer.** Using the homelab router access, read only:
  1. DHCP DNS server handed to clients. Expected: `192.168.10.30` (Pi-hole). Anything else blocks onboarding; report.
  2. Whether DHCP address reservation exists. Expected: yes.
  3. Whether one device's internet access can be blocked (Access Control, Parental Controls or similar). Report the feature name; if none exists, report it (spec R7).
- [ ] **Step 4: Pixel over Tailscale.** `dig +short @192.168.10.30 argocd.epaflix.com` returns `192.168.10.102` (proves `.102` names resolve for Tailscale clients, which use Pi-hole). This pod has no `dig`; run it on a LAN host over SSH.
- [ ] **Step 5: iSCSI pool.** Existing `truenas-iscsi` claims request about 66.5Gi of the 100G `apps` disk; this plan adds 12Gi. Read the `apps` pool's free space on TrueNAS. Less than 20G free: report, Spyros decides first.

### Task 3: Folder, database, Home Assistant and the manifest test

**Files:**
- Create: `2-k3s/36.home-assistant/kustomization.yaml`, `namespace.yaml`, `database.yaml`, `home-assistant.yaml`, `ingressroute.yaml`, `network-policy.yaml`, `ksops-generator.yaml`, `secrets.enc.yaml`, `reloader-values.yaml`, `README.md`
- Create: `2-k3s/36.home-assistant/tools/manifests.test.py`
- Create: `2-k3s/11.argocd/apps/app-home-assistant.yaml`
- Modify: `2-k3s/11.argocd/apps/kustomization.yaml` (add the app)
- Modify: `.github/workflows/ci.yml` (add `python3 2-k3s/36.home-assistant/tools/manifests.test.py -v` next to `2-k3s/35.discord/tools/manifests.test.py -v`, around line 81)

**Interfaces:**
- Produces: namespace `home-assistant`; ksops generator `home-assistant-secrets` reading `secrets.enc.yaml` (later tasks add Secrets to this file); test helpers `documents(path)`, `resource(path, kind, name)` in `manifests.test.py` (same shape as `35.discord/tools/manifests.test.py`); IngressRoute file `ingressroute.yaml`; NetworkPolicy file `network-policy.yaml`.

- [ ] **Step 1: Write the failing tests** in `tools/manifests.test.py` (`unittest`, PyYAML):
  - `test_argo_app`: `app-home-assistant.yaml` has `spec.source.path == "2-k3s/36.home-assistant"`, `destination.namespace == "home-assistant"`, `syncPolicy.automated.selfHeal` and `prune` true, and `ignoreDifferences` for CNPG `Database` and `DatabaseRole` `/status` (copy of `app-paperless.yaml`).
  - `test_database`: `database.yaml` has `DatabaseRole` `homeassistant` and `Database` `homeassistant` (owner `homeassistant`), both in `postgres-system`, cluster `postgres-cluster`, `passwordSecret.name == "homeassistant-db-role"`, reclaim policies `retain`.
  - `test_images_pinned`: every `images:` entry in `kustomization.yaml` has `newTag` matching `^[^@]+@sha256:[0-9a-f]{64}$`.
  - `test_routes_internal_only`: every `IngressRoute` in `ingressroute.yaml` has `entryPoints == ["internal"]` and no middleware named `authentik-forwardauth`.
  - `test_home_assistant_route`: route `home-assistant` matches ``Host(`ha.epaflix.com`)`` and targets Service `home-assistant` port 8123.
  - `test_no_host_network_and_recreate`: every `Deployment` in the folder has `hostNetwork` unset or false and `strategy.type == "Recreate"`.
  - `test_ha_configuration`: parse `configuration.yaml` from ConfigMap `home-assistant-configuration` with a YAML loader that accepts `!secret` and `!include` tags. Assert `http.use_x_forwarded_for is True`, `http.trusted_proxies == ["10.42.0.0/16"]`, `recorder.db_url` is the `!secret recorder_db_url` tag, `recorder.db_max_retries == 100`, and `default_config` present.
  - `test_reloader`: `kustomization.yaml` `helmCharts` has chart `reloader` with `releaseName: reloader-home-assistant`, `namespace: home-assistant`, and the same `repo` and `version` as `35.discord/media/kustomization.yaml`; `network-policy.yaml` has a NetworkPolicy for pods labelled `app.kubernetes.io/instance: reloader-home-assistant` (copy of `35.discord/media`'s `reloader` policy).
  - `test_ha_init_keeps_ui_files`: the init container command copies only `configuration.yaml`, and for `automations.yaml`, `scripts.yaml`, `scenes.yaml` it only creates them when missing (asserts the command contains `[ -e /config/automations.yaml ] ||` style guards and no `cp` targeting those names).
  - `test_ha_network_policy`: NetworkPolicy for `app: home-assistant` allows ingress only from Traefik pods in `traefik-system` on TCP 8123 (same selector shape as `33.paperless/network-policy.yaml`).
- [ ] **Step 2:** `python3 2-k3s/36.home-assistant/tools/manifests.test.py -v`. Expected: FAIL (files missing).
- [ ] **Step 3: Implement.**
  - `kustomization.yaml`: no top-level `namespace:` (database objects live in `postgres-system`), resources as listed, `generators: [ksops-generator.yaml]`, image `ghcr.io/home-assistant/home-assistant` pinned to the latest stable release, and `helmCharts` with the Reloader release `reloader-home-assistant` in namespace `home-assistant` and a `reloader-values.yaml`, both copied from `35.discord/media`.
  - The Home Assistant recorder retries a failed database connection `db_max_retries` times, 3 seconds apart (default 10), then runs without history until restart. Set `db_max_retries: 100` (about 5 minutes) so a Postgres failover or node drain does not drop history.
  - `home-assistant.yaml`: PVC `home-assistant-config` 5Gi `truenas-iscsi` RWO; ConfigMap `home-assistant-configuration` with `configuration.yaml` containing `default_config:`, the `http` block above, `recorder: {db_url: !secret recorder_db_url, db_max_retries: 100}`, and `automation: !include automations.yaml`, `script: !include scripts.yaml`, `scene: !include scenes.yaml`; Deployment `home-assistant` (label `app: home-assistant`, 1 replica, `Recreate`), init container (same image) that copies `configuration.yaml` into `/config` and creates the three include files only if missing; Secret `home-assistant-secrets` mounted with `subPath: secrets.yaml` at `/config/secrets.yaml`; annotation `secret.reloader.stakater.com/reload: home-assistant-secrets` plus `configmap.reloader.stakater.com/reload: home-assistant-configuration`; readiness `httpGet / :8123`; Service `home-assistant` ClusterIP 8123.
  - `secrets.enc.yaml` (SOPS): Secret `homeassistant-db-role` in `postgres-system`, `type: kubernetes.io/basic-auth`, `username: homeassistant`, random 32+ char password; Secret `home-assistant-secrets` in `home-assistant` with key `secrets.yaml` holding `recorder_db_url: postgresql://homeassistant:<password>@postgres-cluster-rw.postgres-system.svc:5432/homeassistant`. Store the password in Vaultwarden.
  - `ingressroute.yaml`: IngressRoute `home-assistant` on `internal`, middleware `security-headers` (namespace `traefik-system`), `tls: {}`.
  - `network-policy.yaml`: the policy above, plus the Reloader policy.
  - `README.md`: what runs, hostnames, where secrets live (described, without vault item names), recovery (history from CNPG backup, config from PVC), spec link. Known limit: Samsung TV power-on uses a Wake-on-LAN broadcast, which does not leave the pod network, so Home Assistant cannot turn the TV on.
- [ ] **Step 4:** Run the test. Expected: PASS. Also `python3 -c "import yaml,sys;[list(yaml.safe_load_all(open(f))) for f in sys.argv[1:]]" 2-k3s/36.home-assistant/*.yaml` and the repo pre-commit hooks pass (SOPS check, plaintext guard).
- [ ] **Step 5:** Commit `feat(home-assistant): Home Assistant with Postgres history on the internal entrypoint`.

### Task 4: ESPHome dashboard

**Files:**
- Modify: `2-k3s/36.home-assistant/kustomization.yaml`, `ingressroute.yaml`, `network-policy.yaml`, `secrets.enc.yaml`, `README.md`, `tools/manifests.test.py`
- Create: `2-k3s/36.home-assistant/esphome.yaml`

**Interfaces:**
- Consumes: Task 3 helpers and files.

- [ ] **Step 1: Write the failing tests:**
  - `test_esphome_route`: route `esphome` matches ``Host(`esphome.epaflix.com`)``, Service `esphome` port 6052 (the Task 3 internal-only test already covers it).
  - `test_esphome_deployment`: container env has `ESPHOME_USERNAME` and `ESPHOME_PASSWORD` from `secretKeyRef` Secret `esphome-auth` (the bare `USERNAME`/`PASSWORD` pair is deprecated in `esphome-device-builder`, `helpers/credentials.py`); no `PASSWORD` env; volume from PVC `esphome-config` at `/config`; readiness `httpGet /version :6052` (the image's own healthcheck).
  - `test_esphome_network_policy`: ingress only from Traefik pods on TCP 6052.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3: Implement.** Known limit for the README: the dashboard finds devices through mDNS, which does not cross into a pod, so device online status may show unknown. Flashing over the air works by IP; each device YAML sets `wifi: use_address: <reserved IP>`. PVC `esphome-config` 5Gi `truenas-iscsi` (build caches; the shared 100G iSCSI disk is the limit); Deployment `esphome` (label `app: esphome`, `Recreate`), image `ghcr.io/esphome/esphome` pinned; Service `esphome` 6052; Secret `esphome-auth` in `secrets.enc.yaml` (password in Vaultwarden); route and policy.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(home-assistant): ESPHome dashboard with its own login`.

### Task 5: Roborock server

**Files:**
- Create: `2-k3s/36.home-assistant/roborock.yaml`
- Modify: `kustomization.yaml`, `network-policy.yaml`, `secrets.enc.yaml`, `README.md`, `tools/manifests.test.py`

**Interfaces:**
- Produces: Service `roborock` (LoadBalancer `192.168.10.116`), Certificate Secret `api-rr-tls`.

- [ ] **Step 1: Write the failing tests:**
  - `test_roborock_service`: Service `roborock` has `type: LoadBalancer`, `loadBalancerIP: 192.168.10.116`, `externalTrafficPolicy: Local`, TCP ports 555 and 8881 only.
  - `test_roborock_certificate`: Certificate `api-rr` has `dnsNames == ["api-rr.epaflix.com"]`, `issuerRef` ClusterIssuer `letsencrypt-dns`, `secretName: api-rr-tls`.
  - `test_roborock_deployment`: `Recreate`; mounts Secret `roborock-config` key `config.toml` read-only at `/app/config.toml` (`subPath`), Secret `api-rr-tls` read-only at `/run/certs`, PVC `roborock-data` at `/data`; annotation `secret.reloader.stakater.com/reload` lists `api-rr-tls` and `roborock-config`; requests `cpu: "1"`, `memory: 1Gi`; readiness `httpGet` `/admin` port 555 scheme `HTTPS`.
  - `test_roborock_network_policy`: ingress to TCP 555 and 8881 from any source; no other ports.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - PVC `roborock-data` 2Gi `truenas-iscsi`; Certificate `api-rr`; Deployment `roborock` (label `app: roborock`) with image `ghcr.io/python-roborock/local_roborock_server` pinned; Service `roborock`. Name the Service `roborock`, not `roborock-server`, so Kubernetes' injected `ROBOROCK_SERVER_*` variables never shadow upstream's names.
  - Secret `roborock-config` in `secrets.enc.yaml`, key `config.toml`:

```toml
[network]
stack_fqdn = "api-rr.epaflix.com"
bind_host = "0.0.0.0"
https_port = 555
mqtt_tls_port = 8881
region = "eu"
timezone = "Europe/Oslo"

[broker]
mode = "embedded"
host = "127.0.0.1"
port = 18830
mosquitto_binary = "mosquitto"
enable_topic_bridge = true

[storage]
data_dir = "/data"

[tls]
mode = "provided"
cert_file = "/run/certs/tls.crt"
key_file = "/run/certs/tls.key"

[admin]
password_hash = "<hash_password(admin password)>"
session_secret = "<random, at least 24 chars>"
protocol_auth_enabled = true
new_connections_enabled = true
protocol_login_email = "<Spyros's email>"
protocol_login_pin_hash = "<hash_password(6-digit PIN)>"
```

  - Generate hashes with upstream's own function, reading the secret from stdin so it stays out of shell history: `PYTHONPATH=src python3 -c 'import sys; from roborock_local_server.security import hash_password; print(hash_password(sys.stdin.readline().strip()))'` inside a clone of the pinned upstream release. `security.py` uses only the standard library, so no `uv` or install step is needed. Store the admin password, PIN and session secret in Vaultwarden.
  - Upstream's dashboard toggles for new connections and protocol auth rewrite `config.toml`. The file is read-only here, so change `new_connections_enabled` through Git only (Task 8 flips it).
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(home-assistant): local Roborock server on its own LoadBalancer IP`.

### Task 6: DNS records and probes

**Files:**
- Modify: `1-proxmox/pihole/dnsmasq.d/10-epaflix.conf` (add `address=/ha.epaflix.com/192.168.10.102`, `address=/esphome.epaflix.com/192.168.10.102`, `address=/api-rr.epaflix.com/192.168.10.116`)
- Modify: `1-proxmox/pihole/unbound-no-aaaa-leak.conf` (matching `local-zone` lines, same form as existing ones)
- Modify: `2-k3s/10.observability/blackbox-values.yaml` (targets below)
- Modify: `2-k3s/10.observability/alertmanager-config/tls-alerts.yaml` (show `{{ $labels.target }}` in the three descriptions; `home-assistant`, `esphome` and `traefik-internal` share `instance` `https://192.168.10.102`)
- Modify: `2-k3s/36.home-assistant/tools/manifests.test.py`

- [ ] **Step 1: Write the failing test** `test_probes_and_dns`: `blackbox-values.yaml` targets include `{name: home-assistant, url: https://192.168.10.102, hostname: ha.epaflix.com}`, `{name: esphome, url: https://192.168.10.102, hostname: esphome.epaflix.com}`, `{name: roborock, url: https://192.168.10.116:555/admin, hostname: api-rr.epaflix.com}`; `10-epaflix.conf` contains the three `address=` lines; each description in `tls-alerts.yaml` contains `{{ $labels.target }}`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3: Implement** the lines above. The default `tls-probe` job gives `TLSProbeFailed` and the certificate-expiry alerts; add no new alert rule. The rule tests in `tls-alerts-tests.yaml` do not check descriptions and must still pass.
- [ ] **Step 4:** Run the test. Expected: PASS.
- [ ] **Step 5:** Commit `feat(observability): probe Home Assistant, ESPHome and the Roborock server`. Open one PR for Tasks 3 to 6, review-gate, merge.

### Task 7: Deploy and verify

- [ ] **Step 1:** After merge, Argo syncs `app-of-apps`, then `home-assistant` (dry run first, as `2-k3s/13.t3code-homelab/files/homelab-ssh.md` describes). Expected: all resources `Synced`, `Healthy`; Certificate `api-rr` `Ready=True`.
- [ ] **Step 2:** Apply the Pi-hole and Unbound lines on CT 1030 exactly as `1-proxmox/pihole/README.md` says. Expected, from a LAN host over SSH: `dig +short @192.168.10.30 api-rr.epaflix.com` returns `192.168.10.116`; same for `ha` and `esphome` with `.102`.
- [ ] **Step 3:** `curl -sS -o /dev/null -w '%{http_code}' https://ha.epaflix.com/` returns 200; `https://esphome.epaflix.com/` returns 200 or 302 to its login; `curl -sS https://api-rr.epaflix.com:555/admin` returns HTML with a valid certificate (no `-k`).
- [ ] **Step 4: Recorder on Postgres.** `kubectl -n home-assistant logs deploy/home-assistant | grep -i recorder` shows no connection error; in Postgres, `\dt` in database `homeassistant` lists `states` and `events`.
- [ ] **Step 5: IP follows the pod (spec R8).** Note the node of pod `roborock`, cordon it, delete the pod, wait for the new pod on another node, then `curl https://api-rr.epaflix.com:555/admin` again from a LAN host and check that the client address recorded for that request (`/data/runtime/https_server.log` or `decompiled_http.jsonl`) is that host's LAN IP, not a `10.42.x.x` or node address. Uncordon. If the IP does not follow or the source is NATed: stop, report; onboarding must not start.
- [ ] **Step 6:** Blackbox: `probe_success{job="tls-probe"}` is 1 for the three new targets in Prometheus.

### Task 8: Onboard the vacuum (Spyros with the laptop, agent guides)

- [ ] **Step 1:** Spyros signs in at `https://api-rr.epaflix.com:555/admin`, runs the cloud import (Roborock account email, code), and confirms the S7 MaxV is in the inventory.
- [ ] **Step 2:** On the laptop (Wi-Fi, Python 3.11+, `uv`, clone of the same upstream release), follow upstream `docs/onboarding.md` against `api-rr.epaflix.com`. The vacuum joins a 2.4 GHz network. Time zone `Europe/Oslo`, POSIX string `CET-1CEST,M3.5.0,M10.5.0/3`; the GUI (`start_onboarding_gui.py`) derives it and the country domain from the time zone. Expected: the vacuum shows as connected in the dashboard.
- [ ] **Step 3:** PR: set `new_connections_enabled = false` in `roborock-config`, review-gate, merge, sync. Expected: dashboard shows new connections off; vacuum stays connected after the pod restart.

### Task 9: Home Assistant, Roborock integration and the Archer

- [ ] **Step 1:** Spyros creates the Home Assistant owner account at `https://ha.epaflix.com` (password into Vaultwarden).
- [ ] **Step 2:** Add integration Roborock, Region `Manual`, URL `https://api-rr.epaflix.com:555`, the protocol email, the PIN as the code. Expected: vacuum entities appear, map image entity updates.
- [ ] **Step 3:** Archer, with Spyros's go-ahead: DHCP reservation for the vacuum's MAC at its current address, then the internet block for that device.
- [ ] **Step 4:** With the block on: map loads in Home Assistant and a single-room clean starts from Home Assistant. Expected: both work. If not, lift the block and report.
- [ ] **Step 5:** Spyros adds Samsung, Mill, ESPHome devices and any HACS extras himself. Reservations for those devices are his call per device.
