# Odysseus Bastion Sandbox — Design

**Date:** 2026-06-14
**Status:** Approved (design); implementation plan to follow
**Owner:** spy

## Problem

Odysseus (k3s AI workspace app) has an in-app shell/exec tool, but it runs inside
the Odysseus pod's own filesystem/namespace. We want a dedicated **execution
sandbox** so that when Odysseus is asked to *do* something (build a site, run a
script, start a server), it does it on a separate box — not in its own pod — and
the result is reachable on the LAN.

Concrete target use case: ask Odysseus to create an HTML page → it writes the
file to a shared folder → starts a small webserver on the bastion → the page is
reachable at `http://bastion.epaflix.com:<port>`.

Odysseus must also "know about our setup": the entire contents of this project
(`SpyrosPsarras/epaflix`) — **including git-ignored files** such as
`secrets.yml` — must be available to it.

## Decisions (locked during brainstorming)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Execution model | **B** — Odysseus SSHes from its pod into the bastion and runs commands there (real sandbox) |
| 2 | Repo/secrets scope | **B** — full committed repo always available; `secrets.yml` + homelab SSH keys present, but on the bastion+NFS only (detachable, not in the pod baseline) |
| 3 | SSH keys bundled | **Homelab-relevant only** (k3s nodes, takaros/evanthoulaki, truenas, pihole, jumpbox) — NOT work keys (`davidhorn`, `ft4`, `dh-demo`) |
| 4 | Bastion VM | name `odysseus-bastion`, **VMID 1043 → 192.168.10.43**, host `evanthoulaki`, Ubuntu 24.04, 4 vCPU / 8 GB / 60 GB |
| 5 | IP / subnet | Flat `192.168.10.0/24` (reachable by whole LAN with zero router/VLAN work; `.20.x` rejected — new unrouted subnet, TP-Link Archer can't route a second LAN) |
| 6 | Storage pool | TrueNAS **`apps`** (redundant RAIDZ1) |
| 7 | Share layout | **One NFS share** = a working copy of the project; `secrets.yml` sits in its normal path (`.github/instructions/secrets.yml`); output in `work/` |
| 8 | Web access | **Pi-hole `bastion.epaflix.com → 192.168.10.43`**; **no persistent server** on the bastion — Odysseus starts its own servers on demand |
| 9 | Workstation access | spy's workstation can `ssh bastion` directly (pubkey in bastion `authorized_keys` + `~/.ssh/config` alias) |
| 10 | Instruction durability | "Always work on the bastion" seeded into Odysseus persistent config (GitOps) + runtime memory/doc |

## Architecture

```
You ──http──> bastion.epaflix.com:<port>   (Pi-hole A → 192.168.10.43)
                          ▲
                          │ Odysseus starts servers here (no always-on server)
[Odysseus pod] ──SSH──> [odysseus-bastion VM 1043 / .43]   (evanthoulaki, Ubuntu 24.04)
       │ (ssh bastion)            │
       │  both NFS-mount the same share
       ▼                          ▼
        TrueNAS  apps/odysseus-bastion   (/mnt/apps/odysseus-bastion)
          repo/   clone of SpyrosPsarras/epaflix + git-ignored secrets.yml in place
          work/   Odysseus's output — HTML, builds, served from here
```

Two channels, distinct jobs:
- **NFS** — shared files. Odysseus (pod side) reads the project / writes output
  directly; the bastion sees the same tree.
- **SSH** — execution. Builds, package installs, starting servers run on the
  bastion.

Verified feasibility: the Odysseus container is Debian-based and already ships
`ssh`, `git`, `apt-get`. (It runs as uid 0 / root — its shell tool executes as
root in the pod; consistent with the accepted-risk posture.)

## Components

### Bastion VM (`odysseus-bastion`, 1043 / .43)
- Provisioned with the existing user-VM flow (Ubuntu 24.04 cloud-init template,
  evanthoulaki, `local-raid` disk). VMID/IP mirror convention: 1043 → .43.
- NFS-mounts the share at `/workspace` (fstab).
- Holds the homelab SSH keys + `~/.ssh/config` in `ubuntu`'s home → can reach
  takaros, evanthoulaki, truenas, pihole, all k3s nodes, jumpbox by alias.
- `authorized_keys` contains: (a) the pod's dedicated `id_bastion` pubkey,
  (b) spy's workstation pubkey (`id_ed25519`).

### Shared NFS storage
- TrueNAS dataset `apps/odysseus-bastion` → `/mnt/apps/odysseus-bastion`, owned
  `1000:1000`.
- Single NFS export, `maproot`/`mapall` → `1000:1000` so pod (root, PUID 1000)
  and bastion (`ubuntu`, 1000) agree on ownership.
- Export access restricted to **k3s worker IPs + the bastion `.43`** — NOT the
  whole LAN.
- Contents: `repo/` (git clone of the project + git-ignored `secrets.yml` placed
  at `repo/.github/instructions/secrets.yml`), `work/` (Odysseus output).

### Odysseus pod changes (`2-k3s/13.odysseus/`)
- NFS PV + PVC; mount the share at `/workspace`.
- Dedicated `id_bastion` private key added to `odysseus-secrets.enc.yaml` (SOPS),
  mounted read-only at `/app/.ssh/id_bastion`, plus a pod `~/.ssh/config` with a
  `bastion` alias (`HostName 192.168.10.43`, `User ubuntu`, `IdentityFile
  /app/.ssh/id_bastion`, `IdentitiesOnly yes`).
- Seeded `settings.json` (in `odysseus-data-seed.enc.yaml`) carries the durable
  "work on the bastion" instruction.

### SSH trust model (two hops, deliberate isolation)
- **Pod → bastion:** a *dedicated* keypair. Private key SOPS-encrypted + mounted
  into the pod; pubkey in `ubuntu@.43:authorized_keys`. The pod key opens **only
  the bastion**.
- **Bastion → everything:** the broad homelab keys live on the VM (a box that
  can be stopped/snapshot/rebuilt), not in the pod.

### DNS
- Pi-hole `bastion.epaflix.com → 192.168.10.43` via `/etc/dnsmasq.d/` (golden
  rule: dnsmasq.d files only; full `pihole-FTL` restart for new names).

### Hardening
- Inbound SSH to `.43` limited to the LAN; `fail2ban` (mirrors the jumpbox).
- VM snapshot for recoverability.
- Egress intentionally **not** locked down — the bastion needs broad outbound
  (apt/npm/git + reaching infra). Optional isolated-subnet hardening deferred.

## Data / control flow — the HTML example

1. User: "Odysseus, build me a landing page."
2. Odysseus writes `index.html` into `/workspace/work/` (pod-side NFS write) — or
   over `ssh bastion` into `/workspace/work/`.
3. Odysseus runs over `ssh bastion`: start a server in `/workspace/work`
   (e.g. `python3 -m http.server <port>` or a built framework server).
4. User opens `http://bastion.epaflix.com:<port>` — served from the bastion.

## Security / blast radius (explicit)

- Anyone who can prompt Odysseus (Authentik-gated, trusted circle) can
  `ssh bastion` and from there reach everything `secrets.yml` + the homelab keys
  unlock — Proxmox root, TrueNAS, all nodes. This is the accepted consequence of
  scope B + bundling the SSH config.
- Mitigations: secrets/keys live on bastion+NFS (not the pod baseline); NFS
  export restricted to k3s workers + `.43`; bastion is a separate
  stop/snapshot/rebuild-able VM; the pod's own key reaches only the bastion; the
  only secret entering git is `id_bastion`, SOPS-encrypted only.

## What is GitOps vs out-of-band

**Committed (PR, per merge policy):**
- `1-proxmox/user-vms/` — `odysseus-bastion-ssh-config` + provisioning README entry (1043 / .43).
- `0-truenas/` — `apps/odysseus-bastion` dataset + NFS export documentation.
- `2-k3s/13.odysseus/` — NFS PV/PVC + `/workspace` mount; `id_bastion` in
  `odysseus-secrets.enc.yaml`; pod `~/.ssh/config`; bastion instruction in
  `odysseus-data-seed.enc.yaml`.
- Pi-hole — `bastion.epaflix.com` line documented in dnsmasq config.

**Out-of-band (operational; log to `.history/`):**
- Create VM (qm/cloud-init), TrueNAS dataset/export (`midclt`), seed workspace
  (`git clone` + drop `secrets.yml` + install homelab keys into `ubuntu@.43`),
  live Pi-hole record, pod pubkey into bastion `authorized_keys`, spy workstation
  pubkey into bastion `authorized_keys`.
- Set Odysseus runtime memory/doc via the scoped API.

## Follow-up issues (per repo rule)

- Optional isolated-subnet hardening for the bastion (the `.20.x` idea done
  properly: VLAN + router routing).
- Reinforce/automate the Odysseus "use the bastion" instruction if it drifts at
  runtime (beyond the seeded config).

## Out of scope

- GPU / model serving on the bastion (Ollama stays on TrueNAS).
- Public internet exposure of the bastion (LAN-only `bastion.epaflix.com`).
- Replacing Odysseus's built-in shell tool.
