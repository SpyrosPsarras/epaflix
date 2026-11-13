# Home Assistant with a local Roborock server

## Intent

Spyros wants Home Assistant as the hub for his home devices: a Roborock S7 MaxV
(`roborock.vacuum.a27`, firmware `02.59.36`), Samsung devices, ESP32 boards, Bluetooth devices and a Mill Wi-Fi socket
thermostat (European plug). The Roborock should run against
[local_roborock_server](https://github.com/Python-roborock/local_roborock_server),
a local copy of Roborock's cloud, so maps and control work without Roborock's
servers. Everything runs the way the rest of epaflix runs: k3s, Argo CD, SOPS,
Renovate, Prometheus, alerts in Discord `#notifications`.

## Goals

- G1. Home Assistant at `https://ha.epaflix.com`, reachable on the LAN and over
  Tailscale. History is stored in `postgres-cluster`.
- G2. An ESPHome dashboard at `https://esphome.epaflix.com` to build and flash
  ESP32 boards, including as Bluetooth proxies for the Bluetooth devices.
- G3. local_roborock_server at `api-rr.epaflix.com`. The vacuum is onboarded to
  it, and Home Assistant's core Roborock integration (Region `Manual`) uses it.
  Map, status and room cleaning work.
- G4. After onboarding, the Archer blocks the vacuum's internet access, and the
  vacuum still works.
- G5. A broken Home Assistant, ESPHome or Roborock endpoint, or an expiring
  certificate, raises an alert in Discord `#notifications`.

## Non-goals

- No MQTT broker. No named device needs one. The Mill socket uses the `mill`
  integration, and the Roborock server has its own broker inside the container.
- No camera streaming (go2rtc, TURN). The S7 MaxV has a camera, but upstream
  has verified camera streaming only on the Saros 10R and Qrevo MaxV, and it
  needs a camera hostname of at most 15 characters plus UDP relay ports. A
  later round can add it.
- No public exposure: no Cloudflare tunnel route, no `websecure` route.
- No Home Assistant OS, Supervisor or add-ons.
- No official Roborock app through MITM or a patched APK.
- No separate network for smart-home devices.
- The build does not configure the Samsung, Mill, ESPHome device, HACS or map
  card integrations. Spyros adds those in the Home Assistant UI after deploy.
- No scraping of Home Assistant's `/api/prometheus` in this round. It needs a
  long-lived token that exists only after the first login.

## Anti-goals

- Any secret in Git as plain text. The repo is public. The vacuum's serial
  number, DID and UID stay out of Git too.
- Leaving Roborock onboarding open after the vacuum is paired. While it is
  open, a client can claim a vacuum being onboarded.
- A Roborock setup that only works through Traefik with the pod CIDR in
  `trusted_proxies`. That lets any pod claim a vacuum during onboarding.

## Constraints and facts

Roborock server (upstream docs at `v1.2.1`, released 2026-10-08, MIT):

- Image `ghcr.io/python-roborock/local_roborock_server`. HTTPS on 555, MQTT/TLS
  on 8881. Reads `/app/config.toml`, keeps state in `/data`. `/data/runtime/`
  holds device keys and credentials as well as logs; never delete it.
- The hostname must start with `api-`. Onboarding sends `host[:port]/` without
  the `api-` prefix, capped at 32 characters. `rr.epaflix.com:555/` is 19.
- Upstream's tested-vacuums table lists the S7 MaxV (a27) on firmware
  `02.59.36`, the same as Spyros's vacuum, as working with both ZeroSSL and
  Let's Encrypt (issues #49, #118, #129).
- `tls.mode = "provided"` with `cert_file` and `key_file` uses certificates
  you supply. `listener_mode = "local_tls"` (the default) makes the server
  handle TLS itself.
- Onboarding links the vacuum's MQTT login to its earlier HTTP requests by
  source IP. NAT in between breaks this unless the NAT address is in
  `trusted_proxies`.
- Onboarding needs 1 CPU and 1 GB RAM. It can run on less later.
- Before onboarding, the admin dashboard's cloud import fetches homes, rooms,
  routines and the known vacuums from Spyros's Roborock account. The server
  needs internet egress for this.
- Known limitations: no firmware updates and no Roborock schedules while local.
  Routines work. Schedules move to Home Assistant automations.
- Onboarding runs from a second machine with Wi-Fi, Python 3.11+ and `uv`:
  the laptop.

epaflix:

- Apps live in numbered folders. The next is `2-k3s/36.home-assistant`, with
  Argo app `2-k3s/11.argocd/apps/app-home-assistant.yaml`. `33.paperless` is
  the template: `database.yaml`, `ksops-generator.yaml`, `network-policy.yaml`,
  `ingressroute.yaml`, a README, image pins as `tag@sha256` in
  `kustomization.yaml`, and `ignoreDifferences` for CNPG `/status`.
- `postgres-cluster` (CNPG, 3 instances, `local-path`, 20Gi each, not
  enforced by `local-path`, so the node disk is the limit) backs up
  nightly to `https://minio.epaflix.com`, kept 10 days. Apps connect to
  `postgres-cluster-rw.postgres-system.svc`.
- `truenas-iscsi` is block storage on the TrueNAS `apps` pool, one 100G
  virtual disk shared by every iSCSI volume in the cluster.
- Traefik entrypoint `internal` (port 8443) is exposed only on `192.168.10.102`.
  `websecure` is on `.101`, which the Cloudflare tunnel reaches.
- Tailscale advertises `192.168.10.0/24`. The Pixel 8 runs it always-on with
  Pi-hole as DNS, so `.102` names work away from home.
- The `*.epaflix.com` wildcard certificate exists only in `traefik-system`.
  Nothing copies certificates across namespaces. ClusterIssuer
  `letsencrypt-dns` (Cloudflare DNS-01) can issue into any namespace.
- kube-vip runs with per-service election. Traefik's LoadBalancer already uses
  `externalTrafficPolicy: Local`. In Git, `.116` is the next free address in
  `range-global` (`.110`-`.199`).
- Pi-hole records are `address=` lines in
  `1-proxmox/pihole/dnsmasq.d/10-epaflix.conf`, applied by hand on CT 1030
  as `1-proxmox/pihole/README.md` describes.
- Every target in `10.observability/blackbox-values.yaml` gets job `tls-probe`,
  which `TLSProbeFailed`, `TLSCertificateExpiringSoon` and
  `TLSCertificateExpiryCritical` already cover. Alertmanager posts to Discord
  `#notifications`.
- Reloader restarts pods when an annotated Secret changes. Each Reloader
  release watches only its own namespace (`watchGlobally: false`), so a new
  namespace needs its own release, as `35.discord/media` has.
- The Archer is the LAN's DHCP server. Its credentials are reachable only from
  the homelab pod.
- Home Assistant's recorder supports PostgreSQL. Home Assistant's own backup
  does not include an external database.
- Mill: Home Assistant's `mill` integration supports Mill sockets in cloud
  mode (`millheater` has a `Socket` type). Local mode is documented for Gen 3
  heaters only; whether the socket answers locally is unknown.

## Approaches

Where it runs:

- O1, picked. Everything in k3s under Argo CD. Same tooling as every other app.
  Costs: no add-on store, and a short outage when kured drains the node.
- O2, rejected. Home Assistant OS VM on Proxmox with the Roborock add-on. Adds
  add-ons and USB passthrough, which none of the named devices need, at the
  cost of a second way of running things outside Argo CD, Renovate and SOPS.
- A VM for Home Assistant with the Roborock server in k3s was dropped: the
  downsides of both.

Home Assistant's network (designer's call):

