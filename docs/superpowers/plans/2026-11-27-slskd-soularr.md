# slskd + Soularr Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lidarr's missing albums are searched on Soulseek through slskd behind its own AirVPN tunnel, and found albums import into Lidarr.

**Architecture:** One `slskd` pod with a gluetun native sidecar (AirVPN device `slskd`, forwarded port 46327) and one `soularr` pod that polls Lidarr's wanted list every 5 minutes. Both live in `2-k3s/08.servarr/` and Argo CD syncs them.

**Tech Stack:** Kubernetes manifests with Kustomize, KSOPS secrets, gluetun v3.41.3, slskd 0.26.2, Soularr v1.2.3, Prometheus Operator, Traefik with Authentik forward auth.

**Spec:** `docs/superpowers/specs/2026-11-27-slskd-soularr-design.md`

## Global Constraints

- Namespace `servarr`. Media UID/GID 568.
- Image pins go in the `images:` block of `2-k3s/08.servarr/kustomization.yaml` as `<version>@sha256:<index digest>`, recorded in `docs/servarr-image-pins.md`.
- Forwarded port `46327` TCP. AirVPN device `slskd`. Countries Netherlands, Germany, Sweden.
- Paths: library `/media/music` (read-only in slskd), downloads `/media/downloads/soulseek`, incomplete downloads `/media/downloads/soulseek-incomplete`.
- Retention: `files.complete: 43200`, `files.incomplete: 43200`, `search: 1440`.
- No secret values in output, Git history, the PR or logs. No vault item names in repo files (rule S2).
- Only one tunnel per AirVPN device. No scratch gluetun with the `slskd` keys may run when Argo syncs.

## Review Focus

1. Tunnel down: slskd must not reach Soulseek from the home IP. Test: the gluetun container is a native sidecar with a startup probe, and slskd sets `SLSKD_VPN=true` with the gluetun URL.
2. Service links: the Service `slskd` injects `SLSKD_*` variables into pods. Test: `enableServiceLinks` is false on the slskd pod.
3. Library writes: slskd must never modify `/media/music`. Test: that mount is `readOnly: true` with subPath `music`.
4. Retention scope: deletion must be limited to slskd's own directories. Test: the downloads and incomplete paths are under `/media/downloads/soulseek*`, never `/media/music`.
5. Exposure: 5030 reachable only from Traefik, Soularr and Prometheus. Test: the NetworkPolicy's `from` lists exactly those three for port 5030.

---

### Task 1: slskd workload

**Files:**
- Create: `2-k3s/08.servarr/slskd/slskd.yaml` (ConfigMap `slskd-config`, Deployment, Service, ServiceMonitor)
- Create: `2-k3s/08.servarr/slskd/network-policy.yaml`, `slskd/ingressroute.yaml`, `slskd/pdb.yaml`
- Modify: `2-k3s/08.servarr/_shared/storage/arr-configs.yaml` (PVC `slskd-config-pithari`, 5Gi, `truenas-iscsi`, like `lidarr-config-pithari`)
- Modify: `2-k3s/08.servarr/kustomization.yaml` (4 resources, image pins for `slskd/slskd` and `qmcgaw/gluetun`)
- Test: `2-k3s/08.servarr/tools/manifests.test.py` (new class `SlskdTests`)

**Interfaces:**
- Produces: Service `slskd:5030` (port name `http`). Secret `slskd` keys used by the pod: `SLSKD_SLSK_USERNAME`, `SLSKD_SLSK_PASSWORD`, `SLSKD_VPN_GLUETUN_API_KEY`, `HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE`, `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_PRESHARED_KEY`, `WIREGUARD_ADDRESSES` (Task 3 creates it).

