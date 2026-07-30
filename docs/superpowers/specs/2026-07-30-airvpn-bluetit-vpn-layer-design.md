# AirVPN Bluetit VPN layer for qBittorrent (k3s + Nick's VM) — design

> Date: 2026-07-30
> Status: design approved, pending spec review
> Repo: `SpyrosPsarras/epaflix` (this repo)
> Nick's VM (`192.168.10.41`) is hand-managed today; this design brings its compose into the repo.

## Purpose

Replace the qBittorrent VPN layer — today a single pinned AirVPN WireGuard endpoint driven by
`wg-quick` inside `tenseiken/qbittorrent-wireguard` — with the official **AirVPN Suite**
(Bluetit daemon + Goldcrest client), so the configuration holds a **server whitelist/blacklist**
and lets AirVPN pick a recommended server instead of us hardcoding one IP.

## What triggered this

A speedtest on Nick's VM on 2026-07-30 found the AirVPN tunnel, not the line, was the problem.
The same fault was hitting the cluster.

`nl3.vpn.airdns.org` is a **single rotating A record** (observed moving from `213.152.161.42`
to `134.19.179.189` within one session), handing out whichever server AirVPN picks that minute.
It landed us on Cygnus / Hassaleh / Kajam — three **2 Gbit** boxes in Alblasserdam, all at
50-57% load, all dropping packets.

Measured from `k3s-worker-61`, 100 ICMP packets per target:

| Target | Loss | RTT avg |
|---|---|---|
| `1.1.1.1` (control) | 0% | 2.6 ms |
| Cygnus `213.152.161.246` — k3s endpoint | 9% | 32.6 ms |
| Kajam `213.152.161.87` — Nick's endpoint | 6.7% | 31.0 ms |
| Dedalus `109.235.50.17` | 0% | 24.6 ms |
| Piautos `134.19.178.169` | 0% | 27.1 ms |
| Dalim `134.19.179.213` | 0% | 27.2 ms |
| Menkab `62.102.148.216` (se) | 0% | 14.9 ms |

Inside the pod it was far worse — 60% loss, RTT up to 8.6 s, upload collapsed to ~0.03 MB/s
against 16.6 MB/s on the bare host. It also kept tripping the liveness probe: **59 restarts in
12 days**.

Two findings worth keeping:

- **AirVPN's own status API reported `health=ok` for all three bad servers.** Do not trust that
  flag; measure the entry-3 IPs from the node that runs the pod.
- The uplink was never the problem: 0% loss to `1.1.1.1` from both the node and Nick's VM, with
  `k3s-worker-61` pushing only ~7 Mbit at the time.

## Why Bluetit rather than a better pinned IP

Pinning a healthy server fixes today and leaves the same trap set for next month. WireGuard
cannot fail over inside one config — a peer has one `Endpoint`, and several peers cannot each
claim `AllowedIPs = 0.0.0.0/0`. AirVPN's generator confirms this: asking for three servers
returns three separate `.conf` files, and the multi-server switch (`openvpn_allservers`) is
OpenVPN-only.

Bluetit takes server *lists* and picks from them:

- `airwhiteserverlist` / `airwhitecountrylist` — the pool it may use
- `airblackserverlist` / `airblackcountrylist` — servers it must never use
- `airconnectatboot = quick` — connect to AirVPN's recommended server from the whitelist, minus
  the blacklist

## Constraints / decisions (locked with owner)

- **Credentials.** Bluetit's recommended-server selection needs the AirVPN **account username and
  password** in `bluetit.rc` in plaintext. There is no API-key or token mode. Owner accepted this
  risk; they live in a SOPS Secret. Already added to `secrets.yml` as `airVPN_user` /
  `airVPN_password` (note: that casing differs from the surrounding `airvpn_*` keys — referenced
  as written, not renamed).
- **Image build.** New capability for this repo: Dockerfile + GitHub Actions workflow publishing
  to GHCR. This repo has no Dockerfiles today.
