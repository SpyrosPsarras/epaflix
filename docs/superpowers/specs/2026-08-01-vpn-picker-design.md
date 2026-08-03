# vpn-picker: pick the best AirVPN server at the current time - design

> Date: 2026-08-01
> Status: spec for review, nothing built
> Repo: `SpyrosPsarras/epaflix` (this repo)
> Map: #498. Consolidates the closed children #499, #500, #501, #502, #503, #504, #505, #509, #510.
> Builds on `docs/superpowers/specs/2026-07-30-airvpn-bluetit-vpn-layer-design.md`.
> Consumers: the `qbittorrent` pod in `servarr`, and Nick's VM (`192.168.10.41`).

## Purpose

Replace today's boot-only `airconnectatboot quick` pick with a mechanism that picks the best
AirVPN server **at the current time**, for both qBittorrent instances.

Goal in one line: least dropped data at maximum speed.

This spec decides the design. Building, deploying and soaking are a separate gated effort.

## What triggered this

**AirVPN's own recommendation is actively bad for us, measured twice.**

On 2026-07-31 `countries[].server_best` for `nl` was `Anser`. AirVPN reported `health: ok`.
We measured 22% ICMP loss on our own line, and 20.67% degraded TCP handshakes on the same entry
IPs. `Anser` was also goldcrest's own #1 of 251 by score. The REST API's recommendation and
Bluetit's own ranking agree with each other, and both are wrong for us.

| Server | `bw_max` | Load | Spare | AirVPN `health` | Our measured ICMP loss |
|---|---|---|---|---|---|
| `Anser` | 2000 | 39% | ~1.2 Gbit | `ok`, `server_best` for `nl` | 21-25% |
| `Dedalus` | 20000 | 15% | ~17 Gbit | `ok` | 0% |
| `Aspidiske` (then live) | - | - | - | `ok` | 0.33% |

**`quick` picks small boxes, measured twice.** On both scratch boots `quick` landed on a 2 Gbit
box at 78% load, while eleven 20 Gbit boxes in the same `nl,de,se` pool sat at 20-33% load.

**Selection happens once, at pod boot.** No re-evaluation, no measurement, no failover for the
whole life of the pod. Nick's VM has no failover at all today.

**Root cause of the blind spot.** AirVPN's `health` flag comes from their own global probe mesh,
at roughly a 20% loss threshold, and that mesh never traverses our specific path. It structurally
cannot see loss that is local to us. Bluetit cannot help either: it has **no pinger**. Its `Score`
is `scoreBase/speedFactor + load/loadFactor + (users*100/users_max)/userFactor`
(`src/airvpnserver.cpp:425-441`, AirVPN-Suite 2.1.0), a pure function of AirVPN's published
manifest, identical on every machine on earth at the same manifest revision. It is dominated by
absolute user count, so it ranks small boxes above big ones - `Anser` scored 180 against `Felix`
at 186, while `Felix` had 14 Gbit spare to `Anser`'s 1.2.

So the quality signal has to be our own measurement. ICMP loss from a LAN node is a proven proxy
for it: against an independent transport (150 TCP handshakes to port 443 on the same entry IPs,
handshake over 200 ms counted as a dropped SYN or SYN/ACK), `n=9`, Pearson `r=0.994`,
Spearman `rho=0.900`. Ruled out as explanations: ICMP policing (six neighbours in `Anser`'s own
`/24` answered 400 packets each at 0.00-3.25% loss while `Anser` dropped a fifth, and `Anser`'s
loss went *down* at 5x the ping rate), our line (both controls at exactly 0.00% loss the whole
window), and load (`Sheliak` at 85% load and `Luhman` at 81% load lost zero packets).

## Architecture

Two pieces, and the split is forced, not a preference.

- **Scorer** - one small always-on Deployment in k3s, **outside** any VPN network namespace. It
  reads the AirVPN REST API, probes a shortlist by ICMP, and publishes a ranked list.
- **Agent** - a sidecar container inside the `qbittorrent` pod, next to `airvpn`, sharing
  `/run/dbus`. It reads the published ranking, watches the current tunnel, and drives Bluetit
  over D-Bus when it has to switch.

