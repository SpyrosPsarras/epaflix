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
  risk; they live in a SOPS Secret. In `secrets.yml` as `airvpn_user` / `airvpn_password`,
  inside the existing `airvpn_*` block. (They were briefly added as `airVPN_user` /
  `airVPN_password`; the owner renamed them to match the block on 2026-07-30, mid-implementation.)
- **Image build.** New capability for this repo: Dockerfile + GitHub Actions workflow publishing
  to GHCR. This repo has no Dockerfiles today.
- **Architecture.** Bluetit runs as a **native sidecar**. The app container moves to
  `qbittorrentofficial/qbittorrent-nox` (the tenseiken image cannot run without a VPN).
- **Degradation handling.** SIGUSR2 reconnect plus an alert. The reconnect is a retry, not a
  cure - see Component 4 for why it cannot be called self-healing.
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
air6to4                     no
ignorednspush               yes
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
  degradation probe depends on, and the probe would read a healthy tunnel as dead. Verified —
  the connected status reports "Ping is allowed to pass the network filter".
- `ignorednspush yes` is **load-bearing and was the subtlest failure found**. Without it Bluetit
  throws `Cannot set pushed DNS to system` when it tries to rewrite `/etc/resolv.conf`, which
  kubelet owns. That exception does not merely warn — it **tears down a tunnel that has already
  connected**, leaving `goldcrest` reporting "Bluetit is not connected" with no error visible in
  the status output.
- `airipv6 no` and `air6to4 no` do **not** stop Bluetit assigning the device's IPv6 address to
  `tun0`. They are kept to express intent, but the pod must not disable IPv6 (see Component 3).
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
time), ports 8080 / 39998, and both PVCs.

**The app container changes image.** `tenseiken/qbittorrent-wireguard` has **no way to disable its
VPN** — `VPN_ENABLED` does not exist in it, and the live Deployment's `VPN_ENABLED=yes` has always
been a no-op. Its author is explicit: *"I also dropped the option to just not use a VPN. If you
don't wish to use a VPN, I highly recommend you make use of the official qBittorrent repo"*. With
a config present it hard-exits 1 on `No WireGuard config file found`.

So the app container becomes **`qbittorrentofficial/qbittorrent-nox`**, pinned by digest. The
feared `/config` migration does not exist: that image's entrypoint uses `profilePath="/config"`
and `/config/qBittorrent/config/qBittorrent.conf`, byte-identical to the live PVC — unsurprising,
since tenseiken wraps it. It honours the same `PUID`/`PGID`, the same `qbtUser`, and the same
`QBT_LEGAL_NOTICE=confirm` the Deployment already sets. It also accepts
**`QBT_TORRENTING_PORT=39998`**, which makes the BT port declarative and deletes the old manual
"set it in the WebUI" step.

Removed from the app container: the `postStart` iptables block, the `wireguard-config` volume, and
the wrapper env `VPN_ENABLED` / `LAN_NETWORK` / `NAME_SERVERS` / `ADDITIONAL_PORTS` /
`HEALTH_CHECK_*`. Since it no longer touches the network it **drops `privileged: true` and
`NET_ADMIN`** — only the sidecar keeps privileges. That is the one real security improvement here.

Added — `airvpn` native sidecar:

- our image, `runAsUser: 0`, capabilities **`NET_ADMIN` + `SYS_MODULE`**
- ConfigMap mounted as a file, Secret as env
- a **`/lib/modules` hostPath mount, read-only**
- **no `/dev/net/tun` mount** — confirmed unnecessary; WireGuard goes through the kernel module
- liveness probe keyed on the string `Connected to AirVPN server`

`SYS_MODULE` and `/lib/modules` are both mandatory, and neither was in the original design.
Bluetit insists on loading `iptable_filter`, `iptable_nat`, `iptable_mangle`, `iptable_security`,
`iptable_raw` and `wireguard` itself — **even though they are already loaded on the node** — and
without them it fails with `Error while loading kernel module ... (-3)` and never connects. This
does mean the sidecar is more privileged than first designed; the app container being unprivileged
is still a net gain over today, where qBittorrent itself runs `privileged: true`.

**`init-sysctls` must stop disabling IPv6.** It keeps `rp_filter=2` and `src_valid_mark=1`, but
`net.ipv6.conf.all.disable_ipv6=1` has to go. Bluetit unconditionally assigns the AirVPN device's
IPv6 address to `tun0`, and with IPv6 off the connect thread dies on
`Error: ipv6: IPv6 is disabled on this device` and the tunnel never establishes. `airipv6 no` and
`air6to4 no` do not prevent this.

