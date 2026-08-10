# AirVPN Bluetit VPN layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace qBittorrent's pinned single AirVPN WireGuard endpoint with the official AirVPN Suite running as a sidecar, so a server whitelist/blacklist replaces the hardcoded IP, and a degrading tunnel is detected and surfaced instead of going unnoticed.

**Architecture:** One small image carries D-Bus + the AirVPN Suite. It runs as a Kubernetes native sidecar (initContainer with `restartPolicy: Always`) owning the WireGuard tunnel and the iptables network lock. The app container moves to `qbittorrentofficial/qbittorrent-nox` and rides the pod's shared network namespace - the current `tenseiken` image cannot run without a VPN at all. Nick's VM reuses the same image via `network_mode: "service:airvpn"`.

**Tech Stack:** Debian 13 slim, AirVPN Suite 2.1.0 (Bluetit daemon + Goldcrest client), Docker/GHCR, Kustomize + ksops/SOPS+age, ArgoCD, Prometheus/Loki alerts.

**Spec:** `docs/superpowers/specs/2026-07-30-airvpn-bluetit-vpn-layer-design.md`

## Global Constraints

- Work in worktree `/home/spy/Documents/Epaflix/k3s-swarm-proxmox-vpn` on branch `airvpn-bluetit-suite`.
- **Commit with `git -c core.hooksPath=.github/hooks commit ...`** in this worktree. `.git/hooks/pre-commit` is a symlink resolving to the *main* checkout's `.github/hooks`, so hook edits made here are otherwise ignored.
- **Never commit a plaintext `kind: Secret` YAML.** The pre-commit hook refuses it. Encrypted Secrets use the `.enc.yaml` suffix.
- Encrypting needs only the public age recipient in `.sops.yaml`. The private key is **not** on this workstation; read it from cluster Secret `argocd/sops-age` if a decrypt is needed.
- **Push aligned git BEFORE ArgoCD reconciles**, or automated sync reverts live to pre-change main.
- Merge policy: rebase onto `origin/main`, `push --force-with-lease`, wait for the `validate` check, then `gh pr merge <n> --merge`. **Never merge without Spyros's explicit OK.**
- Secrets live only in `.github/instructions/secrets.yml` (git-ignored). AirVPN account credentials are there as `airvpn_user` / `airvpn_password`, inside the existing `airvpn_*` block.
- `bluetit.rc` directives are **whitespace-separated**, not `key = value`.
- AirVPN Suite version pinned to `2.1.0`, sha512 `e17add5769b50683a4d2e480995fbe83d9f4b05b9738de58de9ce922ea80b13317b502ad4a49ee01bd23bcf10b8df96a3242fa3e5f9d20138665373c2445720d`.
- Image name: `ghcr.io/spyrospsarras/airvpn-bluetit`.
- Forwarded BT port is `39998` and is **account-wide**, so it follows whichever server is chosen.

## Prerequisite (manual, Spyros — blocks Task 4)

Task 4 connects a scratch pod to AirVPN. Connecting with the `Default` key would kick the live
qBittorrent session off the tunnel, and the `nick` key would kick Nick's VM. So:

**In the AirVPN client area, create a third device key named `k3s-test`.** There is no API for
creating keys — the generator API only references existing ones. Confirm afterwards with:

```bash
KEY=$(sops -d --extract '["airvpn_api_key"]' .github/instructions/secrets.enc.yaml)
curl -sG https://airvpn.org/api/ --data-urlencode "key=$KEY" -d service=devices \
  | python3 -m json.tool | grep -iE '"name"|wireguard_ipv4'
```

Expected: three devices — `Default` (`10.135.227.175`), `nick` (`10.154.38.229`), and the new
`k3s-test` with its own address.

## File Structure

**Create:**
- `images/airvpn-bluetit/Dockerfile` — the image. Bypasses the interactive `install.sh`.
- `images/airvpn-bluetit/entrypoint.sh` — renders `bluetit.rc`, starts D-Bus + syslog + Bluetit, runs the degradation probe, holds the foreground.
- `images/airvpn-bluetit/README.md` — why the image exists, how to build and bump it.
- `2-k3s/08.servarr/_shared/secrets/airvpn-credentials.enc.yaml` — SOPS Secret, `airusername` + `airpassword` only.
- `2-k3s/08.servarr/qbittorrent/bluetit-config.yaml` — ConfigMap with the non-secret directives.
- `.github/workflows/build-airvpn-bluetit.yml` — build + push to GHCR.
- `1-proxmox/user-vms/nick/docker-compose.yml` — Nick's stack, brought into git.
- `1-proxmox/user-vms/nick/README.md` — how his stack is deployed.

**Modify:**
- `2-k3s/08.servarr/qbittorrent/qbittorrent.yaml` — add the sidecar, strip the VPN wrapper env, the `postStart` hook, and `privileged`/`NET_ADMIN` from the app container.
- `2-k3s/08.servarr/kustomization.yaml` — add the ConfigMap resource and the pinned image digest.
- `2-k3s/08.servarr/_shared/secrets/ksops-generator.yaml` — add `airvpn-credentials.enc.yaml`.
- `.github/renovate.json` — `customManager` for `AIRVPN_SUITE_VERSION`.
- `2-k3s/10.observability/alertmanager-config/loki-log-alerts.yaml` — reconnect alert.
- `2-k3s/10.observability/alertmanager-config/custom-alerts.yaml:190-203` — the #352 flap alert description references the old kill-switch mechanism.

**Delete (Task 9, only after soak passes):**
- `2-k3s/08.servarr/_shared/secrets/qbittorrent-wireguard.enc.yaml` and its `ksops-generator.yaml` entry.
- `2-k3s/08.servarr/_shared/secrets/wireguard-secret.yaml` template.

---

### Task 1: Build the image

**Files:**
- Create: `images/airvpn-bluetit/Dockerfile`
- Create: `images/airvpn-bluetit/entrypoint.sh`
- Create: `images/airvpn-bluetit/README.md`

**Interfaces:**
- Consumes: nothing.
- Produces: a local image tag `airvpn-bluetit:dev`. The entrypoint reads env `AIRVPN_USERNAME`, `AIRVPN_PASSWORD`, and optional `PROBE_TARGET` (default `10.128.0.1`), `PROBE_INTERVAL` (`60`), `PROBE_COUNT` (`20`), `PROBE_LOSS_THRESHOLD` (`5`), `PROBE_STRIKES` (`3`), `PROBE_COOLDOWN` (`900`). It reads a config fragment from `/config/bluetit.conf`.

- [ ] **Step 1: Write the verification script**

Create `images/airvpn-bluetit/test.sh`:

```bash
#!/usr/bin/env bash
# Verifies the image without connecting to AirVPN (no credentials => airconnectatboot
# never fires, so the live tunnel is untouched).
set -euo pipefail
IMAGE="${1:-airvpn-bluetit:dev}"

echo "== 1. both binaries resolve every library =="
# NB: `grep -q X && { ...; }` is wrong under `set -e` - when grep finds nothing it
# returns 1, the && list returns 1, and the script exits on the SUCCESS path.
# Use if/then for every "fail if found" check in this file.
if docker run --rm "$IMAGE" sh -c 'ldd /sbin/bluetit; ldd /usr/local/bin/goldcrest' \
     | grep -q "not found"; then
  echo "FAIL: missing shared library"; exit 1
fi
echo "ok"

echo "== 2. bluetit starts, logs to stdout, writes its lock file =="
# No /config mount here on purpose: without the fragment there is no
# `airconnectatboot quick`, so Bluetit never dials out and the live tunnel
# (which uses the same `Default` key) is not disturbed.
out=$(docker run --rm --cap-add NET_ADMIN -e AIRVPN_USERNAME= -e AIRVPN_PASSWORD= "$IMAGE" \
  sh -c '/entrypoint.sh & sleep 12; cat /etc/airvpn/bluetit.lock 2>/dev/null || echo NOLOCK' 2>&1)
if ! echo "$out" | grep -q "Bluetit successfully initialized and ready"; then
  echo "FAIL: no init line on stdout (syslog forwarder broken?)"; echo "$out"; exit 1
fi
if echo "$out" | grep -q NOLOCK; then
  echo "FAIL: no lock file"; echo "$out"; exit 1
fi
echo "ok"

echo "== 3. our directives landed in bluetit.rc, shipped ones survived =="
docker run --rm -e AIRVPN_USERNAME=u -e AIRVPN_PASSWORD=p \
  -v "$PWD/testdata:/config:ro" "$IMAGE" sh -c '
    . /entrypoint-render.sh
    grep -q "^rsamodulus" /etc/airvpn/bluetit.rc || { echo "FAIL: shipped rsamodulus lost"; exit 1; }
    grep -q "^bootserver" /etc/airvpn/bluetit.rc || { echo "FAIL: shipped bootserver lost"; exit 1; }
    grep -q "^airvpntype                  wireguard" /etc/airvpn/bluetit.rc || { echo "FAIL: fragment not appended"; exit 1; }
    grep -q "^airusername                 u" /etc/airvpn/bluetit.rc || { echo "FAIL: username not substituted"; exit 1; }
    echo ok'
echo "ALL CHECKS PASSED"
```