**Why probing cannot happen in the pod.** The sidecar inherits the pod's route table and the
network lock. Measured live:

```
$ kubectl -n servarr exec deploy/qbittorrent -c airvpn -- ip route get 213.152.161.87
213.152.161.87 dev tun0 src 10.135.227.175 uid 0
$ ... -- iptables-legacy -S | head -5
-P OUTPUT DROP
-A OUTPUT -d 109.235.50.7/32 -j ACCEPT      <- the connected server, and ONLY it
```

A ping from inside that netns measures `our line -> connected server -> AirVPN backbone ->
candidate`, not `our line -> candidate`. It is not the number we want, and for every candidate
except the connected one it is not even permitted. Probing must run from a netns outside the
tunnel.

**Why the agent cannot live outside the pod.** Bluetit is driven over its D-Bus system socket at
`/run/dbus/system_bus_socket`, which is container-local. Only a container in the same pod sharing
that directory can reach it.

**Why one scorer, not one per box.** Both boxes sit behind the same gateway and the same ISP line.
Measuring twice buys nothing and doubles the probe traffic.

Rejected alternatives are listed at the end.

## Component 1 - the scorer

One small always-on Deployment. Internal 15 minute timer, probes candidates itself, writes the
`/media` file, serves the same JSON over HTTP. One binary, one place to look.

- It runs in `servarr`, because the transport file lands on the `servarr-media` PVC, which is
  namespaced there. That PVC is `ReadWriteMany`, so mounting it alongside the five media pods is
  not a conflict.
- It is **not** the `airvpn-bluetit` image. It never talks to Bluetit and needs no Suite binaries.
- No `NET_ADMIN`, no `SYS_MODULE`. It needs outbound HTTPS to the AirVPN API and outbound ICMP to
  candidate entry IPs. Nothing else.

## Component 2 - the scoring rule

Filter, then rank, then probe. Two stages, in this order, every cycle.

**Stage 1 - shortlist from the API.** Hard filters, all mandatory:

| Filter | Value | Why |
|---|---|---|
| `health` | must be `ok` | `health == error` means the server is CLOSED to new connections, not merely degraded. AirVPN's own FAQ: "If a server is in error status, it doesn't accept connection". Existing sessions stay up, new ones are refused |
| Country | in `airwhitecountrylist` (`nl,de,se`) | matches what Bluetit will actually accept on connect |
| `currentload` | `<= 75` | drops the boxes `quick` keeps choosing |

Then rank the survivors by **absolute headroom**, `bw_max - bw`. Not bare load percent: a 2 Gbit
box and a 20 Gbit box at the same load are not equals. Take the **top 5**.

`health == ok` is a precondition, never a quality signal. It told us `Anser` was fine at 22% loss.
`health == warning` may be deprioritised, it is nearly free to do.

**Stage 2 - probe gate.** From a LAN vantage point, outside the tunnel:

- 300 ICMP packets at 5/s per candidate, about 60 s, candidates probed in parallel.
- **Reject any candidate over 1% loss.** This is a gate, not a weight.
- Rank the survivors by loss, then RTT, then load.

Probe size is settled by measurement, not taste. Packet count matters and window length barely
does: 600 packets over 600 s and 500 packets over 100 s agreed at `r=0.981`, mean difference 0.68
points. About 100 packets cleanly separates clean (0-1%) from broken (>15%) but cannot tell 4%
from 9% apart - 100-packet blocks for one server ranged 1.0 to 11.0. 300-500 packets settles the
mid-range to about +/-2 points. Loss is not bursty: the longest consecutive run of drops across
the whole dataset was 5 packets, so a short dense probe is a fair sample.

Probe one entry IP per server. `in1` versus `in3` is a non-question - across every server measured
the two differed by at most 0.8 points. The evidence used `ip_v4_in1`.

**Sanity anchors from the 2026-07-31 data.** A correct implementation must reproduce these:
`Dedalus` beats `Anser`; `Anser` is rejected by the probe gate at 22% loss; `Cygnus` is already
excluded on `health`.

## Component 3 - the published ranking contract

One JSON document carrying the full ranked top 5, not a single winner. The agent needs the list
for the top-5 band test (Component 5), for the cached-candidate fallback, and for walking the pool
on a failed connect (Component 7).