- **Architecture.** Bluetit runs as a **native sidecar**; qBittorrent keeps its current image.
- **Degradation handling.** Auto-reconnect via SIGUSR2 plus an alert.
- **Server pool.** Whitelist countries `nl,de,se`; blacklist the three measured-bad boxes.
- Cluster is `v1.35.5+k3s1`, so native sidecars (initContainer with `restartPolicy: Always`,
  GA since 1.29) are available.

## Architecture

A single small image carries only D-Bus and the AirVPN Suite. It runs as a native sidecar that
owns the tunnel and the network lock. qBittorrent rides the pod's shared network namespace.

The VPN layer becomes reusable: Nick's VM consumes the same image with
`network_mode: "service:airvpn"`.

Rejected alternatives:

- **One fat image (qBittorrent + Bluetit).** Closest to today's shape, but we would own packaging
  qBittorrent too, Renovate could no longer track its version separately, the image is much larger
  to patch, and the work repeats whenever the VPN is wanted in front of something else.
- **A namespace-wide VPN gateway pod.** Needs routing and NAT machinery we do not have and widens
  the blast radius from qBittorrent to all of `servarr`. Nothing asked for it.

## Component 1 — the image

`ghcr.io/spyrospsarras/airvpn-bluetit`, built from `images/airvpn-bluetit/`.

- Base `debian:13-slim` (the Suite requires OpenSSL 3.x).
- Packages, verified to satisfy `ldd` for both binaries: `dbus`, `libdbus-1-3`, `libssl3`,
  `libstdc++6`, `libsystemd0`, `libcap2`, `zlib1g`, `libbrotli1`, `libzstd1`, `libxml2`,
  `iptables`, `iproute2`, `iputils-ping`, `busybox`, `procps`, `ca-certificates`, `curl`.
- `install.sh` is **bypassed** — it prompts about systemd, boot-start and creating the `airvpn`
  user/group, which would hang a non-interactive build. The Dockerfile places files directly:
  `bin/bluetit` → `/sbin/bluetit`, `bin/goldcrest` → `/usr/local/bin/goldcrest`,
  `etc/airvpn/*` → `/etc/airvpn/`, `etc/dbus-1/system.d/*.conf` → `/etc/dbus-1/system.d/`,
  plus `groupadd -f airvpn`. The systemd units and init scripts in the tarball are not used.
- AirVPN Suite pinned by a build arg (`AIRVPN_SUITE_VERSION`, initially `2.1.0`), downloaded from
  GitLab and verified against its published `.sha512`. A checksum mismatch fails the build rather
  than shipping something unverified.
- A Renovate `customManager` regex watches `AIRVPN_SUITE_VERSION` so suite upgrades arrive as PRs.
  Without it nothing would ever report 2.1.0 as stale — Renovate only sees the image tag we
  ourselves publish.

**Entrypoint.** Bluetit forks and writes `/etc/airvpn/bluetit.lock` (a PID file), so the container
needs something holding the foreground:

1. start `dbus-daemon --system`
2. start `busybox syslogd -n -O /dev/stdout`
3. render `/etc/airvpn/bluetit.rc` (below)
4. launch `/sbin/bluetit`
5. wait for the tunnel to come up
6. run the degradation probe loop, and watch the PID — if Bluetit dies, exit non-zero so
   Kubernetes restarts the container rather than leaving a live pod with a dead VPN

Bluetit requires `root`.

**The syslog step is mandatory, not cosmetic.** Bluetit has **no `logfile` directive** — the
complete directive list in the shipped `bluetit.rc` has no logging option at all. It logs to
syslog only. Without a syslog daemon forwarding to stdout, Bluetit produces *no container output
whatsoever* — it appears to fail silently even when running perfectly. That also makes the Loki
alert in Component 4 impossible. This cost real debugging time during design; do not remove it.

### Verified during design (2026-07-30)

Built and run locally with Docker 29.6.2, so these are measured, not assumed:

- The package list below builds on `debian:13-slim` and `ldd` reports **no "not found"** for either
  `bluetit` or `goldcrest` — this is AirVPN's own documented validation.
- `bluetit` links `libsystemd.so.0` and `libcap.so.2`, which are easy to miss from the README's
  dependency list.
- Bluetit starts correctly in a container with `dbus-daemon --system` plus `NET_ADMIN`: connects to
  D-Bus, initialises, retrieves the AirVPN manifest, and writes its lock file.