Create `images/airvpn-bluetit/testdata/bluetit.conf` with the Task 3 ConfigMap body (same content, used only by check 3).

- [ ] **Step 2: Run it to confirm it fails**

```bash
cd images/airvpn-bluetit && chmod +x test.sh && ./test.sh
```

Expected: FAIL — `Unable to find image 'airvpn-bluetit:dev'`.

- [ ] **Step 3: Write the Dockerfile**

`images/airvpn-bluetit/Dockerfile`:

```dockerfile
# AirVPN Suite (Bluetit daemon + Goldcrest client) for use as a VPN sidecar.
#
# Why we build this at all: AirVPN ships only a tarball, there is no official
# image, and Bluetit is the only client that takes a server whitelist/blacklist
# and picks a recommended server. See
# docs/superpowers/specs/2026-07-30-airvpn-bluetit-vpn-layer-design.md
#
# install.sh from the tarball is deliberately NOT used: it prompts about systemd,
# boot-start and creating the airvpn user/group, which hangs a build. We place
# the files ourselves and skip the systemd units entirely.
FROM debian:13-slim

ARG AIRVPN_SUITE_VERSION=2.1.0
ARG AIRVPN_SUITE_SHA512=e17add5769b50683a4d2e480995fbe83d9f4b05b9738de58de9ce922ea80b13317b502ad4a49ee01bd23bcf10b8df96a3242fa3e5f9d20138665373c2445720d

# libsystemd0 and libcap2 are NOT in the README's dependency list but both
# binaries link them - verified with ldd. busybox provides the syslogd that
# forwards Bluetit's logs to stdout; Bluetit has no logfile directive at all,
# so without it the container is completely silent.
RUN apt-get update && apt-get install -y --no-install-recommends \
      dbus libdbus-1-3 libssl3 libstdc++6 libsystemd0 libcap2 \
      zlib1g libbrotli1 libzstd1 libxml2 \
      iptables iproute2 iputils-ping busybox procps ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/suite.tar.gz \
      "https://gitlab.com/AirVPN/AirVPN-Suite/-/raw/master/binary/AirVPN-Suite-x86_64-${AIRVPN_SUITE_VERSION}.tar.gz" \
 && echo "${AIRVPN_SUITE_SHA512}  /tmp/suite.tar.gz" | sha512sum -c - \
 && tar xzf /tmp/suite.tar.gz -C /tmp \
 && install -m 0755 /tmp/AirVPN-Suite/bin/bluetit   /sbin/bluetit \
 && install -m 0755 /tmp/AirVPN-Suite/bin/goldcrest /usr/local/bin/goldcrest \
 && mkdir -p /etc/airvpn /etc/dbus-1/system.d \
 && cp /tmp/AirVPN-Suite/etc/airvpn/* /etc/airvpn/ \
 && cp /tmp/AirVPN-Suite/etc/dbus-1/system.d/*.conf /etc/dbus-1/system.d/ \
 && cp /tmp/AirVPN-Suite/etc/airvpn/bluetit.rc /etc/airvpn/bluetit.rc.shipped \
 && groupadd -f airvpn \
 && rm -rf /tmp/suite.tar.gz /tmp/AirVPN-Suite

COPY entrypoint.sh /entrypoint.sh
COPY entrypoint-render.sh /entrypoint-render.sh
RUN chmod 0755 /entrypoint.sh /entrypoint-render.sh

ENTRYPOINT ["/entrypoint.sh"]
```

- [ ] **Step 4: Write the config renderer**

Split out so the test can exercise rendering without starting the daemon.

`images/airvpn-bluetit/entrypoint-render.sh`:

```sh
#!/bin/sh
# Renders /etc/airvpn/bluetit.rc = shipped template + our fragment + credentials.
#
# The shipped file carries bootserver/rsaexponent/rsamodulus, which are REQUIRED
# for AirVPN support to work at all. Always start from the pristine copy so a
# container restart cannot append twice.
set -eu

RC=/etc/airvpn/bluetit.rc
FRAGMENT=${BLUETIT_CONFIG:-/config/bluetit.conf}

cp /etc/airvpn/bluetit.rc.shipped "$RC"

{
  echo ""
  echo "# --- appended by entrypoint-render.sh, do not edit by hand ---"
  if [ -f "$FRAGMENT" ]; then
    cat "$FRAGMENT"
  fi
  printf 'airusername                 %s\n' "${AIRVPN_USERNAME:-}"
  printf 'airpassword                 %s\n' "${AIRVPN_PASSWORD:-}"
} >> "$RC"

chmod 0600 "$RC"
```

- [ ] **Step 5: Write the entrypoint**

`images/airvpn-bluetit/entrypoint.sh`:

```sh
#!/bin/sh
# AirVPN Bluetit sidecar entrypoint.
#
# Bluetit forks and returns 0, so this script has to hold the foreground and
# watch the daemon. It also runs the degradation probe: a server that stays up
# but drops packets is invisible to both the network lock and a restart, and
# that is exactly the failure that caused this work (9% loss on the entry IP,
# 60% inside the tunnel, upload down to 0.03 MB/s).
set -eu

PROBE_TARGET=${PROBE_TARGET:-10.128.0.1}
PROBE_INTERVAL=${PROBE_INTERVAL:-60}
PROBE_COUNT=${PROBE_COUNT:-20}
PROBE_LOSS_THRESHOLD=${PROBE_LOSS_THRESHOLD:-5}
PROBE_STRIKES=${PROBE_STRIKES:-3}
PROBE_COOLDOWN=${PROBE_COOLDOWN:-900}

log() { echo "airvpn-bluetit: $*"; }

/entrypoint-render.sh

mkdir -p /run/dbus
dbus-daemon --system --fork

# Bluetit logs ONLY to syslog - there is no logfile directive. Without this the
# container emits nothing and a healthy daemon looks like a silent crash.
busybox syslogd -n -O /dev/stdout &

rm -f /etc/airvpn/bluetit.lock
/sbin/bluetit

i=0
while [ ! -f /etc/airvpn/bluetit.lock ]; do
  i=$((i + 1))
  if [ "$i" -gt 30 ]; then
    log "bluetit did not create its lock file within 30s - giving up"
    exit 1
  fi
  sleep 1
done

BLUETIT_PID=$(cat /etc/airvpn/bluetit.lock)
log "bluetit running pid=${BLUETIT_PID}"

# ponytail: monotonic clock via /proc/uptime instead of pulling in date maths.
now_secs() { cut -d. -f1 /proc/uptime; }

strikes=0
last_reconnect=0

while kill -0 "$BLUETIT_PID" 2>/dev/null; do
  sleep "$PROBE_INTERVAL"

  loss=$(ping -c "$PROBE_COUNT" -i 0.2 -W 2 "$PROBE_TARGET" 2>/dev/null \
         | sed -n 's/.*[^0-9]\([0-9][0-9]*\)% packet loss.*/\1/p' | tail -1)
  [ -n "${loss:-}" ] || loss=100

  if [ "$loss" -gt "$PROBE_LOSS_THRESHOLD" ]; then
    strikes=$((strikes + 1))
    log "probe loss=${loss}% strike=${strikes}/${PROBE_STRIKES}"
  else
    strikes=0
  fi

  if [ "$strikes" -ge "$PROBE_STRIKES" ]; then
    now=$(now_secs)
    if [ $((now - last_reconnect)) -ge "$PROBE_COOLDOWN" ]; then
      # This exact string is what the Loki alert matches - keep them in step.
      log "reconnect triggered, loss=${loss}%"
      kill -USR2 "$BLUETIT_PID"
      last_reconnect=$now
    else
      log "reconnect suppressed by cooldown, loss=${loss}%"
    fi
    strikes=0
  fi
done

log "bluetit exited - failing so the container restarts"
exit 1
```

- [ ] **Step 6: Build and run the checks**

```bash
cd images/airvpn-bluetit
docker build -t airvpn-bluetit:dev .
./test.sh
```

Expected: `ALL CHECKS PASSED`.

If check 1 fails with a missing library, add the package and rebuild — that is AirVPN's own documented validation and the authority on the package list.

- [ ] **Step 7: Write the README**

`images/airvpn-bluetit/README.md` covering: why we build it (no official image; only Bluetit takes server lists), that `install.sh` is bypassed because it prompts, that the syslog forwarder is mandatory because Bluetit has no `logfile` directive, how to bump (`AIRVPN_SUITE_VERSION` + the sha512 from `<tarball>.sha512`, which Renovate raises as a PR), and how to run `test.sh`.