- N1, picked. Normal pod network. Devices are added by IP, so they get DHCP
  reservations on the Archer. None of the named devices needs network
  discovery to work: ESPHome boards, Bluetooth proxies, the Samsung TV and the
  Mill socket are reached by IP, and Roborock and SmartThings by login.
  One cost: Samsung TV power-on uses a Wake-on-LAN broadcast, which does not
  leave the pod network, so Home Assistant cannot turn the TV on.
- N2, rejected. `hostNetwork`. It gives automatic discovery, but the pod skips
  NetworkPolicy and port 8123 opens on a node's LAN address, outside Traefik.

History: PostgreSQL in `postgres-cluster` (picked) over SQLite on a volume. It
gets 3 replicas and the existing backups and keeps the shared 100G iSCSI pool
for small volumes. Spyros asked for it.

Roborock endpoint:

- E1, picked. Its own LoadBalancer IP with `externalTrafficPolicy: Local`. The
  server handles TLS with a cert-manager certificate. The vacuum's real IP
  reaches the server, so onboarding works with default `trusted_proxies`.
- E2, rejected. Behind Traefik with `listener_mode = "external_tls"`. Needs a
  new TCP entrypoint for MQTT and the pod CIDR in `trusted_proxies` (see
  anti-goals).

## Design