```json
{
  "schema": 1,
  "generated_at": "<RFC3339 UTC>",
  "ttl_seconds": 2100,
  "servers": [
    { "name": "Dedalus", "entry_ip": "109.235.50.5", "loss_pct": 0.0,
      "rtt_ms": 26.4, "load": 19, "bw_max": 20000, "headroom": 16801 }
  ]
}
```

- `name` is the exact server name for `--air-connect --air-server`. Exact match, see Component 6.
- Cadence: score every 15 min. `ttl_seconds: 2100` (35 min = two missed cycles plus slack).
- Past its TTL, **stale = absent**. One rule, no second staleness state.
- Versioning: `schema: 1`, consumers ignore unknown fields, a breaking change ships as a new
  filename (`ranking.v2.json`) so the non-GitOps consumer on Nick's VM can lag safely.

**Transport, two channels carrying the same bytes:**

| Consumer | Transport |
|---|---|
| `qbittorrent` pod in `servarr` | file at `/media/.vpn-picker/ranking.json` |
| Nick's VM (`192.168.10.41`) | plain HTTP on a `range-global` LoadBalancer IP (pool `192.168.10.110-192.168.10.199`) |

The dot-dir keeps the file invisible to the *arr media scanners.

**Why the obvious LoadBalancer-only answer is impossible.** The pod cannot reach RFC1918 or the
kube API outbound. Measured on the live connected pod:

```
--- LAN LoadBalancer 192.168.10.101 (Traefik) ---   http=000 time=6.005185 rc=28 (timeout)
--- ClusterIP 10.43.134.241:8080 (qbittorrent svc) --- http=000 time=6.002095 rc=28 (timeout)
--- k8s API 10.43.0.1:443 ---                       http=000              rc=28 (timeout)
```

Two mechanisms, either one sufficient. Bluetit installs ~35 split routes that carefully exclude
RFC1918, then installs a blunt `0.0.0.0/1` + `128.0.0.0/1` pair that re-swallows exactly those
ranges into `tun0`, where AirVPN drops them as unroutable. And `allowprivatenetwork` only emits
same-class-to-same-class rules (`-s 192.168.0.0/16 -d 192.168.0.0/16`), which a pod-sourced packet
never matches, because routing already sent it into the tunnel before the filter is relevant. A
LAN host survives this because it has a link-scope route to its own `/24` that beats a `/1`. A pod
has no such route, so `/0` loses to `/1`. There is no per-destination allow directive to punch a
hole.

`/media` NFS was verified writable from inside the pod and is not routed through `tun0`, so a file
there is the transport that actually works. Nick's VM is unaffected - its source IP is
`192.168.10.41`, which does match the LAN-to-LAN allow rule, so HTTP works for him.

## Component 4 - the agent sidecar

A second container in the `qbittorrent` pod, sharing `/run/dbus` as an `emptyDir` with the
`airvpn` container. Verified working on a scratch pod: `goldcrest --bluetit-status` from the
second container returned real Bluetit status, and the `emptyDir` does not break `dbus-daemon`.

- Same `ghcr.io/spyrospsarras/airvpn-bluetit` image, different command. It needs `goldcrest`, and
  building a second image to hold one binary buys nothing.
- `runAsUser: 0`, **no added capabilities**. `NET_ADMIN` and `SYS_MODULE` are not needed on the
  agent container. uid 1000 makes `goldcrest` SIGSEGV (exit 139) printing only its banner and no
  error - a silent failure a probe would misread as a dead tunnel.
- Failures on the agent side are clean. `goldcrest` exits 1 with `Connection refused` or
  `No such file or directory`, never hangs. A plain retry/backoff loop is the right behaviour.
- Local cache of the last good ranking on an `emptyDir`. No PVC: boot always starts on `quick`, so
  the cache only has to survive within one pod lifetime.

For Nick's VM: a shared named volume for `/run/dbus`, `network_mode: "service:airvpn"`, and
`user: "0"` on the agent service.

## Component 5 - when the agent switches

Three moments, deliberately separated. Boot is unchanged, degradation is conservative, everything
else is "stay put".