- [ ] **Step 8: Commit**

```bash
git add images/airvpn-bluetit
git -c core.hooksPath=.github/hooks commit -m "feat(images): AirVPN Bluetit sidecar image

Bluetit is the only AirVPN client that takes a server whitelist/blacklist and
picks a recommended server; WireGuard cannot fail over inside one config. No
official image exists, so build one from the pinned 2.1.0 tarball with its
published sha512 verified at build time.

install.sh is bypassed because it prompts about systemd and user creation. A
busybox syslogd forwards Bluetit's logs to stdout - it has no logfile directive,
so without that the container is silent even when healthy.

The entrypoint also runs the degradation probe that sends SIGUSR2 to force a
reconnect, which is the only thing that catches a server that stays up and goes
slow."
```

---

### Task 2: Publish the image and let Renovate track the Suite

**Files:**
- Create: `.github/workflows/build-airvpn-bluetit.yml`
- Modify: `.github/renovate.json`

**Interfaces:**
- Consumes: `images/airvpn-bluetit/` from Task 1.
- Produces: `ghcr.io/spyrospsarras/airvpn-bluetit:<sha>` plus `:latest`, and a digest for Task 5.

- [ ] **Step 1: Write the workflow**

```yaml
name: build-airvpn-bluetit

on:
  push:
    branches: [main]
    paths: ['images/airvpn-bluetit/**', '.github/workflows/build-airvpn-bluetit.yml']
  pull_request:
    paths: ['images/airvpn-bluetit/**', '.github/workflows/build-airvpn-bluetit.yml']
  workflow_dispatch:

permissions:
  contents: read
  packages: write

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Verify the image before publishing anything
        run: |
          cd images/airvpn-bluetit
          docker build -t airvpn-bluetit:dev .
          chmod +x test.sh && ./test.sh

      - name: Log in to GHCR
        if: github.event_name != 'pull_request'
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push
        if: github.event_name != 'pull_request'
        uses: docker/build-push-action@v6
        with:
          context: images/airvpn-bluetit
          push: true
          tags: |
            ghcr.io/spyrospsarras/airvpn-bluetit:${{ github.sha }}
            ghcr.io/spyrospsarras/airvpn-bluetit:latest

      - name: Print the digest to pin
        if: github.event_name != 'pull_request'
        run: docker buildx imagetools inspect ghcr.io/spyrospsarras/airvpn-bluetit:${{ github.sha }} --format '{{.Manifest.Digest}}'
```

On a pull request it builds and tests but does not push, so a broken image cannot reach GHCR.

- [ ] **Step 2: Confirm the existing `validate` gate is untouched**

```bash
grep -n "validate" .github/workflows/ci.yml | head
```

Expected: the `validate` job in `ci.yml` is unchanged. The new workflow is additive and must not become a required check without Spyros's say.

- [ ] **Step 3: Add the Renovate customManager**

In `.github/renovate.json`, inside `customManagers`:

```json
{
  "customType": "regex",
  "description": "AirVPN Suite tarball version in the Bluetit image",
  "managerFilePatterns": ["/^images/airvpn-bluetit/Dockerfile$/"],
  "matchStrings": ["ARG AIRVPN_SUITE_VERSION=(?<currentValue>\\S+)"],
  "depNameTemplate": "AirVPN/AirVPN-Suite",
  "datasourceTemplate": "gitlab-tags",
  "registryUrlTemplate": "https://gitlab.com"
}
```

Renovate only ever sees the image tag we publish ourselves, so without this nothing would report 2.1.0 as stale. Note the bump also needs the sha512 updating by hand — say so in the image README.

- [ ] **Step 4: Validate the Renovate config parses**

```bash
npx --yes renovate-config-validator .github/renovate.json
```

Expected: no errors. If `renovate-config-validator` is unavailable offline, at minimum `python3 -c "import json;json.load(open('.github/renovate.json'))"` must pass.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/build-airvpn-bluetit.yml .github/renovate.json
git -c core.hooksPath=.github/hooks commit -m "ci: build and publish the AirVPN Bluetit image

Builds and runs test.sh on pull requests without pushing, so a broken image
cannot reach GHCR. Adds a Renovate customManager for AIRVPN_SUITE_VERSION -
Renovate only sees the tag we publish, so nothing else would ever flag the
pinned 2.1.0 tarball as stale. Leaves the existing validate gate alone."
```

---

### Task 3: Configuration - ConfigMap and SOPS Secret

**Files:**
- Create: `2-k3s/08.servarr/qbittorrent/bluetit-config.yaml`
- Create: `2-k3s/08.servarr/_shared/secrets/airvpn-credentials.enc.yaml`
- Modify: `2-k3s/08.servarr/_shared/secrets/ksops-generator.yaml`
- Modify: `2-k3s/08.servarr/kustomization.yaml`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: ConfigMap `airvpn-bluetit-config` with key `bluetit.conf`, and Secret `airvpn-credentials` with keys `airusername` and `airpassword`. Task 5's Deployment mounts both.

- [ ] **Step 1: Write the ConfigMap**

`2-k3s/08.servarr/qbittorrent/bluetit-config.yaml`:

```yaml
---
# Non-secret Bluetit directives. Kept out of the Secret on purpose so that
# server-pool changes are readable in a PR diff.
#
# Syntax is DIRECTIVE<whitespace>VALUE - not `key = value`. Copied from the
# shipped bluetit.rc, which this fragment is appended to at container start.
apiVersion: v1
kind: ConfigMap
metadata:
  name: airvpn-bluetit-config
  namespace: servarr
data:
  bluetit.conf: |
    # quick = connect to an AirVPN-recommended server from the whitelist,
    # minus the blacklist. This is the whole point of using Bluetit: WireGuard
    # cannot fail over inside one config, so the client has to choose.
    airconnectatboot            quick
    airvpntype                  wireguard
    airport                     1637

    # Named 20 Gbit servers, NOT a country whitelist - a country list readmits
    # the 2 Gbit boxes that caused the outage. All measured at 0% loss.
    airwhiteserverlist          Dedalus,Piautos,Dalim,Menkent,Felix,Adhil,Ashlesha

    # Measured bad on 2026-07-30 from k3s-worker-61: 6.7-9% packet loss each,
    # all three are 2 Gbit boxes in Alblasserdam at 50-57% load, and
    # nl3.vpn.airdns.org rotates between them. AirVPN's own status API still
    # called them health=ok, so do not trust that flag - measure before editing
    # this list. Recipe is in the design doc.
    airblackserverlist          Cygnus,Hassaleh,Kajam

    # Home country. Bluetit auto-detects this correctly via ipleak.net, but
    # pinning it avoids an outbound call on every start.
    country                     NO
    forbidquickhomecountry      yes

    # Real killswitch, replacing the postStart iptables hack. allowprivatenetwork
    # keeps Pi-hole DNS (192.168.10.30), the cluster CIDRs and the WebUI
    # reachable. allowping is REQUIRED - without it the lock drops the ICMP the
    # degradation probe runs on and the probe reads a healthy tunnel as dead.
    #
    # networklockPERSIST, not networklock: the plain form is scoped to "during
    # the connection", so a sidecar restart leaves the app container announcing
    # to trackers unprotected for the ~20s Bluetit needs to reconnect. Do NOT
    # simplify this back - see the full reasoning in bluetit-config.yaml.
    networklockpersist          iptables
    allowprivatenetwork         yes
    allowping                   yes

    # init-sysctls does NOT disable IPv6 - doing so kills the tunnel.
    # NB: neither of these stops Bluetit assigning the device's IPv6 to tun0.
    # They express intent only. The pod must NOT disable IPv6 - see Task 5.
    airipv6                     no
    air6to4                     no

    # REQUIRED. Without it Bluetit throws "Cannot set pushed DNS to system"
    # (kubelet owns /etc/resolv.conf) and that exception tears down a tunnel
    # that has ALREADY connected. Verified on a scratch pod 2026-07-30.
    ignorednspush               yes

    # SIGUSR2 reconnect only works with TUN persistence on. The probe depends
    # on it - do not set this to no.
    tunpersist                  yes

    # AirVPN device key. Default = the cluster (10.135.227.175). Nick's VM uses
    # its own key `nick`, so the two never share key material.
    airkey                      Default
```

- [ ] **Step 2: Build the encrypted Secret**

Write the plaintext **outside the repo** so the pre-commit hook never sees it, and name it `.enc.yaml` so the `.sops.yaml` `path_regex` matches:

```bash
mkdir -p /tmp/airvpn-secret && cd /tmp/airvpn-secret
REPO=/home/spy/Documents/Epaflix/k3s-swarm-proxmox-vpn
S=$REPO/../k3s-swarm-proxmox/.github/instructions/secrets.enc.yaml
U=$(sops -d --extract '["airvpn_user"]' "$S")
P=$(sops -d --extract '["airvpn_password"]' "$S")
cat > airvpn-credentials.enc.yaml <<YAML
apiVersion: v1
kind: Secret
metadata:
    name: airvpn-credentials
    namespace: servarr