Namespace `home-assistant`. No top-level namespace in the kustomization,
because the database objects live in `postgres-system`.

### Home Assistant

- Deployment, 1 replica, strategy `Recreate`. Image
  `ghcr.io/home-assistant/home-assistant`, pinned `tag@sha256`.
- `/config` on a `truenas-iscsi` PVC.
- `configuration.yaml` contains:
  - `http.use_x_forwarded_for: true` and `trusted_proxies: 10.42.0.0/16` for Traefik
  - `recorder.db_url` from a SOPS Secret
  - the default 10-day purge
  - `recorder.db_max_retries: 100` (3 seconds apart, about 5 minutes), so a
    Postgres failover at start does not leave Home Assistant without history
- Database: `database.yaml` with `DatabaseRole` and `Database` named
  `homeassistant` in `postgres-system` (no hyphen, so no SQL quoting), role
  password Secret `homeassistant-db-role` (SOPS), as in Paperless.
- Service ClusterIP 8123. IngressRoute on entrypoint `internal`,
  `Host(ha.epaflix.com)`, middleware `security-headers`. No Authentik: Home
  Assistant's own login, so the phone app works. The `.102` Service uses the
  default `externalTrafficPolicy`, so Home Assistant sees node addresses as
  clients, not phones.
- NetworkPolicy: ingress to 8123 from Traefik pods only. Egress open, as in
  Paperless, because Home Assistant reaches devices across the LAN and vendor
  clouds.

### ESPHome dashboard

- Deployment, 1 replica, `Recreate`. Image `ghcr.io/esphome/esphome`, pinned.
- `/config` on a 5Gi `truenas-iscsi` PVC for build caches. The shared 100G
  iSCSI disk is the limit.
- The dashboard finds devices through mDNS, which does not reach a pod, so
  device online status may show unknown. Flashing over the air works by IP
  (`wifi: use_address` in each device's YAML).
- IngressRoute on `internal`, `Host(esphome.epaflix.com)`, middleware
  `security-headers`. No route on `internal` uses Authentik today, and a
  forward-auth redirect would keep the blackbox probe green while ESPHome is
  down. ESPHome's own login (`ESPHOME_USERNAME`, `ESPHOME_PASSWORD` from a SOPS
  Secret)
  protects it instead. Designer's call.
- NetworkPolicy: ingress to 6052 from Traefik pods only.

### Roborock server

- Deployment, 1 replica, `Recreate`. Image
  `ghcr.io/python-roborock/local_roborock_server`, pinned. Requests 1 CPU and
  1Gi for onboarding.