**Boot stays `airconnectatboot quick`.** Bluetit self-heals at boot exactly as today and both
probes keep working. `airconnectatboot off` was tested and is wrong: both `startupProbe` and
`livenessProbe` grep for `Connected to AirVPN server`, so if the agent ever fails to connect the
kubelet kills the `airvpn` container every ~3 minutes forever, with qBittorrent alive and
tunnel-less. Keeping `quick` costs one extra ~4-5 s switch per pod start and buys a boot failsafe
that survives a dead agent.

**Post-boot upgrade - sticky top-N band.** After boot the agent switches away from `quick`'s pick
only if that pick is **not** in the scorer's current top 5, or it fails the probe gate. Inside the
band, stay put. Ordinary pod restarts therefore cause no churn.

**Mid-session degradation - consecutive-failure hysteresis.** The agent watches the current server
only:

| Knob | Value | Why |
|---|---|---|
| Probe | 50 pings to the in-tunnel gateway `10.128.0.1`, every 60 s | in-tunnel, reachable because `allowping yes` |
| Bad window | loss `>= 5%` | the 2026-07-31 outage profile was 6.7-9% sustained |
| Trip | 3 consecutive bad windows (~3 min) | same semantics as `failureThreshold: 3` |
| Cooldown | 6 h between degradation switches | a switch changes the exit IP |
| Cap | max 3 switches per day | flap damping |

A fully dead tunnel is **exempt** - the liveness probe plus `quick` already own that path.

The cooldown is long on purpose. A switch changes the exit IP, which drops every peer and
re-announces to private trackers that have hit-and-run rules. We never switch to chase a
marginally better server.

**Fallback chain when the ranking is unreachable or stale:**

```
fresh ranking  >  cached last-good candidate  >  quick
```

- No ranking at boot: stay on `quick`'s pick.
- Degradation with no fresh ranking: switch to the cached candidate, the winner from the last good
  ranking the agent fetched.
- Blind `quick` is the final fallback only when no cache exists.
- The degradation bar, cooldown and cap apply to cached-candidate switches exactly as to ranked
  ones.

No path here can leave qBittorrent running without a tunnel. `networklockpersist` holds during
every gap, and boot ordering is unchanged.

## Component 6 - applying a switch

A live switch is **refused**. Bluetit will not `--air-connect` while already connected:

```
ERROR: Cannot start AirVPN connection. Bluetit is connected to VPN (WireGuard)
```

So a switch is two calls:

```
goldcrest --disconnect
goldcrest --air-connect --air-server <name> --async
```

**`--async` is mandatory.** Without it the tunnel is bound to the goldcrest process lifetime. The
mandatory `timeout N` wrapper kills goldcrest, its signal handler sends `stop_connection`, and the
tunnel comes down - so the agent as originally specced would leave the pod tunnel-less on every
switch. `head -c` does not save it, goldcrest survives SIGPIPE. With `--async`, goldcrest returns
in ~1.9 s, the tunnel survives, and no goldcrest processes are left behind.