- It auto-detected the home country as `NO` and read DNS `192.168.10.30` / `192.168.10.1` from the
  container resolver.
- Published `.sha512` for `2.1.0` verifies against the downloaded tarball.
- Goldcrest's status flags are `--bluetit-status` and `--bluetit-stats` (**not** `--status`).

## Component 2 — configuration

The Suite ships a `bluetit.rc` template carrying `bootserver`, `rsaexponent` and `rsamodulus`,
which are **required for AirVPN support at all**. We append to that shipped file; we do not write
one from scratch.

**ConfigMap `airvpn-bluetit-config`** — everything non-secret, so server-pool changes are readable
in a PR diff:

Directives are **whitespace-separated**, not `key = value` — confirmed against the shipped file:

```
airvpntype                  wireguard
airconnectatboot            quick
airwhitecountrylist         nl,de,se
airblackserverlist          Cygnus,Hassaleh,Kajam
airport                     1637
country                     NO
forbidquickhomecountry      yes
networklock                 iptables
allowprivatenetwork         yes
allowping                   yes
airipv6                     no
tunpersist                  yes
airkey                      Default
```

**Secret `airvpn-credentials`** (SOPS `.enc.yaml`) — only `airusername` and `airpassword`,
injected as env and substituted by the entrypoint. They never enter a ConfigMap or the image.

Notes on specific directives:

- `airkey` is the only value differing between deployments: `Default` for k3s,
  `nick` for his VM. The two keep separate AirVPN device keys, as they do today
  (`Default` = `10.135.227.175`, `nick` = `10.154.38.229`).
- `tunpersist yes` is load-bearing, not cosmetic: **SIGUSR2 reconnect only works with TUN
  persistence enabled**. Whether it also needs a `/dev/net/tun` mount under
  `airvpntype wireguard` is **still unverified** — Bluetit starts without the device, but no
  connection has been attempted yet. On the verification list; if it needs the device, add the
  mount back.
- `allowping yes` is required, not optional: the network lock would otherwise drop the ICMP the
  degradation probe depends on, and the probe would read a healthy tunnel as dead.
- `airipv6 no` matches the existing `init-sysctls` container, which disables IPv6. Bluetit
  otherwise reports "IPv6 is available in this system" and may try to use it.
- `country NO` is technically optional — Bluetit auto-detects it correctly via ipleak.net — but
  pinning it avoids an outbound call on every start and removes a dependency on that service.
- `networkcheck <on|gateway|airvpn|off>` exists and overlaps with our own probe. Leave it at the
  default; our probe is what drives SIGUSR2, and two competing checks would be harder to reason
  about than one.
- `allowprivatenetwork = yes` keeps Pi-hole DNS, the cluster CIDRs and the WebUI reachable once
  the network lock is armed. It replaces the six hand-written iptables rules in the current
  `postStart` hook.
- The country whitelist is deliberately wider than `nl` so a quick-connect has somewhere to go
  when Amsterdam is busy.
- `forbidquickhomecountry = on` is AirVPN's recommended default; home country is `NO`, and we
  connect to NL/DE/SE, so it costs us nothing.

## Component 3 — the Deployment

Unchanged: `dnsPolicy: None` + `dnsConfig`, the `Recreate` strategy (still one VPN session at a
time), ports 8080 / 39998, both PVCs, and the `init-sysctls` initContainer.

Added — `airvpn` native sidecar:

- our image, `NET_ADMIN`, `runAsUser: 0`
- ConfigMap mounted as a file, Secret as env
- **no `/dev/net/tun` mount** — that is an OpenVPN need; WireGuard uses the kernel module the
  current pod already relies on
- startup/liveness probe via Goldcrest confirming a connected state

Removed from the qBittorrent container: the `postStart` iptables block, the `wireguard-config`
volume, and the wrapper env `VPN_ENABLED` / `LAN_NETWORK` / `NAME_SERVERS` / `ADDITIONAL_PORTS` /
`HEALTH_CHECK_*`. It sets `VPN_ENABLED=no` and, since it no longer touches the network, **drops
`privileged: true` and `NET_ADMIN`** — only the sidecar keeps them. This is the one real security
improvement in the change.