- `/app/config.toml` from a SOPS Secret:
  - `stack_fqdn = "api-rr.epaflix.com"`, ports 555 and 8881
  - `broker.mode = "embedded"`
  - `tls.mode = "provided"` pointing at the mounted certificate
  - `region = "eu"` (the account uses Europe) and
    `timezone = "Europe/Oslo"` (the robot's time zone)
  - the generated `admin` hashes and session secret
  - `protocol_login_email` and the PIN hash
- `/data` on a `truenas-iscsi` PVC.
- cert-manager `Certificate` for `api-rr.epaflix.com` from `letsencrypt-dns`,
  mounted read-only. The namespace's own Reloader restarts the pod when it
  renews.
- Service type LoadBalancer, `loadBalancerIP: 192.168.10.116` (after the live
  check), `externalTrafficPolicy: Local`, TCP 555 and 8881.
- NetworkPolicy: ingress to 555 and 8881 from anywhere (vacuum on the LAN,
  Home Assistant pod, laptop during onboarding). Egress open for the cloud
  import.

### DNS and the Archer

- Pi-hole: `ha.epaflix.com` and `esphome.epaflix.com` to `192.168.10.102`,
  `api-rr.epaflix.com` to `192.168.10.116`, each with its Unbound
  `local-zone` line, as `1-proxmox/pihole/README.md` requires.
- Archer: DHCP reservations for the vacuum, the Mill socket, each ESP32 and
  each Samsung device Home Assistant reaches by IP. After onboarding, a rule
  that blocks the vacuum's internet access.

### Monitoring

Blackbox targets in `blackbox-values.yaml`:

- `home-assistant`: `https://192.168.10.102`, hostname `ha.epaflix.com`
- `esphome`: `https://192.168.10.102`, hostname `esphome.epaflix.com`
- `roborock`: `https://192.168.10.116:555/admin`, hostname `api-rr.epaflix.com`

The existing TLS probe alerts cover them. Their descriptions show only
`instance`, the probe URL, which `home-assistant`, `esphome` and
`traefik-internal` share, so the descriptions also show the `target` label.

### Secrets

The raw values (Roborock admin password, protocol PIN, Home Assistant owner
password) go into Vaultwarden. Git holds only SOPS-encrypted hashes, URLs and
passwords.

## Rollout

1. PR with this design.
2. Homelab pod: check live LoadBalancer IPs (confirm `.116`), Postgres disk
   use, free space on the TrueNAS `apps` pool, and the Archer: that its DHCP
   hands out Pi-hole as DNS (upstream onboarding fails with outside DNS), DHCP
   reservations, and a per-device internet block.
3. PR with the manifests and README. Argo syncs. Apply the Pi-hole records.
4. Roborock: sign in to `/admin`, run the cloud import, then onboard the
   vacuum from the laptop. Confirm it appears in the inventory. Turn new
   connections off.
5. Home Assistant: create the owner account. Add the Roborock integration with
   Region `Manual`, `https://api-rr.epaflix.com:555`, the protocol email, and
   the PIN as the code.
6. Archer: block the vacuum's internet access. Confirm the map loads and a room
   clean starts with the block in place.
7. Spyros adds Samsung, Mill, ESPHome devices and any HACS extras in the UI.

## Risks

- R1. Upstream reports this exact model and firmware working with Let's
  Encrypt. A firmware update before onboarding could change that. If the
  vacuum rejects the certificate, add a ZeroSSL `ClusterIssuer` (EAB credentials in SOPS) to
  cert-manager and reissue `api-rr`, keeping `tls.mode = "provided"`. The
  Cloudflare token stays in cert-manager, out of a third-party container.
  Designer's call.
- R2. Home Assistant's own backup excludes the history database. History
  restores from the CNPG backup, config from the PVC.
- R3. The recorder writes every state change. Many sensors grow the database
  and its WAL on the Postgres node disks. Exclude chatty entities from history.
- R4. The Home Assistant base image installs `psycopg2==2.9.13`
  (`home-assistant/docker` `requirements.txt`), so the driver is present. If
  Postgres is down at start, the recorder retries `db_max_retries` times, 3
  seconds apart, then Home Assistant runs without history until restart.
- R5. Mill local mode may not support the socket. Cloud mode does.
- R6. Roborock schedules and firmware updates do not work while local.
- R7. If the Archer cannot block one device's internet access, G4 falls back
  to Spyros's decision on another way.
- R8. Unverified: that kube-vip moves `.116` to the node running the single
  Roborock pod with `externalTrafficPolicy: Local`. Traefik runs 2 replicas,
  so its setup does not prove this. Test by rescheduling the pod before
  onboarding.

## Left to the builder

- Resource requests and limits after onboarding, PVC sizes, probes.
- How `configuration.yaml` and the recorder URL reach `/config` (Secret file,
  `!secret`, init step).
- Whether new Roborock connections are turned off in the dashboard or in
  `config.toml`.
- Exact NetworkPolicy selectors, following Paperless.