- [ ] **Step 1: Write failing tests** in `SlskdTests`:
  - `test_vpn_sidecar_gates_slskd`: the init container `gluetun` has `restartPolicy: Always` and a `startupProbe`. Its env has `VPN_SERVICE_PROVIDER=airvpn`, `VPN_TYPE=wireguard`, `SERVER_COUNTRIES=Netherlands,Germany,Sweden`, `FIREWALL_VPN_INPUT_PORTS=46327`, `FIREWALL_INPUT_PORTS=5030,9999`, `FIREWALL_OUTBOUND_SUBNETS=10.42.0.0/16`. Container `slskd` env has `SLSKD_VPN=true`, `SLSKD_VPN_GLUETUN_URL=http://127.0.0.1:8000`, `SLSKD_SLSK_LISTEN_PORT=46327`.
  - `test_no_service_links`: `enableServiceLinks is False`, and `dnsConfig.nameservers == ["127.0.0.1"]`.
  - `test_library_read_only`: the media mounts are `{"/media/music": ("music", True), "/media/downloads": ("downloads", False)}`.
  - `test_paths_and_retention`: `SLSKD_SHARED_DIR=/media/music`, `SLSKD_DOWNLOADS_DIR=/media/downloads/soulseek`, `SLSKD_INCOMPLETE_DIR=/media/downloads/soulseek-incomplete`, and `SLSKD_CONFIG` points at the mounted `slskd.yml`. Parsed, that file's `retention` equals `{"search": 1440, "files": {"complete": 43200, "incomplete": 43200}}`.
  - `test_ingress_sources`: in `slskd-ingress`, the rule for port 5030 admits only the Traefik selector, `app: soularr` and the Prometheus selector. Port 9999 has a rule with no `from`.
  - `test_images_pinned`: the `slskd/slskd` and `qmcgaw/gluetun` tags match `^v?\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$`.
- [ ] **Step 2:** `python3 2-k3s/08.servarr/tools/manifests.test.py SlskdTests`. Expected: errors because the files are missing.
- [ ] **Step 3: Write the manifests.**
  - gluetun: copy the settings from `2-k3s/14.searxng/airvpn-proxy.yaml`, without the HTTP proxy, with `envFrom` Secret `slskd`, and with the `postStart` rule cleanup.
  - slskd: env from the spec. `SLSKD_NO_AUTH=true`, `SLSKD_METRICS=true`, `SLSKD_METRICS_NO_AUTH=true`, `SLSKD_NO_HTTPS=true`, `SLSKD_APP_DIR=/app` on the PVC. Credentials and the gluetun key come through `secretKeyRef`. Run as UID/GID 568, with memory limits.
  - ServiceMonitor `slskd`: `jobLabel: app`, port `http`, path `/metrics`.
  - IngressRoutes: copy `lidarr/ingressroute.yaml` with host `slskd.epaflix.com` and port 5030.
  - Image digests: `crane digest slskd/slskd:0.26.2`, and the gluetun pin copied from `14.searxng/kustomization.yaml`.
- [ ] **Step 4:** Run the whole test file. Expected: OK.
- [ ] **Step 5:** Commit: `feat(slskd): Soulseek client behind its own AirVPN tunnel`.

### Task 2: Soularr workload

**Files:**
- Create: `2-k3s/08.servarr/soularr/soularr.yaml` (ConfigMap `soularr-config` with `config.ini`, and the Deployment)
- Modify: `_shared/storage/arr-configs.yaml` (PVC `soularr-data-pithari`, 1Gi)
- Modify: `lidarr/network-policy.yaml` (add `app: soularr` to the 8686 rule)
- Modify: `kustomization.yaml` (resource and the `mrusse08/soularr` pin)
- Test: `manifests.test.py` (class `SoularrTests`)

**Interfaces:**
- Consumes: Service `slskd:5030`, Service `lidarr:8686`, Secret `music-stack` key `lidarr-api-key`.

- [ ] **Step 1: Write failing tests:**
  - `test_config_points_at_shared_downloads`: parse `config.ini` with `configparser.RawConfigParser`. `[Lidarr] download_dir` and `[Slskd] download_dir` both equal `/media/downloads/soulseek`. `[Lidarr] api_key == "${LIDARR_API_KEY}"`. `host_url` values are `http://lidarr:8686` and `http://slskd:5030`.
  - `test_search_settings`: `allowed_filetypes == "mp3 320,flac,flac 24/44.1,flac 24/48,flac 24/96,flac 24/192"`, `search_source == "missing"`, `failed_import_denylist == "True"`, `[Logging] log_to_file == "False"`.
  - `test_runtime`: env `LIDARR_API_KEY` comes from `music-stack/lidarr-api-key`, `SCRIPT_INTERVAL=300`, `WEBUI_ENABLED=false`. Args include `--config-dir /config --var-dir /data`. `/data` is PVC `soularr-data-pithari`.
  - `test_lidarr_admits_soularr`: the `lidarr-ingress` 8686 rule includes `{"podSelector": {"matchLabels": {"app": "soularr"}}}`.
- [ ] **Step 2:** Run `SoularrTests`. Expected: failures.
- [ ] **Step 3:** Write the manifests. Base `config.ini` on upstream `config.ini` at tag v1.2.3, with the spec's values. `[Slskd] api_key` gets a fixed non-secret placeholder (`unused-no-auth-placeholder`). Run as UID/GID 568. Mount `servarr-media` subPath `downloads` at `/media/downloads`. Pin by digest.
- [ ] **Step 4:** Run all tests. Expected: OK.
- [ ] **Step 5:** Commit: `feat(soularr): send Lidarr's missing albums to Soulseek`.

