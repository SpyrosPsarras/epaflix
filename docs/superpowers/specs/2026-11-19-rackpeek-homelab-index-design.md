# Homelab index in RackPeek: design

Date: 2026-11-19. Status: draft for owner approval.

## Problem

Nobody can see at a glance what runs where, where its config lives, and whether that config is in Git. Today the answer comes from asking an agent, which rescans by hand. The owner wants one admin-only web page that answers it for every host, VM, LXC, k3s app and object, disk and agent pod, kept current without manual work.

## Decisions

- D1. Each RackPeek item has two parts. Facts (labels) are rewritten by the nightly sync. The explanation (notes) is written by an agent or a person, and the sync carries it over unchanged.
- D2. Host scans run over SSH with one key that can only run one read-only scan script. The key is limited with `restrict`, `command=` and `from=` (the k3s node addresses). One key instead of one per host: all keys would sit in the same Kubernetes Secret, so separate keys add no protection.
- D3. No secret values leave a host or the cluster. See [Secrets](#secrets).
- D4. Only the hosts in the [allowlist](#host-allowlist) are scanned. The namespaces and paths in [Boundaries](#boundaries) are never read.
- D5. RackPeek saves its own config to a private repo on a self-hosted Forgejo with its built-in Git sync.
- D6. Forgejo uses the existing CloudNativePG cluster `postgres-system/postgres-cluster`, not SQLite.
- D7. Forgejo's website is admin-only behind Authentik. RackPeek and the sync reach it on the cluster-internal service.
- D8. Owner steps (S5): the bot user and its two tokens. Proxmox and TrueNAS are read by the root scan with their local tools (`pvesh`, `midclt`), so they need no API token.

## Components

```
Authentik (admins only)
   │ forward-auth
Traefik ──► rackpeek.epaflix.com ──► RackPeek (2-k3s/37.rackpeek)
       └──► git.epaflix.com      ──► Forgejo  (2-k3s/38.forgejo) ──► postgres-cluster
                                        ▲  ▲
RackPeek Git sync (repo rackpeek-config)┘  │
rackpeek-sync CronJob (repo homelab-live) ──┘
   ├─ reads Kubernetes API (per-namespace RoleBindings)
   ├─ ssh to allowlisted hosts → forced command rackpeek-scan
   ├─ reads the public epaflix repo for Git status
   └─ POST /api/inventory (Replace for sync items) to RackPeek
```

### Forgejo (PR 1, `2-k3s/38.forgejo`)

- Image `codeberg.org/forgejo/forgejo:17.0.2-rootless`, digest-pinned in `kustomization.yaml` for Renovate. `strategy: Recreate`, because the iSCSI volume attaches to one pod only.
- Database: `DatabaseRole` and `Database` in `postgres-system`, same pattern as `2-k3s/36.home-assistant/database.yaml`. Role password in a SOPS Secret through ksops. The Argo Application copies home-assistant's `ignoreDifferences`.
- Repos and attachments on a `truenas-iscsi` PVC (daily TrueNAS snapshots, 7 days). Postgres holds users, tokens and the repo list only.
- Config through env vars, so nothing depends on a generated `app.ini`:
  - `FORGEJO__database__DB_TYPE=postgres`, `HOST=postgres-cluster-rw.postgres-system.svc:5432`, `NAME`, `USER`, `PASSWD` (from SOPS), `SSL_MODE=disable`
  - `FORGEJO__security__INSTALL_LOCK=true`, `SECRET_KEY` and `INTERNAL_TOKEN` from SOPS, generated inside the command that encrypts them
  - `FORGEJO__server__DISABLE_SSH=true`, `ROOT_URL=https://git.epaflix.com`, `FORGEJO__service__DISABLE_REGISTRATION=false` with `ALLOW_ONLY_EXTERNAL_REGISTRATION=true` (OIDC accounts only), no mailer
- Ingress: IngressRoute modeled on `2-k3s/27.speedtest/ingress.yaml`. It uses `security-headers` and `authentik-forwardauth`, has the outpost path at priority 15, and redirects http.
- Authentik blueprint (`2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml`): proxy provider `forward_single`, application, binding to `authentik Admins`, outpost membership.
- Login: Authentik only, in two layers. Traefik forward auth gates the site to `authentik Admins`. Forgejo then signs in through the Authentik OIDC application `forgejo-sso` (client secret in the blueprint and in `forgejo-secrets`). New accounts take the part of the email before `@` as username, because Authentik usernames can be emails and Forgejo rejects `@`. The `forgejo-role` scope sends `forgejo: admin` for `authentik Admins`; Forgejo requires that claim and grants admin from it. Password and basic-auth login are off, so the bot uses tokens. The header login of D9 was replaced on 2026-11-23: it failed on the email username and let any pod in namespace `rackpeek` act as any user. Not exposed to the internet; LAN and Tailscale only.
- Network policy: ingress from Traefik and from namespace `rackpeek` on the HTTP port. Egress to Postgres, DNS and Traefik on 443 for Authentik OIDC.
- DNS: `git.epaflix.com` in `1-proxmox/pihole/dnsmasq.d/10-epaflix.conf` and a local-zone in `1-proxmox/pihole/unbound-no-aaaa-leak.conf`. Pi-hole is not managed by Argo. After merge, the files are copied to the Pi-hole container and it restarts, as described in `1-proxmox/pihole/README.md`. That is a live change and needs the owner's OK.
- Owner steps after deploy, from the owner's terminal with the `forgejo admin user` CLI: add the Authentik login source with `forgejo admin auth add-oauth`, the bot user `rackpeek-bot`, and two `write:repository` tokens. The bot's first push creates the private repos `rackpeek-config` and `homelab-live` (`ENABLE_PUSH_CREATE_USER`). A hidden-input script writes the tokens to SOPS.

### RackPeek (PR 2, `2-k3s/37.rackpeek`)

- Image `aptacode/rackpeek:v2.3.0`, digest-pinned for Renovate. Config on a `truenas-iscsi` PVC at `/app/config`, with `fsGroup: 1654` (the image user) so Git can write there and `strategy: Recreate`.
- `RPK_API_KEY` and, once the bot token exists, `GIT_TOKEN` (optional key) from SOPS Secret `rackpeek-secrets`; `GIT_USERNAME=rackpeek-bot` as plain env. RackPeek turns Git sync on only when `GIT_TOKEN` is set at startup. RackPeek accepts only HTTPS remotes, so the remote is `https://git-internal.epaflix.com/rackpeek-bot/rackpeek-config.git`: Traefik's LAN-only internal listener routes only Git smart-HTTP paths to Forgejo, without forward auth. RackPeek fetches as soon as a remote is added, so the repo is created empty through the API first; RackPeek then pushes `main`. An init container puts `.dataprotection/` in `/app/config/.gitignore` so the ASP.NET key files never reach Git.
- Ingress, middlewares, Authentik blueprint and DNS as for Forgejo, at `rackpeek.epaflix.com`. RackPeek has no login of its own, so Authentik is the only gate for people.
- `/api` and `/mcp` need `X-Api-Key`. From outside they also sit behind forward-auth. In-cluster callers use the Service directly.
- Network policy: ingress from Traefik, from the sync pod and from namespace `t3code-homelab` (the agent writes notes in PR 4). Egress to Forgejo and DNS.
- Argo app registered in `2-k3s/11.argocd/apps/` and its `kustomization.yaml`.

### Nightly sync (PR 3)

- Image `ghcr.io/spyrospsarras/rackpeek-sync`, built from `images/rackpeek-sync/` by a workflow copied from `build-vpn-picker.yml`. Contents: Python 3 standard library, `openssh-client`, `git`, `gitleaks`.
- CronJob in namespace `rackpeek`, 03:30 Europe/Athens, `concurrencyPolicy: Forbid`.
- Network policy egress: Kubernetes API on 6443 to the masters (same pattern as the `reloader` policy in `2-k3s/36.home-assistant/network-policy.yaml`), SSH to allowlisted hosts, HTTPS to GitHub, Forgejo, RackPeek, DNS.
- Sources:
  1. Kubernetes API. Argo `Application` objects give app → Git path → managed resources and sync status. It also reads workloads, CronJobs, Jobs, ConfigMaps, Services, IngressRoutes, NetworkPolicies, PVCs, PVs, StorageClasses and nodes, but not Secrets.
  2. SSH scan of each allowlisted host (below). On one Proxmox host it also reads the cluster with `pvesh`: nodes, guests and their config, storage. On TrueNAS it reads pools, datasets, zvols, shares and apps with `midclt`.
  3. A shallow clone of the public epaflix repo for Git status and Renovate rules.
- Writes:
  - RackPeek: one `POST /api/inventory` with `mode: Replace` for sync-owned items. Before posting, the sync reads `GET /api/inventory` and copies each item's `notes` and its tags other than `sync` into the payload. That is how D1 keeps the explanations. `Merge` cannot be used: it merges labels key by key, so stale facts would never go away.
  - Forgejo `homelab-live`: masked content of every scanned host file and every ConfigMap, plus package lists, one commit per run. RackPeek labels link to the file there.
- Gone items: an item tagged `sync` that a successful source no longer reports gets the label `gone-since: <date>` and stays until a person deletes it. Items from a failed source keep their last facts.
- Failure: any failed source makes the Job exit non-zero, so the existing `KubeJobFailedLastRun` alert fires. The pod log has counts and per-source errors.

### Host scan (PR 3)

- Script `1-proxmox/rackpeek-scan/rackpeek-scan.sh` in Git. It ignores `SSH_ORIGINAL_COMMAND` and takes no input.
- Install, once per host, by the agent after the owner confirms. This is a live change on about 20 hosts:
  - copy the script to `/usr/local/sbin/rackpeek-scan`
  - add one `authorized_keys` line for the host's existing SSH inventory user: `restrict,from="<k3s node addresses>",command="sudo -n /usr/local/sbin/rackpeek-scan"`
  - for non-root users (the k3s VMs log in as `ubuntu`), add a sudoers line that allows only that script
- Output: one JSON document on stdout.
  - Hardware: `dmidecode` system, CPU and RAM, `lsblk -J`, `smartctl -i`, NICs.
  - Storage, on Proxmox hosts: `pvs`, `lvs`, `zpool status -P`, iSCSI targets.
  - OS: release, kernel, uptime, package list.
  - Custom files: modified conffiles (`dpkg --verify`), files under `/etc` that no package owns, `/usr/local`, non-hidden files in `/root`, crontabs, systemd units under `/etc/systemd`, and `/etc/pve` on Proxmox.
  - TrueNAS: the key goes on `truenas_admin` through the TrueNAS user settings, because the middleware rewrites `authorized_keys`. To verify in PR 3: that TrueNAS keeps the `command=` options. If it does not, TrueNAS falls back to a read-only API key made by the owner.
  - For each file: path, size, mtime and the sha256 of its raw content. Masked content is added for text files under 64 KiB that are not on the name-only list.
- Excluded: `/var/lib` (k3s local-path storage and kubelet pod dirs included), and every node path that holds volumes of excluded namespaces.

### Host allowlist

takaros, evanthoulaki, patroklos, shypowder, pegaprox, pihole, pbs, jump-box, nick, k3s-master-51 to 53, k3s-worker-61 to 65, truenas.

Not scanned: the `t3code` SSH alias (the legacy pod), homepc, laptop, `laptop-ts`, vidar's insides. Vidar still appears as a guest from the Proxmox API.

### Git status

- Host file: "in Git: <path>" when its raw sha256 matches a file in the repo clone, otherwise "not in Git". Limitation: a file that differs from its Git copy shows as "not in Git", not as drift. Drift reporting is out of scope.
- Kubernetes object: managed when an Argo Application lists it, otherwise "live only".
- Agent pods: `instructions.md`, `tools/package.json` and the StatefulSet come from Git. The live `AGENTS.md`, skills and MCP servers are compared only when the homelab agent rescans, because it reads its own files. The sync gets no exec rights into agent pods: that would let it borrow the agent's cluster-admin kubeconfig.

## Data model

RackPeek tells items apart by name only, ignoring upper/lower case. Every name gets a prefix so no two items can collide.

| RackPeek kind / type | Name | Homelab item | runsOn |
|---|---|---|---|
| Server / Desktop | `hw/takaros` | takaros, evanthoulaki, patroklos, shypowder | none |
| System `hypervisor` | `pve/takaros` | Proxmox on each host | its hardware |
| System `vm` / `container` | `vm/truenas`, `lxc/pihole` | each VM and LXC (TrueNAS, nick, vidar, PBS, pihole, pegaprox, jump-box, k3s nodes) | its Proxmox system |
| System `cluster` | `k3s/epaflix` | k3s | the 8 node VMs |
| System `other`, label `kind: argo-app` | `app/home-assistant` | each Argo app, and each namespace with no app | k3s |
| Service | `home-assistant/deploy/home-assistant` | each Deployment, StatefulSet, DaemonSet, CronJob | its app |
| Service | `truenas/app/<name>` | each TrueNAS app | the TrueNAS VM |

Services that have no Kubernetes Service (CronJobs, workers) get no `network` block. The server accepts that, but `config.yaml` will then fail strict schema validation. That is accepted because nothing validates it.

Labels per workload Service:
- `nodes`, `images`
- `updates`: the Renovate rule, CI build or manual, read from `.github/renovate.json` and the workflows
- `configmaps`: links into `homelab-live`
- `secrets`: names and key names
- `ingress`, `auth` (the Authentik group)
- `uses`: from env, ConfigMaps and NetworkPolicies
- `git`
- `disk-chain`: PVC → PV → TrueNAS zvol or local-path dir → VM disk → Proxmox storage → physical drive

RackPeek has no service-to-service link type, so "uses" lives in labels and is found with search.

## Secrets

- Masking happens on the host, before anything leaves it:
  1. Name-only list. These send name and size only, with no content and no hash: `/etc/ssh`, `/etc/pve/priv`, `/etc/rancher`, `/etc/proxmox-backup`, `/etc/pihole`, `/etc/wireguard`, `/etc/ssl/private`, `/etc/shadow*`, `*.key`, `*.pem`, `*.pw`, `id_*`, and all dotfiles and dotdirs under `/root`.
  2. Binary files send name and hash only.
  3. In text files, a value whose key matches `pass|pwhash|secret|token|key|credential|auth`, and the password part of any `scheme://user:pass@` URL, are replaced by `***`.
- Second check in the sync pod: `gitleaks` runs over the masked tree before the Forgejo commit and the RackPeek post. The content of any flagged file is dropped and the file is listed in the run log.
- Kubernetes Secrets are never read. Their names and key names come from the SOPS files in Git, where key names are plain text. Secrets that are not in Git show up when an agent rescans and are labeled "live only".
- RBAC:
  - ClusterRole `rackpeek-reader` without Secrets, bound with a RoleBinding in each indexed namespace (including `argocd`).
  - ClusterRole `rackpeek-cluster-reader` with only nodes, PVs, StorageClasses and namespaces, bound with a ClusterRoleBinding.
  - A namespace without a RoleBinding shows in RackPeek as "not indexed", so gaps are visible.
- Stored in SOPS:
  - RackPeek API key
  - Forgejo DB role password, `SECRET_KEY` and `INTERNAL_TOKEN`
  - the bot's two Forgejo tokens
  - the scan SSH private key

## Boundaries

- Never read or list `t3code-work`, `mcp-hub-work`, the legacy `t3code` pod's home, or node paths holding their volumes:
  - those namespaces get no RoleBindings
  - Argo Applications whose destination is one of them are skipped
  - PVs and TrueNAS zvols bound to their claims are skipped in code
- RackPeek and Forgejo hold internal addresses and masked config. Both stay admin-only. Nothing from them goes into the public repo.

## First load (PR 4)

The first sync fills in the facts. Then the homelab agent writes the notes for every app, host and agent pod: what it is, why it is set up that way, and how it updates. It uses the handoff inventory of 2026-11-16 and the live files, through the RackPeek API from its own namespace. This PR adds only the rescan procedure to the RackPeek README. The data lives in RackPeek and Forgejo.

## Risks

- R1. A root-capable forced command runs on every host. The script is read-only and in Git. Anyone with root on a host could change it, but they already have root there. The key cannot run anything else.
- R2. Masking can still miss a secret in an unusual format. The name-only list and gitleaks reduce that. Masked copies sit only in admin-only RackPeek and Forgejo. A finding is fixed by adding a rule and rewriting `homelab-live` history.
- R3. RackPeek is v2, young and maintained mainly by one person. The YAML file plus Forgejo history make leaving it cheap.
- R4. Forgejo behind forward-auth means no `git clone` from outside the cluster. Not needed now.

## Out of scope

Forgejo OIDC, drift diffs for host files, Redfish reads from iLO and iDRAC, homepc and laptop, scanning inside vidar, deleting gone items automatically.

## Delivery

1. PR 1: Forgejo, its database, Authentik, DNS.
2. PR 2: RackPeek, Authentik, DNS, Git sync to Forgejo.
3. PR 3: sync image, CronJob, RBAC, network policy, scan script. The hosts are installed after the owner confirms.
4. PR 4: first load, notes, rescan procedure.

Each PR merges, then Argo syncs with a dry run first at the merged SHA.