type: Opaque
stringData:
    airusername: "${U}"
    airpassword: "${P}"
YAML
sops --encrypt --config $REPO/.sops.yaml --input-type yaml --output-type yaml \
  airvpn-credentials.enc.yaml > $REPO/2-k3s/08.servarr/_shared/secrets/airvpn-credentials.enc.yaml
shred -u airvpn-credentials.enc.yaml
```

- [ ] **Step 3: Verify no plaintext leaked and the round-trip works**

```bash
cd /home/spy/Documents/Epaflix/k3s-swarm-proxmox-vpn
grep -c "airusername: [^E]" 2-k3s/08.servarr/_shared/secrets/airvpn-credentials.enc.yaml
AGEKEY=$(kubectl --context epaflix get secret sops-age -n argocd -o jsonpath='{.data}' \
  | python3 -c "import sys,json,base64;[print(base64.b64decode(v).decode(),end='') for v in json.load(sys.stdin).values()]")
SOPS_AGE_KEY="$AGEKEY" sops -d 2-k3s/08.servarr/_shared/secrets/airvpn-credentials.enc.yaml \
  | sed -E 's/(airusername|airpassword): .*/\1: <redacted>/'
unset AGEKEY
```

Expected: the `grep -c` prints `0` (the value is ciphertext), and the decrypt prints a well-formed Secret with both keys present.

- [ ] **Step 4: Register the Secret and ConfigMap**

In `2-k3s/08.servarr/_shared/secrets/ksops-generator.yaml`, add to `files:`:

```yaml
  - _shared/secrets/airvpn-credentials.enc.yaml
```

In `2-k3s/08.servarr/kustomization.yaml`, add next to the other qbittorrent entries (after `qbittorrent/priority-class.yaml`):

```yaml
  - qbittorrent/bluetit-config.yaml
```

and add the matching comment line to the `# Reconciled via the ksops generator` block near line 22:

```yaml
#   - airvpn-credentials              from _shared/secrets/airvpn-credentials.enc.yaml
```

- [ ] **Step 5: Confirm the render succeeds**

```bash
cd /home/spy/Documents/Epaflix/k3s-swarm-proxmox-vpn
export PATH="$HOME/.local/bin:$PATH"
export SOPS_AGE_KEY=$(kubectl --context epaflix get secret sops-age -n argocd -o jsonpath='{.data}' \
  | python3 -c "import sys,json,base64;[print(base64.b64decode(v).decode(),end='') for v in json.load(sys.stdin).values()]")
kustomize build --enable-alpha-plugins --enable-exec 2-k3s/08.servarr >/dev/null && echo "render ok"
unset SOPS_AGE_KEY
```

Expected: `render ok`.

**CI does not cover this render — do not assume it does.** `.github/workflows/ci.yml`
deliberately excludes `2-k3s/08.servarr` from its `kustomize build` loop, because the ksops
generator needs the age key and CI withholds it. The list of directories it does build is
hardcoded and `08.servarr` is not in it. So if you skip this step, nothing verifies the render
until ArgoCD tries to sync it.

`kustomize` 5.8.1 (matching the CI pin) and `ksops` 4.5.1 are installed at `~/.local/bin` —
hence the `PATH` export, `--enable-alpha-plugins --enable-exec` for the ksops exec plugin, and
the age key pulled from the cluster. Never report a green render you did not run.

- [ ] **Step 6: Commit**

```bash
git add 2-k3s/08.servarr/qbittorrent/bluetit-config.yaml \
        2-k3s/08.servarr/_shared/secrets/airvpn-credentials.enc.yaml \
        2-k3s/08.servarr/_shared/secrets/ksops-generator.yaml \
        2-k3s/08.servarr/kustomization.yaml
git -c core.hooksPath=.github/hooks commit -m "feat(servarr): Bluetit config and AirVPN credentials

Server pool lives in a ConfigMap so changes are reviewable in a PR diff; only
the account username/password go in the SOPS Secret. The blacklist names the
three Alblasserdam boxes measured at 6.7-9% loss on 2026-07-30."
```

---

### Task 4: Verify the two risky assumptions on a scratch pod

**Blocked by:** the manual AirVPN `k3s-test` device key (see Prerequisite).

**Files:** none committed. This task produces evidence, and possibly a spec correction.

**Interfaces:**
- Consumes: the image from Task 2 and the ConfigMap from Task 3.
- Produces: a go/no-go on (a) `VPN_ENABLED=no`, (b) `tunpersist` without `/dev/net/tun`, and the exact connected-state string for Task 5's probe.

The spec calls both of these out as unverified. **Do not skip this task and do not touch the live pod first.** If either fails, stop and report rather than improvising.

- [ ] **Step 1: Confirm the live pod is untouched by what follows**

```bash
kubectl --context epaflix get pod -n servarr -l app=qbittorrent -o wide
kubectl --context epaflix exec -n servarr deploy/qbittorrent -c qbittorrent -- wg show | grep -E "endpoint|handshake"
```

Record the endpoint and restart count so a regression is provable later.

- [ ] **Step 2: Create the scratch namespace and a test ConfigMap using the test key**

```bash
kubectl --context epaflix create namespace airvpn-test --dry-run=client -o yaml | kubectl --context epaflix apply -f -
kubectl --context epaflix -n servarr get cm airvpn-bluetit-config -o yaml \
  | sed 's/namespace: servarr/namespace: airvpn-test/; s/^\(\s*\)airkey  *Default/\1airkey                      k3s-test/' \
  | kubectl --context epaflix apply -f -
kubectl --context epaflix -n servarr get secret airvpn-credentials -o yaml \
  | sed 's/namespace: servarr/namespace: airvpn-test/' \
  | grep -v "annotations\|argocd" | kubectl --context epaflix apply -f -
```

Using `k3s-test` is what keeps the live tunnel and Nick's VM connected.

- [ ] **Step 3: Run the scratch pod**

```bash
cat <<'YAML' | kubectl --context epaflix apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: airvpn-scratch
  namespace: airvpn-test
spec:
  restartPolicy: Never
  dnsPolicy: None
  dnsConfig:
    nameservers: ["192.168.10.30", "1.1.1.1"]
  initContainers:
    - name: init-sysctls
      image: busybox:latest
      command: ["/bin/sh","-c","sysctl -w net.ipv4.conf.all.rp_filter=2; sysctl -w net.ipv4.conf.all.src_valid_mark=1; sysctl -w net.ipv6.conf.all.disable_ipv6=1"]
      securityContext: { privileged: true }
    - name: airvpn
      image: ghcr.io/spyrospsarras/airvpn-bluetit:latest
      restartPolicy: Always
      env:
        - name: AIRVPN_USERNAME
          valueFrom: { secretKeyRef: { name: airvpn-credentials, key: airusername } }
        - name: AIRVPN_PASSWORD
          valueFrom: { secretKeyRef: { name: airvpn-credentials, key: airpassword } }
      volumeMounts:
        - { name: bluetit-config, mountPath: /config, readOnly: true }
      securityContext:
        runAsUser: 0
        capabilities: { add: ["NET_ADMIN"] }
  containers:
    - name: qbittorrent
      image: tenseiken/qbittorrent-wireguard:latest
      env:
        - { name: VPN_ENABLED, value: "no" }
        - { name: PUID, value: "568" }
        - { name: PGID, value: "568" }
        - { name: TZ, value: "Europe/Oslo" }
        - { name: QBT_LEGAL_NOTICE, value: "confirm" }
  volumes:
    - name: bluetit-config
      configMap:
        name: airvpn-bluetit-config
        items: [{ key: bluetit.conf, path: bluetit.conf }]
YAML
```

- [ ] **Step 4: Check assumption (a) - `VPN_ENABLED=no` leaves iptables alone**

```bash
kubectl --context epaflix -n airvpn-test logs airvpn-scratch -c qbittorrent | head -40
kubectl --context epaflix -n airvpn-test exec airvpn-scratch -c airvpn -- iptables -S | head -30
```

Expected: the qBittorrent container does not install a DROP policy of its own, and the only rules present are Bluetit's network lock. If qBittorrent still writes iptables rules, **stop** — the fallback is a stock qBittorrent image plus a `/config` migration, which is separate work and needs Spyros's decision.

- [ ] **Step 5: Check assumption (b) and capture the connected string**

```bash
kubectl --context epaflix -n airvpn-test exec airvpn-scratch -c airvpn -- goldcrest --bluetit-status
kubectl --context epaflix -n airvpn-test exec airvpn-scratch -c airvpn -- wg show 2>/dev/null | grep -E "endpoint|handshake"
kubectl --context epaflix -n airvpn-test logs airvpn-scratch -c airvpn | grep -iE "connected|network filter|lock" | tail -20
```