### Task 3: Secret and vault item

**Files:**
- Create: `2-k3s/08.servarr/_shared/secrets/slskd.enc.yaml`
- Modify: `_shared/secrets/ksops-generator.yaml` (add the file)

- [ ] **Step 1:** In one `vault run` command, with the age key (`sops-age-k3s-cluster`) and the AirVPN API field, all inside a `mktemp -d` dir with mode 700:
  - Generate the Soulseek username (`slskd-` plus 8 random lowercase alphanumerics), the password (`openssl rand -base64 24`) and the gluetun key (`openssl rand -hex 24`).
  - Set `HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE` to `{"auth":"apikey","apikey":"<key>"}`.
  - Fetch the WireGuard config from `https://airvpn.org/api/generator/?protocols=wireguard_3_udp_1637&servers=nl&device=slskd&key=…` and take the private key, the preshared key and the IPv4 `/32` address.
  - Write Secret `slskd` with the 7 `stringData` keys and run `sops -e -i` on it.
  - Write the username and password to a private file for Step 2.
  - Print nothing.
- [ ] **Step 2:** `vault create --input-file` a login item with that username and password, and notes pointing to `slskd.enc.yaml`. Expected output: `status` and `itemId` only.
- [ ] **Step 3:** Verify that `sops -d --output <private file>` lists exactly the 7 key names (print names only). Then delete the private dir by its explicit path.
- [ ] **Step 4:** `kubectl kustomize --enable-alpha-plugins --enable-exec 2-k3s/08.servarr` (under the age key) renders, and `kubectl apply --dry-run=server -f -` on the rendered slskd and soularr objects passes. Send that output to a private file and print only the kinds and names.
- [ ] **Step 5:** Commit: `feat(slskd): encrypted Soulseek and AirVPN secret`.

### Task 4: Alert, Authentik, DNS, docs

**Files:**
- Modify: `2-k3s/10.observability/alertmanager-config/custom-alerts.yaml` (add `SlskdDown` next to `LidarrDown`, same shape, with `deployment="slskd"` and `job="slskd"`)
- Modify: `2-k3s/10.observability/alertmanager-config/custom-alerts-tests.yaml` (a firing test and a quiet test, copied from LidarrDown's)
- Modify: `2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml` with `sops -d`/edit/`sops -e` on a private copy: a proxy provider (forward auth, single application, external host `https://slskd.epaflix.com`), an application `slskd`, a policy binding to group `authentik Admins`, and the embedded outpost's providers list. Copy the Lidarr entries' shape.
- Modify: `1-proxmox/pihole/dnsmasq.d/10-epaflix.conf` (`address=/slskd.epaflix.com/192.168.10.101`) and `1-proxmox/pihole/unbound-no-aaaa-leak.conf` (the local-zone line, like Lidarr's)
- Modify: `docs/servarr-image-pins.md`. Create `2-k3s/08.servarr/slskd/README.md` with access rules, the owner step, and the live-check record.

- [ ] **Step 1:** Run the alert tests the way CI does (`.github/workflows/ci.yml`, the promtool step). Expected: the new tests pass.
- [ ] **Step 2:** Verify that the decrypted blueprint parses as YAML and contains `slskd` 4 times (provider, application, binding, outpost). Print counts only.
- [ ] **Step 3:** Commit: `feat(slskd): alert, Authentik app and DNS`.

### Task 5: Ship and verify live

- [ ] **Step 1:** Rebase on `origin/main`. Run the full `manifests.test.py`. Push. Open the PR with `file-pr`.
- [ ] **Step 2:** Spyros approves the merge. Merge with a merge commit. Wait for Argo `servarr`, `observability` and `authentik` to be Synced at the merge SHA. They auto-sync.
- [ ] **Step 3:** Apply the DNS change on CT 1030 following `lidarr/README.md` (back up, copy, `unbound-checkconf`, reload). Expected: `slskd.epaflix.com` resolves to `192.168.10.101`.
- [ ] **Step 4:** Run the spec's verification checks 1 to 5. Record them in `slskd/README.md` in a follow-up commit on the same PR if it's still open, otherwise in a docs PR.

## Self-review

- Spec coverage: VPN (T1), shares and retention (T1), account and vault (T3), UI and Authentik (T1, T4), Soularr (T2), observability (T1, T4), DNS (T4, T5), verification (T5). No gaps.
- Proportion: the plan is about as long as the spec and has no code bodies.