**`--air-connect` must pass `--air-key` explicitly.** Bluetit does not re-read the `airkey`
directive on the connect path - proven during the #613 scratch-pod work: a credentialed
`goldcrest` call pushes a fresh option set into Bluetit, and the device key in that set is the
built-in `Default`. The country white/black lists ARE re-applied on the same call; the key
specifically is not. On a pod configured `airkey test`, the first run logged
`Selected user key: Default`. So every `--air-connect` the agent issues must carry `--air-key`
explicitly, never rely on the config directive alone. Consequence of skipping it: a switch on a
consumer using a non-default device key silently moves onto `Default`, and two sessions on one key
fight each other - the same failure mode Component 9 already warns about for scratch work. The
agent already does this correctly; this is a documentation gap only, no code change (#687).

**Timings, measured on a real switch** (scratch pod, device key `test`, live tunnel verified
monotonic throughout):

| Measurement | Value |
|---|---|
| `tun0` absence during a switch | **2.38 s**, at 0.25 s sampling, identical across two switches |
| Agent-observed wall time, including a status re-read | ~4 s |
| `--disconnect` + `--air-connect` pair | ~4-5 s |
| Boot connect on a warm pod | ~3 s |

**The lock stays armed for the whole gap.** `stop_vpn_connection()` calls `rollbackSession()`, never
`restore()`, when persistent mode is on. Every rule difference between connected and mid-switch is
a removed `ACCEPT` - the switch state is a strict subset of the connected rule set, never a
superset. Nothing gets out during those 2.38 s, including DNS, so tracker announces in flight
during the window fail and retry. That is the correct behaviour, not a leak.

**Server names exact-match on connect.** `--air-connect --air-server X` calls `getServerByName(X)`,
exact and case-insensitive. A non-exact name fails loudly with
`ERROR: AirVPN Server "Fel" does not exist.` The substring matching that made `Fel` hit both
`Felis` and `Felix` is `--air-list` behaviour only (`searchServer()`), and does not carry over.

**The pool is enforced, not bypassed.** `start_airvpn_connection()` applies all four lists
(`airwhiteserverlist`, `airblackserverlist`, `airwhitecountrylist`, `airblackcountrylist`) on the
explicit-server path. A winner outside the pool gets
`ERROR: AirVPN Server "X" is not allowed by <list> policy.` The CLI list flags cannot widen this -
they are validated against the rc directive first and are only read on the quick-connect path. So
the agent's winner must be inside `nl,de,se` and not one of `Cygnus,Hassaleh,Kajam`. Nothing to
change today, but the design honours the pool, it does not sidestep it.

**Re-assert `tun0` after every connect.** `tunpersist` is inert under WireGuard (Component 9), so
the device is destroyed and rebuilt on every switch. It comes back named `tun0` only because the
device-naming loop picks the first free `tun<N>` and slot 0 happens to be free. If a disconnect
ever fails to delete the device, the next connect lands on `tun1`, and qBittorrent - bound to
`tun0` in its config - opens no listen socket at all while the WebUI, both probes and ArgoCD stay
green. **The agent must assert the device is named `tun0` after every connect and refuse to
declare the switch successful otherwise.**

**Never signal Bluetit.** The agent's only Bluetit-affecting calls are `--bluetit-status`,
`--disconnect` and `--air-connect --async`. No `kill`, no SIGUSR2, no restarting the `airvpn`
container. The network lock does not survive SIGTERM (Component 9).

**Treat `--disconnect` as retryable.** It reads an uninitialized `VPNClient::Status` in its
busy-check, so do not trust its return value. Always re-read `--bluetit-status` afterwards.

Port 39998 survives a switch. There is zero port-forwarding code anywhere in the Suite - grepping
`src/` for `port.forward|portforward|forwarded_port|remote_forward` returns nothing - because the
forward is applied by AirVPN's own infrastructure, keyed on the account and device key. Confirmed
live after a `Felix -> Aspidiske` switch: 7 established inbound peer connections on the new
server, which cannot happen without a working forward on the new exit.

## Component 7 - when a connect fails (#557)

A switch is two calls, and the second one can fail. This is a different problem from "no ranking
available" (Component 5) - here the pod has already given up its tunnel, the lock is armed, and the
clock against the liveness probe is running.

**Verify, do not trust the return code.** `--async` returns in about 1.9 s, before the tunnel is
up, so its exit status proves nothing. After issuing the connect, poll `goldcrest --bluetit-status`
every 2 s for up to 30 s, and require `Connected to AirVPN server <ExactName>` matching the
requested server. Names are exact-match on the connect path (Component 6), so a mismatch is a
failure too.

**Bounded retry, then walk the pool.** Two attempts per candidate: immediate, then one more after
about 5 s of backoff with jitter. If both fail, move to the next candidate in the ranked top 5.
This is one more reason the contract publishes the full top 5 and not just a winner (Component 3).

**Terminal fallback is `quick`.** Connected to a mediocre server beats tunnel-less. Same last
resort as the fallback chain in Component 5.

**The whole recovery has to fit inside the liveness budget - the sharp part of this decision.** The
sidecar probe is `periodSeconds: 60` and `failureThreshold: 3`, about 180 s end to end. Cap
recovery at about 120 s to leave margin. Normally you would let the supervisor restart a process
that is stuck and move on. Not here: a kubelet-driven restart is a SIGTERM, and #535 already proved
SIGTERM removes the network lock (Component 9). The agent must never let the liveness restart
become its recovery path - it has to resolve the failure itself, inside the budget, every time.

**Circuit breaker.** If recovery ends on the `quick` fallback rather than a ranked candidate, treat
the switch as failed and suppress further degradation switches for the full 6 h cooldown (same
cooldown as Component 5). A bad pool must not cause thrash - every switch costs peer connections
and a private-tracker re-announce.

**Boot and mid-session are asymmetric.** At boot we are already connected via `quick`, so the
switch is optional: on failure, fail closed to the working state and stay on `quick`. Mid-session
the tunnel is already down before any of this logic runs, so recovery is mandatory.

**Jitter before switching.** Both qBittorrent instances read the same ranking. Jitter the switch
timing so the cluster pod and Nick's VM do not stampede the same winner right after a ranking
publishes.

## Component 8 - Nick's VM

Same contract, different transport, and it is designed for from day one rather than bolted on.

Repo-scope note added after this spec was written: Nick's box is managed directly on the machine
and is not tracked in this repo. The rest of this section stays as the point-in-time design record
it was.

- Same `ghcr.io/spyrospsarras/airvpn-bluetit` image already (#493, 2026-07-31), config
  bind-mounted from `/home/nick/work/bluetit.conf`, `airkey nick`.
- No NFS mount, so it reads the ranking over HTTP from the `range-global` LB IP.
- Everything else is identical: same JSON, same top-5 band test, same trigger numbers, same
  `--disconnect` / `--air-connect --async` switch.
- Rollout is cluster-first. His box is stage 4.

## Component 9 - constraints that will bite you

Each of these cost real debugging. Every one has the failure that proved it.

- **`--air-connect` without `--async` tears the tunnel down.** The `timeout` wrapper kills
  goldcrest, whose signal handler sends `stop_connection`. Non-negotiable.
- **goldcrest with no credentials prompt-loops.** `AirVPN Username:` forever, no TTY needed,
  **673 MB of output in 20 s**. The prompt has **no newline**, so `head -n` does not save you -
  bound the output by **bytes** with `head -c`. And `< /dev/null` does **not** stop it. Only the
  `timeout` does. Every goldcrest invocation gets `timeout N ... < /dev/null | head -c <bytes>`.
- **uid != 0 SIGSEGVs goldcrest.** Exit 139, banner printed, no error message. A probe reads it as
  a dead tunnel. The agent container runs `runAsUser: 0`.
- **The network lock does NOT survive SIGTERM.** About 1.4 s after `pkill -x bluetit` the OUTPUT
  chain is empty and policy is `-P OUTPUT ACCEPT`. Proved with an A/B against control `1.0.0.1`
  (never allow-listed by Bluetit): `http=301` in **7 ms** direct, against **73 ms** tunnelled
  through Amsterdam on the same probe seconds earlier. A clean `--disconnect` and SIGKILL are both
  safe - only SIGTERM leaks. So the agent never signals Bluetit. This contradicts the current
  comment in `bluetit-config.yaml`; correcting it is #535. A kubelet-driven restart from the
  liveness probe delivers this same SIGTERM - never let the liveness restart become your recovery
  path for a failed connect, it drops the lock instead of fixing anything (Component 7, #557).
- **`tunpersist` is inert under WireGuard.** `wireguardclient.cpp` never reads the directive, only
  the OpenVPN path in `bluetit.cpp` does. `WireGuardClient::stop()` unconditionally calls
  `wg_del_device()` on every disconnect and every reconnect. Nothing may assume `tun0` survives.
- **The shared `/run/dbus` needs the stale-file cleanup.** With `/run/dbus` as a shared `emptyDir`,
  the first `airvpn` container restart finds the previous run's `/run/dbus/pid`, `dbus-daemon`
  refuses to start, and the container CrashLoopBackOffs. Reproduced twice. The agent side fails
  cleanly and no amount of agent retry recovers it, because it is the `airvpn` container that is
  dead. Fixed in #524 - `entrypoint.sh` now removes `/run/dbus/pid` and
  `/run/dbus/system_bus_socket` on start. **This is a ship-blocker dependency: the agent sidecar
  must not deploy on an image built before that fix.**
- **Verify images by digest, not tag.** The `airvpn` container runs
  `ghcr.io/spyrospsarras/airvpn-bluetit:latest`. The tag tells you nothing about which build is
  actually live, and the #524 fix is exactly the kind of thing a stale `latest` will silently not
  have. Check the running pod's `imageID`.
- **Server-listing calls against the live daemon are not read-only.** On 2026-07-31 at 16:45 a
  goldcrest listing call (`set_options: air-sort -> score`, `air-limit -> 500`,
  `airvpn_server_list`) was immediately followed by a full tunnel outage: probe loss 100%, two
  strikes, liveness probe SIGKILLed the sidecar at 16:47:53, reconnect at 16:47:57. The scorer uses
  the REST API and never talks to Bluetit at all. The agent must not run listing calls against the
  live daemon.
- **`--air-rsort score` is DESCENDING.** It lists the worst server first. The "Felix 190, Felis
  1132" figures quoted while charting were a worst-first listing. Use `--air-sort score` if you
  ever need it - but see the point above, and #499's conclusion that the score is not the ranking
  we want anyway.
- **Scratch work uses device key `test`.** Never `Default` (live cluster) or `nick` (Nick's VM).
  Two sessions on one key fight each other.
- **`--air-bootserver-info` is about AirVPN's bootstrap manifest hosts, not exit servers.** It also
  prints "Bluetit options successfully reset", which is harmless - the live tunnel was verified
  unbroken after running it.

## Observability

Metrics are exported from day one, so the data already exists when alerting is fixed. Scraping
works because *inbound* traffic to the VPN pod is fine - only outbound RFC1918 is blackholed.

- **Scorer `/metrics`:** per-candidate `loss_pct`, `rtt_ms`, `load`, `headroom`;
  `ranking_generated_timestamp_seconds`; `candidates_passing_gate`; `scrape_success`.
- **Agent `/metrics`:** `current_server` info metric,
  `switches_total{reason="degradation|boot_upgrade|fallback"}`, `current_loss_pct`,
  `consecutive_bad_windows`, `ranking_age_seconds`.
- Both via `ServiceMonitor`, matching the existing kube-prometheus-stack setup.

**Alerting is DEFERRED.** Alertmanager email delivery is dead (#461) and will be fixed later. The
hard consequence, which this design honours: **no behaviour here may depend on a human being
told something.** Every failure path has to be safe unattended, which the fallback chain
(Component 5) and the conservative trigger give us.

Loki alert rule E in `2-k3s/10.observability/alertmanager-config/loki-log-alerts.yaml` is left
exactly as it is. Rewriting a rule that currently delivers nowhere is churn. It does become wrong
once alerting works, because it fires on degradation this mechanism will self-heal, so that
revisit is tracked separately.

## Rollout

Four progressive stages, each gated on a **manual** check, because no alert will tell us anything.

1. **Scorer only, nobody consuming.** Confirm the ranking file appears and its numbers look sane
   against a hand-run probe.
2. **Agent in dry-run.** It logs the switch it would make and takes no action. This is where the
   evidence comes from, so give it a real soak and read the logs. Longer than it would need if
   alerting worked.
3. **Agent enabled.**
4. **Nick's VM.**

**Rollback: flip the agent back to dry-run.** One flag, never a manifest revert.

**Proof of life at every stage** - inside the sidecar, `ss -tulnp | grep 39998` must show
`<vpn-ip>%tun0:39998` on both tcp and udp. A green WebUI proves nothing, and neither does a green
ArgoCD.

## Rejected alternatives

| Rejected | Why |
|---|---|
| Trust AirVPN's `server_best` | It picked `Anser` at 22% measured loss while reporting `health: ok`. Its probe mesh cannot see our path |
| Rank by Bluetit's own `Score` | No pinger, pure manifest function, favours small boxes. It agreed with `server_best` and was wrong the same way |
| Probe from inside the qbittorrent pod | Impossible. The pod's route table and network lock only permit the connected server, and the measurement would go through the tunnel anyway |
| Publish over a LoadBalancer or ClusterIP for both consumers | Impossible for the pod. Split routes plus the lock blackhole RFC1918 and the kube API outbound |
| One scorer per box | Both boxes are behind the same gateway and ISP line. Measuring twice buys nothing |
| `airconnectatboot off`, agent owns the first connect | Breaks `startupProbe` and `livenessProbe`, turning a dead agent into a permanent ~3 min restart loop with qBittorrent alive and tunnel-less |
| SIGUSR2 reconnect as the switch mechanism | It re-asks AirVPN for its recommendation and lands on the same server. It also means signalling Bluetit, which the SIGTERM finding rules out |
| Publish a single winner instead of the list | The agent needs the list for the top-5 band test and the cached-candidate fallback |
| Switch whenever a better server appears | A switch changes the exit IP, drops every peer, and re-announces to private trackers with hit-and-run rules. Degradation only |
| Rank by bare load percent | A 2 Gbit box and a 20 Gbit box at the same load are not equals. Rank by absolute headroom |
| Named-server whitelist instead of `nl,de,se` | Tried live on 2026-07-31 and Bluetit refused to boot: `ERROR: Reached end of AirVPN server list. No suitable server found.` The country list is the configuration that demonstrably connects |
| Widen the pool beyond `nl,de,se` | Decided against while charting. Revisit only if the pool proves too small |
| Any non-AirVPN provider | Out of scope |
| qBittorrent achieved throughput as a day-one signal | Declined in favour of the API shortlist plus ICMP probe. Not ruled out forever, see Open questions |
| Fix #491 (stale bundled manifest, costs one first-boot restart) | We stop depending on `airwhiteserverlist`, but the country whitelist stays and #491 remains its own live issue |
| Codify Nick's Bluetit stack into git as part of this | Not in git today (`/opt/qbit`, `/home/nick/work/bluetit.conf`). A prerequisite for building on his box, tracked separately, not a decision this design needs |

## Open questions

Four came off the map unspecified. Three more surfaced while writing this up. None of them block
review of the design, all of them block a finished build.

**From the map:**

1. **What the scorer serves while the AirVPN API itself is unreachable.** A stale cached ranking
   with an age stamp, or nothing at all. The contract fixed the payload and TTL that *consumers*
   see, not the scorer's own upstream-outage behaviour.
2. **Which vantage point probes.** The scorer is settled as one always-on Deployment outside the
   VPN netns, but not which node(s) it probes from, nor confirmation that a normal pod's ICMP
   egress to AirVPN entry IPs works via node SNAT without `hostNetwork`. First thing to verify in
   the build phase.
3. **The agent-side rule about signals is decided** - never signal Bluetit, only
   `--bluetit-status` / `--disconnect` / `--air-connect --async` (Component 6). What stays open is
   the other half: which signal a real kubelet-driven restart (a liveness-probe kill specifically)
   actually delivers to `bluetit`, and whether `entrypoint.sh` needs a SIGTERM trap that runs a
   clean `--disconnect`. Tracked in #535.
4. **Whether qBittorrent's achieved throughput gets added later as a demoting signal.** Declined as
   a day-one signal, not ruled out forever.

**Found while writing this spec:**

5. **Where the scorer lives in the repo, and which ArgoCD Application owns it.** The namespace
   follows from the `servarr-media` PVC being namespaced in `servarr`, but the directory, the
   kustomization and the App are not decided.
6. **Whether `ranking.json` is published atomically.** Nothing says write-temp-then-rename. A
   consumer polling the file can read a half-written document, and a JSON parse failure on a
   partial read is indistinguishable from a corrupt publish.
7. **Stage 4 has an unowned prerequisite.** Nick's VM cannot get the agent without either
   codifying his stack into git or hand-editing his compose. Codifying it is explicitly out of
   scope here and tracked separately, so stage 4 is blocked on work this design does not own.

## Out of scope

- Building, deploying and soaking any of this. This spec produces a design.
- Fixing #491.
- Codifying Nick's Bluetit stack into git.
- Widening the pool beyond `nl,de,se`.
- Any non-AirVPN provider.
- Achieved-throughput-as-a-signal for day one.