Expected: it connects with **no** `/dev/net/tun` mount. Record the exact connected wording — the disconnected form is `Bluetit is not connected`, so any grep must be anchored to avoid matching it. If it fails to connect without the device, add `/dev/net/tun` back to the Deployment in Task 5 and correct the spec.

- [ ] **Step 6: Confirm the chosen server honours the blacklist, and measure**

```bash
kubectl --context epaflix -n airvpn-test exec airvpn-scratch -c airvpn -- goldcrest --bluetit-status | grep -i server
kubectl --context epaflix -n airvpn-test exec airvpn-scratch -c airvpn -- sh -c 'curl -s --max-time 10 https://ipinfo.io/json'
kubectl --context epaflix -n airvpn-test exec airvpn-scratch -c airvpn -- ping -c100 -i0.2 -W2 10.128.0.1 | tail -3
kubectl --context epaflix -n airvpn-test exec airvpn-scratch -c airvpn -- sh -c \
  "curl -s -o /dev/null --max-time 45 -w 'down bytes=%{size_download} speed=%{speed_download} B/s\n' 'https://speed.cloudflare.com/__down?bytes=50000000'"
kubectl --context epaflix -n airvpn-test exec airvpn-scratch -c airvpn -- sh -c \
  "head -c 10000000 /dev/zero | curl -s -o /dev/null --max-time 45 -X POST --data-binary @- -H 'Expect:' -w 'up bytes=%{size_upload} speed=%{speed_upload} B/s\n' https://speed.cloudflare.com/__up"
```

Expected: the server is **not** Cygnus, Hassaleh or Kajam; loss near 0%; and throughput in the same order as the bare host measured on 2026-07-30 (~20 MB/s down, ~16 MB/s up), not the ~1.6 MB/s down / 0.03 MB/s up the broken tunnel gave.

- [ ] **Step 7: Exercise the SIGUSR2 path deliberately**

```bash
PID=$(kubectl --context epaflix -n airvpn-test exec airvpn-scratch -c airvpn -- cat /etc/airvpn/bluetit.lock)
kubectl --context epaflix -n airvpn-test exec airvpn-scratch -c airvpn -- goldcrest --bluetit-status | grep -i server
kubectl --context epaflix -n airvpn-test exec airvpn-scratch -c airvpn -- kill -USR2 "$PID"
sleep 25
kubectl --context epaflix -n airvpn-test exec airvpn-scratch -c airvpn -- goldcrest --bluetit-status | grep -i server
```

Expected: it reconnects and reports a connected server again (possibly the same one — quick-mode may re-pick the same recommendation; what matters is that it reconnects rather than dying).

- [ ] **Step 8: Tear down and record results**

```bash
kubectl --context epaflix delete namespace airvpn-test
```

Write the measured numbers into the PR description in Task 7. If anything in Steps 4-7 failed, correct the spec and this plan before continuing.

---

### Task 5: Rewrite the Deployment with the sidecar

**Files:**
- Modify: `2-k3s/08.servarr/qbittorrent/qbittorrent.yaml`
- Modify: `2-k3s/08.servarr/qbittorrent/bluetit-config.yaml` (two directives found necessary in Task 4)
- Modify: `2-k3s/08.servarr/kustomization.yaml` (pin both image digests)

**Interfaces:**
- Consumes: the image from Task 2, ConfigMap `airvpn-bluetit-config` (key `bluetit.conf`) and Secret `airvpn-credentials` (keys `airusername`, `airpassword`) from Task 3.
- Produces: the final Deployment. Task 7 deploys it.

**Everything below is derived from a live scratch-pod run on 2026-07-30, not from theory.** Seven
things in the original design were wrong; each correction is marked WHY so it does not get
"tidied" back to the broken form.

- [ ] **Step 1: Add the two missing directives to the ConfigMap**

In `bluetit-config.yaml`, alongside `airipv6 no`, add:

```
    air6to4                     no
    ignorednspush               yes
```

WHY `ignorednspush`: without it Bluetit throws `Cannot set pushed DNS to system` because kubelet
owns `/etc/resolv.conf`, and that exception **tears down a tunnel that has already connected** —
`goldcrest` then reports "Bluetit is not connected" with no error in the status output. This was
the single subtlest failure found.

- [ ] **Step 2: Swap the app container image**

Replace `tenseiken/qbittorrent-wireguard:latest` with `qbittorrentofficial/qbittorrent-nox`.

WHY: `VPN_ENABLED` **does not exist** in the tenseiken image. Its VPN is mandatory — with no
config it exits 1 on `No WireGuard config file found`, and the live Deployment's `VPN_ENABLED=yes`
has always been a no-op. The author's README: *"I also dropped the option to just not use a VPN...
I highly recommend you make use of the official qBittorrent repo"*.

No `/config` migration is needed. That image's entrypoint uses `profilePath="/config"` and
`/config/qBittorrent/config/qBittorrent.conf`, which is exactly what the live PVC holds, and it
honours the same `PUID`/`PGID` and `qbtUser`.

Set its env to exactly:

```yaml
          env:
            - { name: PUID, value: "568" }
            - { name: PGID, value: "568" }
            - { name: TZ, value: "Europe/Oslo" }
            - { name: QBT_LEGAL_NOTICE, value: "confirm" }
            - { name: QBT_WEBUI_PORT, value: "8080" }
            # Declarative BT port - replaces the old manual "set it in the WebUI" step.
            - { name: QBT_TORRENTING_PORT, value: "39998" }
```

Delete `VPN_ENABLED`, `LAN_NETWORK`, `NAME_SERVERS`, `ADDITIONAL_PORTS`, `HEALTH_CHECK_HOST`,
`HEALTH_CHECK_AMOUNT`, the whole `lifecycle.postStart` block, and the `wireguard-config` and `tun`
volumeMounts.

Replace its `securityContext` with:

```yaml
          # No longer touches the network - the sidecar owns the tunnel and the
          # lock, so this drops privileged and NET_ADMIN.
          securityContext:
            runAsUser: 0
```

- [ ] **Step 3: Stop disabling IPv6 in init-sysctls**

Change its command to:

```yaml
            - |
              sysctl -w net.ipv4.conf.all.rp_filter=2
              sysctl -w net.ipv4.conf.all.src_valid_mark=1
```

WHY: Bluetit unconditionally assigns the AirVPN device's IPv6 address to `tun0`. With
`net.ipv6.conf.all.disable_ipv6=1` the connect thread dies on
`Error: ipv6: IPv6 is disabled on this device` and the tunnel never establishes. `airipv6 no` and
`air6to4 no` do **not** prevent the assignment. Verified: removing the sysctl is what made it
connect.

- [ ] **Step 4: Add the sidecar**

Append to `initContainers`, after `init-sysctls` (order matters — native sidecars start in list
order and the sysctls must be set first):

```yaml
        # Native sidecar (restartPolicy: Always) - starts before the app container
        # and keeps running. Replaces wg-quick inside the old qbittorrent image:
        # Bluetit takes a server whitelist/blacklist and picks an AirVPN-recommended
        # server, which WireGuard alone cannot do.
        - name: airvpn
          image: ghcr.io/spyrospsarras/airvpn-bluetit:latest
          restartPolicy: Always
          env:
            - name: AIRVPN_USERNAME
              valueFrom:
                secretKeyRef:
                  name: airvpn-credentials
                  key: airusername
            - name: AIRVPN_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: airvpn-credentials
                  key: airpassword
          volumeMounts:
            - name: bluetit-config
              mountPath: /config
              readOnly: true
            # REQUIRED. Bluetit checks modules.builtin and loads iptable_* and
            # wireguard itself, even though the node already has them loaded.
            - name: lib-modules
              mountPath: /lib/modules
              readOnly: true
          livenessProbe:
            exec:
              # The connected output contains "Connected to AirVPN server <name>".
              # It does NOT say "Bluetit is connected" - that string never appears.
              # The disconnected form is "Bluetit is not connected", so do not
              # grep for a bare "connected".
              command: ["sh","-c","goldcrest --bluetit-status | grep -q 'Connected to AirVPN server'"]
            initialDelaySeconds: 90
            periodSeconds: 60
            timeoutSeconds: 20
            failureThreshold: 3
          resources:
            requests:
              memory: "64Mi"
              cpu: "50m"
          securityContext:
            runAsUser: 0
            capabilities:
              add:
                # SYS_MODULE is REQUIRED, not defensive. Without it Bluetit fails
                # with "Error while loading kernel module wireguard (-3)" and never
                # connects, even though the module is already loaded on the node.
                - NET_ADMIN
                - SYS_MODULE
```

Note `initialDelaySeconds: 90` — the scratch pod took roughly 20 s from start to
`Connected to AirVPN server`, so 90 s leaves headroom without letting a genuinely dead tunnel sit.