**Known assumption to verify, not assert.** We keep `tenseiken/qbittorrent-wireguard` as the app
container with its VPN switched off, rather than swapping to a stock qBittorrent image, because
its `/config` layout is what the existing PVC holds and a data migration should not ride along
with this change. Whether that image behaves cleanly with `VPN_ENABLED=no` — in particular whether
it still tries to install iptables rules — **must be verified on a scratch pod before the live one
is touched**. If it misbehaves, fall back to a stock qBittorrent image and accept the `/config`
migration as separate work.

## Component 4 — degradation handling

Three failure modes, deliberately separated:

1. **Bluetit dies.** Entrypoint exits non-zero → sidecar restarts. While it is down the network
   lock blocks everything except private networks, so there is no leak. This is a real killswitch
   replacing the one we currently fake with `postStart`.
2. **Tunnel drops but Bluetit lives.** Bluetit reconnects on its own.
3. **Server stays up but goes bad** — today's failure. Neither the network lock nor a restart
   notices it. A loop in the sidecar pings the tunnel gateway; after N consecutive bad checks it
   sends **SIGUSR2**, Bluetit reconnects, and quick-mode picks a fresh recommended server outside
   the blacklist. A cooldown between reconnects prevents flapping.

Starting thresholds — chosen so today's fault trips it and ordinary jitter does not, and to be
re-tuned once we have real numbers from the new server:

| Knob | Start value | Reason |
|---|---|---|
| probe | 20 pings to the tunnel gateway `10.128.0.1`, every 60 s | in-tunnel, no upstream ICMP filtering; same target the current health check uses |
| trip threshold | > 5% loss | measured bad servers were 6.7-9%; healthy ones were 0% |
| consecutive checks | 3 | ~3 minutes of sustained loss, so a transient blip is ignored |
| cooldown | 15 min | one reconnect per window at worst, so a bad pool cannot cause a reconnect storm |

**Alerting.** The sidecar logs a single distinct line on every reconnect
(`airvpn-bluetit: reconnect triggered, loss=<n>%`) and a Loki rule alerts on it. This reuses the
observability stack already in the repo rather than adding a metrics endpoint and a scrape config
to a pod that has a network lock in front of it. A silent reconnect loop therefore cannot hide.

## Component 5 — Nick's VM

Same image, `airkey = nick`. A compose file with an `airvpn` service and qBittorrent on
`network_mode: "service:airvpn"`. His stack is hand-managed and undocumented today; the compose
goes under `1-proxmox/user-vms/nick/`.

His VM also runs `cloudflared`, which is **outside** the VPN and stays that way.

Applied only after the cluster side has passed its soak.

## Verification

On a scratch pod first, then live:

- Goldcrest reports connected, to a server **not** on the blacklist
- exit IP is AirVPN
- 100-ping loss through the tunnel near zero
- download and upload measured inside the pod, compared against the bare host
- Pi-hole DNS resolves from the pod; WebUI answers from the LAN
- port 39998 reachable (AirVPN's forward is account-wide, so it follows the chosen server)
- ArgoCD `servarr` Synced/Healthy
- restart count flat over 24h
- SIGUSR2 path exercised deliberately once, confirming it reconnects to a different server
- `tunpersist = on` works without a `/dev/net/tun` mount under `airvpntype = wireguard` (see the
  open question in Component 2) — if not, the mount goes back and the spec is corrected
- `VPN_ENABLED=no` leaves the qBittorrent container's iptables untouched (see Component 3)

## Rollback

Revert the PR; ArgoCD restores the previous Deployment. The existing `qbittorrent-wireguard`
Secret stays in git until the soak passes, so rollback needs no key recovery.

## Out of scope

- Moving other `servarr` apps behind the VPN.
- Replacing the qBittorrent image / `/config` migration (only a fallback if the
  `VPN_ENABLED=no` assumption fails).
- Rotating the AirVPN account password.
- The parked `airvpn-server-swap` branch, which pinned Dedalus as an interim fix and was
  superseded by this design.