## Component 4 — degradation handling

Three failure modes, deliberately separated:

1. **Bluetit dies.** Entrypoint exits non-zero → sidecar restarts. While it is down the network
   lock blocks everything except private networks, so there is no leak. This is a real killswitch
   replacing the one we currently fake with `postStart`.
2. **Tunnel drops but Bluetit lives.** Bluetit reconnects on its own.
3. **Server stays up but goes bad** — today's failure. Neither the network lock nor a restart
   notices it. A loop in the sidecar pings the tunnel gateway; after N consecutive bad checks it
   sends **SIGUSR2** and Bluetit reconnects. A cooldown between reconnects prevents flapping.

   **This is weaker than "self-healing", and the spec should not claim otherwise.** SIGUSR2 was
   verified to reconnect cleanly (`Reconnecting VPN server` → disconnect → 3 s wait → connected),
   but it reconnected to **the same server**, because quick-mode simply re-asks AirVPN for its
   recommendation and AirVPN still rated that server healthy — which is the original problem, since
   its status API reported `health=ok` for servers dropping 9% of packets. So the reconnect only
   escapes a bad server when AirVPN's own view changes. The real recovery loop is the alert below
   telling the owner to add the server to `airblackserverlist`. The probe's value is that it makes
   a degrading tunnel *visible and logged* rather than silent, plus a free retry.

Starting thresholds — chosen so today's fault trips it and ordinary jitter does not, and to be
re-tuned once we have real numbers from the new server:

| Knob | Start value | Reason |
|---|---|---|
| probe | 20 pings to the tunnel gateway `10.128.0.1`, every 60 s | in-tunnel, no upstream ICMP filtering; same target the current health check uses |
| trip threshold | > 5% loss | measured bad servers were 6.7-9%; healthy ones were 0%, and the verified good tunnel measured 0/100 |
| consecutive checks | 3 | ~3 minutes of sustained loss, so a transient blip is ignored |
| cooldown | 15 min | one reconnect per window at worst, so a bad pool cannot cause a reconnect storm |

The alert threshold must respect that cooldown. Three log lines are **at least** 1800 s apart, so
`count_over_time(... [30m]) > 2` can never be satisfied — a rule that reads as coverage and never
fires. Owner chose **`> 0` over `[30m]`**: any forced reconnect is worth surfacing. It will not spam,
because the rule is level-triggered — sustained degradation holds one continuous firing state
rather than alerting per reconnect.

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

### Verified on a scratch pod, 2026-07-30

Run on `k3s-worker-61` against a dedicated AirVPN device key `k3s-test`, so the live tunnel was
never disturbed — `goldcrest` confirmed `AirVPN user: <redacted> - Key: k3s-test`. The image was
side-loaded with `k3s ctr images import`, since the build workflow only publishes from `main`.

Confirmed working: credentials log in; `airkey` selects the intended device; quick-mode honoured
the blacklist (picked Aspidiske, not Cygnus/Hassaleh/Kajam); the network lock came up reporting
"Private network is allowed" and "Ping is allowed"; `tunpersist` works with **no** `/dev/net/tun`
mount; SIGUSR2 reconnects.

| Measurement | Broken tunnel (baseline) | Bluetit sidecar |
|---|---|---|
| Loss to `10.128.0.1` | 60% | **0 / 100 packets** |
| RTT avg | up to 8600 ms | **30.6 ms** |
| Download 50 MB | 1.61 MB/s | **6.69 MB/s** |
| Upload 10 MB | 0.03 MB/s | **1.70 MB/s** |

Upload is 56x better but still short of the 16.6 MB/s measured on the bare host, so the tunnel
costs real throughput — it is simply no longer the bottleneck.

One non-fatal oddity seen once: `AirVPNManifest->loadManifest() failed: AES decryption error`
during a manifest refresh. It fell back to the local copy and the tunnel stayed up. Worth watching
rather than acting on.

## Rollback

Revert the PR; ArgoCD restores the previous Deployment. The existing `qbittorrent-wireguard`
Secret stays in git until the soak passes, so rollback needs no key recovery.

## Out of scope

- Moving other `servarr` apps behind the VPN.
- Any `/config` data migration. The app-image swap turned out not to need one (Component 3).
- Rotating the AirVPN account password.
- The parked `airvpn-server-swap` branch, which pinned Dedalus as an interim fix and was
  superseded by this design.