- [ ] **Step 5: Fix the volumes**

Remove `wireguard-config` and `tun`. Add:

```yaml
        - name: bluetit-config
          configMap:
            name: airvpn-bluetit-config
            items:
              - key: bluetit.conf
                path: bluetit.conf
        - name: lib-modules
          hostPath:
            path: /lib/modules
            type: Directory
```

`/dev/net/tun` is confirmed unnecessary — the scratch pod connected without it.

- [ ] **Step 6: Rewrite the header comment**

The current block (lines 1-26) documents the WireGuard/`postStart` design being deleted. Rewrite it
to describe the sidecar, and keep the two facts still true: the live-wins drift policy, and that BT
port 39998 is an account-wide AirVPN forward. Drop the "set it in the WebUI" instruction — Step 2
makes it declarative.

- [ ] **Step 7: Pin both image digests**

In `2-k3s/08.servarr/kustomization.yaml` under `images:`:

```yaml
  # Our own image - see images/airvpn-bluetit/ and the 2026-07-30 design doc.
  - name: ghcr.io/spyrospsarras/airvpn-bluetit
    digest: sha256:<digest printed by the build workflow in Task 2>
  # Official qBittorrent. Replaced tenseiken/qbittorrent-wireguard, which cannot
  # run without a VPN of its own.
  - name: qbittorrentofficial/qbittorrent-nox
    digest: sha256:<current digest>
```

Get the qBittorrent digest with:

```bash
docker buildx imagetools inspect qbittorrentofficial/qbittorrent-nox:latest --format '{{.Manifest.Digest}}'
```

- [ ] **Step 8: Verify the rendered Deployment shape**

```bash
export PATH="$HOME/.local/bin:$PATH"
export SOPS_AGE_KEY=$(kubectl --context epaflix get secret sops-age -n argocd -o jsonpath='{.data}' \
  | python3 -c "import sys,json,base64;[print(base64.b64decode(v).decode(),end='') for v in json.load(sys.stdin).values()]")
kustomize build --enable-alpha-plugins --enable-exec 2-k3s/08.servarr | python3 -c "
import sys,yaml
docs=[d for d in yaml.safe_load_all(sys.stdin) if d]
dep=[d for d in docs if d['kind']=='Deployment' and d['metadata']['name']=='qbittorrent'][0]
spec=dep['spec']['template']['spec']
inits=[c['name'] for c in spec['initContainers']]
assert inits==['init-sysctls','airvpn'], inits
side=[c for c in spec['initContainers'] if c['name']=='airvpn'][0]
assert side['restartPolicy']=='Always'
caps=side['securityContext']['capabilities']['add']
assert set(caps)=={'NET_ADMIN','SYS_MODULE'}, caps
assert any(m['mountPath']=='/lib/modules' for m in side['volumeMounts']), 'lib-modules not mounted'
sysctls=[c for c in spec['initContainers'] if c['name']=='init-sysctls'][0]
assert 'disable_ipv6' not in str(sysctls['command']), 'init-sysctls still disables IPv6'
app=[c for c in spec['containers'] if c['name']=='qbittorrent'][0]
assert 'qbittorrent-nox' in app['image'], app['image']
assert 'lifecycle' not in app, 'postStart hook still present'
assert app['securityContext'].get('privileged') is None, 'app still privileged'
assert 'NET_ADMIN' not in str(app.get('securityContext',{})), 'app still has NET_ADMIN'
env={e['name'] for e in app['env']}
assert 'VPN_ENABLED' not in env and 'HEALTH_CHECK_HOST' not in env, env
assert 'QBT_TORRENTING_PORT' in env
names=[v['name'] for v in spec['volumes']]
assert 'wireguard-config' not in names and 'tun' not in names, names
assert 'bluetit-config' in names and 'lib-modules' in names, names
cm=[d for d in docs if d['kind']=='ConfigMap' and d['metadata']['name']=='airvpn-bluetit-config'][0]
body=cm['data']['bluetit.conf']
for req in ('ignorednspush','air6to4','allowping','tunpersist'):
    assert req in body, req
# The lock must be session-scoped, not connection-scoped, or a sidecar restart
# leaves the app container unprotected. Plain 'networklock' must NOT be present.
assert 'networklockpersist' in body, 'network lock is not session-scoped'
assert not any(l.strip().startswith('networklock ') for l in body.splitlines()), 'plain networklock still set'
print('deployment shape ok')
"
unset SOPS_AGE_KEY
```

Expected: `deployment shape ok`. **CI does not build `08.servarr`** (see Task 3 Step 5), so this
assertion block is the only thing standing between a broken manifest and ArgoCD. Run it.

- [ ] **Step 9: Commit**

```bash
git add 2-k3s/08.servarr/
git -c core.hooksPath=.github/hooks commit -m "feat(servarr): run qbittorrent's VPN through a Bluetit sidecar

Bluetit owns the tunnel and the iptables network lock; the app container moves
to the official qbittorrent-nox image and shares the pod netns. Deletes the
postStart iptables hack and the wrapper health-check env it needed.

The old tenseiken image had to go: VPN_ENABLED does not exist in it, its VPN is
mandatory, and the Deployment's VPN_ENABLED=yes was always a no-op. The official
image needs no /config migration - identical profile layout - and takes
QBT_TORRENTING_PORT so the BT port stops being a manual WebUI step.

Verified on a scratch pod: the sidecar needs SYS_MODULE and /lib/modules because
Bluetit loads kernel modules itself, and init-sysctls must stop disabling IPv6
or the connect thread dies assigning the device's IPv6 to tun0.

Because the app container no longer touches the network it drops privileged and
NET_ADMIN - only the sidecar keeps them."
```

---

### Task 6: Alerts

**Files:**
- Modify: `2-k3s/10.observability/alertmanager-config/loki-log-alerts.yaml`
- Modify: `2-k3s/10.observability/alertmanager-config/custom-alerts.yaml` (the #352 rule, ~lines 190-203)

**Interfaces:**
- Consumes: the log line `airvpn-bluetit: reconnect triggered, loss=<n>%` emitted by Task 1's entrypoint.
- Produces: alerting. Nothing depends on this.

- [ ] **Step 1: Add the reconnect rule**

Follow the existing file's group/rule structure. The rule must match the entrypoint string exactly:

```yaml
        - alert: AirVPNBluetitReconnecting
          expr: |
            sum(count_over_time({namespace="servarr", container="airvpn"}
              |= "reconnect triggered" [30m])) > 0
          for: 5m
          labels:
            severity: warning
          annotations:
            summary: "AirVPN server degrading - Bluetit reconnected repeatedly"
            description: "The Bluetit sidecar forced a reconnect because the tunnel gateway was losing packets. quick-mode should have moved to another recommended server; if this keeps firing the whitelisted pool (nl,de,se) is bad, not one server. Measure the candidate entry-3 IPs from the node before editing airblackserverlist - AirVPN's status API reported health=ok for all three servers that caused this. Recipe: docs/superpowers/specs/2026-07-30-airvpn-bluetit-vpn-layer-design.md"
```

- [ ] **Step 2: Update the stale #352 description**

`custom-alerts.yaml` around line 203 says the WireGuard kill-switch drops traffic and the container re-inits, and points at the `qbittorrent` container's logs. After this change the killswitch is Bluetit's network lock and the VPN logs live in the `airvpn` container. Update the description to point at `{namespace="servarr", container="airvpn"}` and drop the `wg-quick` framing. Leave the `expr` alone — restart-count flapping is still the right signal.

- [ ] **Step 3: Verify both files parse and the expr is well-formed**

```bash
python3 -c "import yaml,sys; [yaml.safe_load(open(f)) for f in sys.argv[1:]]; print('yaml ok')" \
  2-k3s/10.observability/alertmanager-config/loki-log-alerts.yaml \
  2-k3s/10.observability/alertmanager-config/custom-alerts.yaml
grep -c "reconnect triggered" 2-k3s/10.observability/alertmanager-config/loki-log-alerts.yaml
grep -c "reconnect triggered" images/airvpn-bluetit/entrypoint.sh
```

Expected: `yaml ok`, and both greps print `1` — if the alert string and the entrypoint string ever drift, the alert silently never fires.

- [ ] **Step 4: Commit**

```bash
git add 2-k3s/10.observability/alertmanager-config/
git -c core.hooksPath=.github/hooks commit -m "feat(observability): alert on Bluetit reconnect storms

Fires when the sidecar forces more than 2 reconnects in 30m, which means the
whitelisted pool is bad rather than one server. Also corrects the #352 flap
alert, which still described the wg-quick kill-switch and pointed at the wrong
container for VPN logs."
```

---

### Task 7: Deploy, verify, soak

> **Superseded — this is now automated.** The `init-bind-interface` initContainer in
> `qbittorrent.yaml` rewrites `Session\Interface`, `Session\InterfaceName`,
> `Connection\Interface` and `Connection\InterfaceName` to `tun0` on every pod start,
> idempotently, in the only window where `tun0` exists and qBittorrent is stopped.
>
> It had to be automated rather than done by hand: the WebUI's *Network interface*
> dropdown only offers devices that exist at that moment, and it currently lists
> `wg0`/`lo`/`eth0` with no `tun0` — because `wg0` belongs to the image being removed.
> There is no point before the rollout at which a human can select `tun0`.
>
> So the steps below are **verification**, not actions. If the initContainer worked,
> the values already read `tun0`.

**Files:** none. This task is the PR, the deploy and the evidence.

**Blocked by:** Tasks 1-6.

- [ ] **Step 1: Rebase and push**

```bash
cd /home/spy/Documents/Epaflix/k3s-swarm-proxmox-vpn
git fetch origin
git rebase origin/main
git push -u origin airvpn-bluetit-suite --force-with-lease
```

Force-push on a feature branch after a rebase is expected here, but the rule stands: get Spyros's OK before force-pushing.

- [ ] **Step 2: Open the PR with a test plan**

Draft the body, **show it to Spyros, and only post after he confirms.** It must include the Task 4 measurements and an unchecked `## Test plan` matching Steps 4-6 below — every box gets run and the result recorded by editing the PR description, never as a new comment.

- [ ] **Step 3: Merge and let ArgoCD sync**

Wait for `validate` to pass, then **ask Spyros to approve the merge**. After merge:

```bash
kubectl --context epaflix -n argocd get app servarr -o jsonpath='{.status.sync.status} {.status.health.status}{"\n"}'
kubectl --context epaflix -n servarr rollout status deploy/qbittorrent --timeout=300s
```

The Deployment uses `Recreate`, so the old pod goes away before the new one starts — expect a short outage, which is correct given one VPN session at a time.

- [ ] **Step 4: Bind qBittorrent to `tun0` — AUTOMATED - verify only (see note below)**

The live PVC's `/config/qBittorrent/config/qBittorrent.conf` still has `Session\Interface=wg0`,
`Session\InterfaceName=wg0`, `Connection\Interface=wg0` and `Connection\InterfaceName=wg0`. `wg0`
was created by the *old* image's internal `wg-quick` and no longer exists — Bluetit's device is
`tun0`. Left as `wg0`, qBittorrent logs `The configured network interface is invalid. Interface:
"wg0"`, opens no listen socket, and moves zero data — while the WebUI keeps answering, so **both
httpGet probes stay green, the pod stays Ready, and ArgoCD stays Synced/Healthy** the whole time.
That error goes to qBittorrent's own log file, not stdout, so Loki cannot see it either — this is
not a hypothetical, it was reproduced against the exact pinned image digest.

Two ways to fix it, pick one:

- **WebUI** (no downtime): Settings → Connections → Network Interface → select `tun0` → Save.
- **Direct conf edit**: qBittorrent must be **stopped first** — it rewrites the whole conf file on
  exit, so an edit made while it is running gets overwritten. Stop it, change all four keys from
  `wg0` to `tun0`, then start it again.

Do **not** clear the interface field instead of setting `tun0` — binding to the tunnel device is a
second, independent leak guard (see the comment on the app container in `qbittorrent.yaml`): a
socket bound to `tun0` breaks rather than silently falling back to `eth0`.

**Follow-up:** this binding is PVC state, not git, so it does not survive a fresh PVC and nothing
in this repo enforces it. The same shape was already solved twice for other PVC-only servarr
config — issue #137 (newtarr config seed) and #138 (Cleanuparr blocklist) — via a SOPS-seeded
Secret plus a non-clobbering `initContainer`. Worth doing the same here; open as a follow-up issue
per the repo rule rather than doing it inside this task.

- [ ] **Step 5: Run the verification list**

```bash
POD=$(kubectl --context epaflix -n servarr get pod -l app=qbittorrent -o name | head -1)
kubectl --context epaflix -n servarr exec $POD -c airvpn -- goldcrest --bluetit-status
kubectl --context epaflix -n servarr exec $POD -c airvpn -- sh -c 'curl -s --max-time 10 https://ipinfo.io/json'
kubectl --context epaflix -n servarr exec $POD -c airvpn -- ping -c100 -i0.2 -W2 10.128.0.1 | tail -3
kubectl --context epaflix -n servarr exec $POD -c airvpn -- sh -c "curl -s -o /dev/null --max-time 45 -w 'down speed=%{speed_download} B/s\n' 'https://speed.cloudflare.com/__down?bytes=50000000'"
kubectl --context epaflix -n servarr exec $POD -c airvpn -- sh -c "head -c 10000000 /dev/zero | curl -s -o /dev/null --max-time 45 -X POST --data-binary @- -H 'Expect:' -w 'up speed=%{speed_upload} B/s\n' https://speed.cloudflare.com/__up"
kubectl --context epaflix -n servarr exec $POD -c qbittorrent -- nslookup sonarr.servarr.svc.cluster.local 192.168.10.30
curl -s -o /dev/null -w "webui http=%{http_code}\n" http://qbittorrent.epaflix.com/

# --- The two checks below are the ones that actually catch the wg0/tun0 bug.
# A green WebUI curl and a green ArgoCD Synced/Healthy prove NOTHING about the
# download client here — both only touch/observe the local WebUI HTTP server,
# which answers fine whether or not qBittorrent could bind its BT socket.

# 1. qBittorrent's own log must show a successful listen on tun0, not the
#    "configured network interface is invalid" error. Check via the WebUI's
#    own Log viewer (Execution Log, filter Normal+Info), or if shelling in:
kubectl --context epaflix -n servarr exec $POD -c qbittorrent -- sh -c \
  'grep -i "network interface" /config/qBittorrent/data/logs/*.log 2>/dev/null | tail -5 || echo "check the WebUI Log viewer instead"'

# 2. Torrents must genuinely transfer data - not just answer HTTP. Confirm
#    dl_info_data/up_info_data actually climb across a wait, with an active
#    torrent present (a flat counter is exactly the wg0-bug symptom):
kubectl --context epaflix -n servarr exec $POD -c qbittorrent -- sh -c 'curl -s http://localhost:8080/api/v2/transfer/info'
# ... wait 5-10 minutes with a torrent downloading/seeding, then re-run and
# diff the two outputs. Also confirm connection status is "Connected"/"Firewalled"
# and NOT "Disconnected" in the WebUI status bar.
```

Expected: connected to a server outside the blacklist; AirVPN exit IP; loss near 0%; throughput in
the order of the bare host; DNS resolves; WebUI answers; qBittorrent's log shows a clean listen on
`tun0` with no interface error; transfer counters actually increase over time.

- [ ] **Step 6: Verify the port forward**

`QBT_TORRENTING_PORT` already passes `--torrenting-port=39998` to the app container declaratively —
this is no longer a manual WebUI step, so there is nothing to "set … if it did not survive". Just
confirm it took: check qBit's Settings → Connections shows `39998`, and check reachability from
outside the tunnel. The AirVPN forward is account-wide so it follows the chosen server, but verify
rather than assume.

- [ ] **Step 7: Soak 24h, then record**

```bash
kubectl --context epaflix -n servarr get pod -l app=qbittorrent -o jsonpath='{.items[0].status.containerStatuses[*].restartCount} {.items[0].status.initContainerStatuses[*].restartCount}{"\n"}'
kubectl --context epaflix -n servarr logs $POD -c airvpn --since=24h | grep -cE "reconnect triggered|strike"
```

Expected: restart counts flat (the baseline being 59 restarts in 12 days), and few or no reconnects. Tick the PR test-plan boxes by editing the description.

---

### Task 8: Nick's VM

> **Corrected from the Task 4 scratch-pod findings** — this task was written before Task 4 ran and
> still carried three settings the scratch pod proved fatal (see Task 5's "Everything below is
> derived from a live scratch-pod run" note). Fixed below so executing this task does not
> reproduce already-diagnosed bugs.

**Files:**
- Create: `1-proxmox/user-vms/nick/docker-compose.yml`
- Create: `1-proxmox/user-vms/nick/README.md`

**Blocked by:** Task 7's soak passing.

- [ ] **Step 1: Capture the current state before changing anything**

```bash
ssh ubuntu@192.168.10.41 'sudo -n docker ps --format "{{.Names}}\t{{.Image}}"'
ssh ubuntu@192.168.10.41 'sudo -n docker inspect qbittorrent --format "{{json .Mounts}}"' | python3 -m json.tool
ssh ubuntu@192.168.10.41 'sudo -n docker inspect qbittorrent --format "{{range .Config.Env}}{{println .}}{{end}}"'
```

Note `ubuntu` is not in the `docker` group there, so every docker command needs `sudo -n`. Record the volume paths and env — the compose file must reproduce them exactly or his config is lost.

- [ ] **Step 2: Write the compose file**

Reproduce the mounts and env from Step 1. Shape:

```yaml
services:
  airvpn:
    image: ghcr.io/spyrospsarras/airvpn-bluetit:latest
    container_name: airvpn
    # SYS_MODULE is REQUIRED, not defensive - same as the k8s sidecar. Without
    # it Bluetit fails with "Error while loading kernel module wireguard (-3)"
    # and never connects, even though the module is already loaded on the
    # host. The /lib/modules mount is required for the same reason.
    cap_add: ["NET_ADMIN", "SYS_MODULE"]
    volumes:
      - ./bluetit.conf:/config/bluetit.conf:ro
      - /lib/modules:/lib/modules:ro
    # No IPv6-disabling sysctl here - Bluetit unconditionally assigns the
    # AirVPN device's IPv6 address to tun0, and with IPv6 disabled the connect
    # thread dies on "IPv6 is disabled on this device" (same failure verified
    # on the k3s scratch pod in Task 4 / qbittorrent.yaml's init-sysctls).
    environment:
      AIRVPN_USERNAME: ${AIRVPN_USERNAME:?set in .env}
      AIRVPN_PASSWORD: ${AIRVPN_PASSWORD:?set in .env}
    ports:
      # Published here, not on qbittorrent, because qbittorrent shares this
      # container's network namespace.
      - "8080:8080"
      - "39998:39998/tcp"
      - "39998:39998/udp"
    restart: unless-stopped

  qbittorrent:
    # Must be the official image, matching the Kubernetes side - NOT
    # tenseiken/qbittorrent-wireguard. VPN_ENABLED does not exist in that
    # image, its VPN is mandatory, and with no WireGuard config present it
    # hard-exits 1 on "No WireGuard config file found" - there is no way to
    # run it VPN-less. See qbittorrent.yaml / Task 5 Step 2 for the same fix
    # on the cluster side.
    image: qbittorrentofficial/qbittorrent-nox
    container_name: qbittorrent
    network_mode: "service:airvpn"
    depends_on: [airvpn]
    environment:
      # Declarative BT port, matching QBT_TORRENTING_PORT on the k8s side -
      # no manual WebUI step needed for this part.
      QBT_TORRENTING_PORT: "39998"
      # remaining env copied verbatim from Step 1
    volumes:
      # copied verbatim from Step 1
    restart: unless-stopped
```

`bluetit.conf` next to it is the ConfigMap body from Task 3 with one change: `airkey nick`.

`cloudflared` stays outside the VPN and is not part of this compose.

- [ ] **Step 2b: Bind qBittorrent to `tun0` — same trap as the cluster side**

Nick's config faces the identical failure mode as plan Task 7 Step 4: his existing
`qBittorrent.conf` almost certainly still has `Session\Interface`/`Connection\Interface` pointed at
whatever device his previous setup created, not `tun0`. Left wrong, qBittorrent opens no listen
socket and moves no data while the WebUI keeps answering. Fix it the same way: either WebUI
Settings → Connections → Network Interface → `tun0`, or stop qBittorrent, edit all four
`*Interface`/`*InterfaceName` keys in the conf file to `tun0` by hand (it must be stopped first —
it rewrites the file on exit), then start it again.

- [ ] **Step 3: Write the README**

Cover: `sudo -n docker` (the `ubuntu` user is not in the docker group), that the SSH host key is not in `known_hosts` so the first connection needs `-o StrictHostKeyChecking=accept-new`, that credentials come from a git-ignored `.env` sourced from `secrets.yml`, and that `airkey` must stay `nick` so his device key is never shared with the cluster.

- [ ] **Step 4: Deploy and verify**

```bash
ssh ubuntu@192.168.10.41 'cd /opt/qbittorrent && sudo -n docker compose up -d'
ssh ubuntu@192.168.10.41 'sudo -n docker exec airvpn goldcrest --bluetit-status'
ssh ubuntu@192.168.10.41 'sudo -n docker exec airvpn ping -c100 -i0.2 -W2 10.128.0.1 | tail -3'
ssh ubuntu@192.168.10.41 "sudo -n docker exec airvpn sh -c \"head -c 10000000 /dev/zero | curl -s -o /dev/null --max-time 45 -X POST --data-binary @- -H 'Expect:' -w 'up speed=%{speed_upload} B/s\n' https://speed.cloudflare.com/__up\""
```

Expected: connected outside the blacklist, loss near 0%, and upload far above the 0.03 MB/s measured on 2026-07-30. Compare against the bare host (~16 MB/s) to prove the tunnel is no longer the bottleneck.

- [ ] **Step 5: Commit**

```bash
git add 1-proxmox/user-vms/nick/
git -c core.hooksPath=.github/hooks commit -m "feat(user-vms): bring Nick's qbittorrent stack into git

Same Bluetit sidecar as the cluster, with airkey nick so the device keys stay
separate. His stack was previously undocumented; the compose reproduces the
mounts and env captured from the running containers. cloudflared stays outside
the VPN."
```

---

### Task 9: Cleanup and follow-ups

**Blocked by:** Tasks 7 and 8 soaking clean.

- [ ] **Step 1: Remove the superseded WireGuard Secret and template**

Only now that rollback is no longer wanted:

```bash
git rm 2-k3s/08.servarr/_shared/secrets/qbittorrent-wireguard.enc.yaml
git rm 2-k3s/08.servarr/_shared/secrets/wireguard-secret.yaml
```

Drop the matching line from `ksops-generator.yaml` and the comment block in `kustomization.yaml`. Leave `qbittorrent-openvpn.enc.yaml` alone — it is unrelated to this change.

- [ ] **Step 2: Verify the render still works and nothing references the removed Secret**

```bash
grep -rn "qbittorrent-wireguard" --include="*.yaml" . | grep -v "^./.git/"
kustomize build 2-k3s/08.servarr >/dev/null && echo "render ok"
```

Expected: no remaining references, `render ok`.

- [ ] **Step 3: Open the follow-up issues**

Per the repo rule, every deferred item gets a `gh issue` on `SpyrosPsarras/epaflix` before the thread closes. Draft each body and **show Spyros before posting** — issues are external text. Use the existing `## Finding` / `## Current state` / `## Desired outcome` / `## Notes` shape and cross-link.

1. **Delete or land the parked `airvpn-server-swap` branch.** It pins Dedalus as an interim fix and is superseded by this work.
2. **`postgres-secret.yaml` is a plaintext placeholder that is not on the pre-commit allowlist**, so any edit to it is blocked the same way `wireguard-secret.yaml` was. Either allowlist it or convert it.
3. **AirVPN account password now sits in a cluster Secret.** Track enabling 2FA on the account and confirming Bluetit still authenticates with it, plus a rotation runbook. Cross-link the secret-rotation pod-reload gap (#299).
4. **`tenseiken/qbittorrent-wireguard:latest` is unpinned** and now used only as a plain qBittorrent build. Consider moving to a stock image with a digest pin, noting the `/config` migration.

- [ ] **Step 4: Commit**

```bash
git add -A
git -c core.hooksPath=.github/hooks commit -m "chore(servarr): drop the superseded WireGuard Secret and template

qBittorrent's VPN is Bluetit-managed now and has soaked clean, so the pinned
single-endpoint Secret and its placeholder template are no longer a rollback
target."
```

---

## Self-Review

**Spec coverage:** Purpose → Tasks 1-8. Image → Task 1. Build/GHCR/Renovate → Task 2. Config split (ConfigMap + SOPS Secret) → Task 3. The two "verify, do not assert" assumptions → Task 4. Deployment/sidecar/dropping privileged → Task 5. Degradation handling → entrypoint in Task 1 plus the alert in Task 6. Verification list → Task 4 (scratch) and Task 7 (live). Rollback → Task 5 keeps the old Secret, Task 9 removes it only after soak. Nick's VM → Task 8. Out-of-scope items → Task 9 follow-up issues.

**Gap found and closed:** the spec named a Loki alert but no task owned the *existing* #352 alert, whose description documents the mechanism being deleted. Added as Task 6 Step 2.

**Second gap found and closed:** connecting a scratch pod would have kicked the live tunnel, because both would use the `Default` device key. Added the manual `k3s-test` key as a blocking prerequisite.

**Placeholder scan:** no TBDs. Every code step carries the actual file content. Where a value genuinely cannot be known in advance — the GHCR digest in Task 5 Step 5 and the connected-state string in Task 5 Step 2 — the plan says exactly which earlier step produces it.

**Type/name consistency:** `airvpn-bluetit-config` / key `bluetit.conf`, Secret `airvpn-credentials` / keys `airusername`+`airpassword`, sidecar container name `airvpn`, and the log string `reconnect triggered` are used identically in Tasks 1, 3, 5, 6 and 8. Task 6 Step 3 asserts the log string matches the entrypoint, since a silent drift there disables the alert.
