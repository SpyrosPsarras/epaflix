# Odysseus — TrueNAS Custom App (GPU agent) + Authentik SSO

> **SUPERSEDED (2026-06-07, #184).** This TrueNAS Custom App path is **no longer the
> live deployment.** Odysseus now runs on k3s (GitOps tier **`2-k3s/13.odysseus`**),
> pulling `ghcr.io/spyrospsarras/odysseus:73673258` from GHCR. This runbook is retained
> for provenance — it documents how the pinned image was originally built and run on the
> TrueNAS docker engine. For the current deployment see `2-k3s/13.odysseus` and **#184**.
>
> **Authoritative image refresh runbook:** the current rebuild → push → deploy
> cadence lives in the **Image refresh / rebuild** section of
> [`../../../2-k3s/13.odysseus/README.md`](../../../2-k3s/13.odysseus/README.md) (#211).
> The build recipe below is still accurate and is referenced from there.

Runbook for deploying **Odysseus** as a TrueNAS SCALE Custom App on `192.168.10.200`,
fronted by Traefik + Authentik forward-auth at `https://odysseus.epaflix.com`.

- Pinned build SHA: **`73673258199b353f9b3e04da9b37ae95077e2c8b`**
- Image tag: **`odysseus:73673258`** (built `--build-arg INSTALL_OPTIONAL=false` → MIT-clean, no AGPL/PyMuPDF)
- Host port: **`30070`** (`0.0.0.0:30070 → 7000`; mirrors the `300xx` convention: ollama `30068`, newtarr `30262`)
- Dataset: **`/mnt/pool1/odysseus`** (mirrors the `/mnt/pool1/ollama` convention)
- Local model serving: **REUSE the existing GPU-pinned Ollama app** on host port `30068`
  (`OLLAMA_BASE_URL=http://host.docker.internal:30068/v1`)

> TrueNAS Custom Apps **run** compose — they do **not** build. The upstream Odysseus
> compose uses `build: .`, which the Custom App path silently ignores / fails on.
> We therefore pre-build the image at the pinned SHA on a builder host and reference
> it by an immutable tag. The base compose and `docker/gpu.nvidia.yml` overlay are
> hand-merged into the single compose document in `docker-compose.yaml` here (Custom
> Apps take one compose document, not a `COMPOSE_FILE` chain).

---

## 0. Files in this directory

| File | Purpose |
|------|---------|
| `docker-compose.yaml` | The exact compose body to paste into the TrueNAS Custom App YAML editor. Secrets are `<PLACEHOLDER>` tokens — replace with real values **on TrueNAS only**, never in git. |
| `README.md` | This runbook. |

---

## 1. Pre-build the image at the pinned SHA (builder host)

Run on a host with Docker + internet — the repo workstation, or `ds-master`
(`192.168.10.71`, already runs Docker).

```bash
git clone https://github.com/pewdiepie-archdaemon/odysseus
cd odysseus
git checkout 73673258199b353f9b3e04da9b37ae95077e2c8b
git rev-parse HEAD     # MUST print 73673258199b353f9b3e04da9b37ae95077e2c8b before building

docker build --build-arg INSTALL_OPTIONAL=false -t odysseus:73673258 .
```

**AGPL decision:** `INSTALL_OPTIONAL=true` pulls PyMuPDF (AGPL-3.0) + markitdown. AGPL
linkage on a publicly reachable network service triggers source-offer obligations the
owner has not opted into, for a non-essential PDF/Office viewer feature. Keep it
MIT-clean. If the owner later wants the PDF/Office viewer, rebuild the **same** pinned
SHA with `INSTALL_OPTIONAL=true` and re-tag (e.g. `odysseus:73673258-optional`).

> Note: pinning the `main` tip SHA while the GitHub default branch may be `dev` —
> confirm `main` is the intended stable line before building.

## 2. Land the image on the TrueNAS docker engine (no registry needed)

**Actual path used:** the image was built **directly on the TrueNAS docker engine**, so
no cross-host transfer was needed — the `git clone … && docker build …` from section 1
was run on TrueNAS itself (the docker socket needs root, so via `sudo`), and the image
was already present on the engine the Custom App runs against. Verify and record the
immutable image ID so the Custom App is provably pinned:

```bash
ssh truenas_admin@192.168.10.200 'sudo docker images odysseus:73673258'
```

**Pinned image ID:** `44a5ac0a2364`

> **Alternative — remote builder + transfer.** If you build on a separate host (e.g.
> `ds-master`) instead of on-engine, ship the image over SSH with `docker load`. The
> TrueNAS docker socket needs root, so `docker load` runs via `sudo` — run it
> **interactively** (owner enters the sudo password); do **not** script the password:
>
> ```bash
> docker save odysseus:73673258 | ssh truenas_admin@192.168.10.200 'sudo docker load'
> ```

> Optional registry path: if a private registry is later preferred over docker-save,
> `docker tag odysseus:73673258 <registry>/odysseus:73673258 && docker push`, then
> reference the registry path in compose. docker-save is recommended here — there is
> no private registry in the inventory and it avoids publishing the image.

## 3. Create the dataset + subdirs

```bash
ssh truenas_admin@192.168.10.200
sudo midclt call pool.dataset.create '{"name":"pool1/odysseus"}'

sudo mkdir -p /mnt/pool1/odysseus/data \
              /mnt/pool1/odysseus/logs \
              /mnt/pool1/odysseus/config/searxng
sudo chown -R 1000:1000 /mnt/pool1/odysseus   # PUID/PGID = 1000:1000
```

(`data/huggingface` and `data/local` are created on first run as subdirs of `data/`.)

## 4. Paste the compose into the Custom App

TrueNAS UI → **Apps → Discover Apps → Custom App → Install via YAML**. Paste the body
of `docker-compose.yaml` from this directory, then **replace the placeholders with real
values typed into the TrueNAS env directly** (TrueNAS stores app config encrypted at
rest under `apps/ix-apps`):

| Placeholder | Source of real value |
|-------------|----------------------|
| `<ODYSSEUS_ADMIN_PASSWORD>` | generate without echoing: `PW=$(openssl rand -base64 24)` - a bare `openssl rand` prints the new password into the transcript (#602); store in the credential store `.github/instructions/secrets.enc.yaml` as a top-level `odysseus_admin_password` key (the store is flat - no nested blocks, #979). Change in-app after first login + rotate the stored value. |
| `<SEARXNG_SECRET>` | Leave unset to let the searxng entrypoint auto-generate (`token_urlsafe(48)`), OR an `openssl rand` value kept in the credential store. |
| `<OPENAI_API_KEY>` | Only if cloud OpenAI fallback is wanted; else leave blank. Credential store if used. |
| `<HF_TOKEN>` | Only if pulling gated HuggingFace models; else leave blank. Credential store if used. |

Start the app and confirm it reaches **RUNNING**.

## 5. Pull the local model on the existing Ollama app

Odysseus reuses the already GPU-pinned Ollama (the single RTX 2070 SUPER, 8 GB),
**not** a second in-process CUDA runtime:

```bash
ssh truenas_admin@192.168.10.200 'sudo docker exec ollama ollama pull qwen2.5:7b-instruct-q4_K_M'
# ~4.7 GB. Alt: llama3.1:8b-instruct-q4_K_M (~4.9 GB).
```

Embeddings stay on Odysseus's bundled FastEmbed `all-MiniLM-L6-v2` (CPU) to avoid
spending VRAM on an embed model.

### GPU posture — read before loading any in-process model

- The compose **keeps** the `deploy.resources.reservations.devices` nvidia block, so
  `/dev/nvidia*` + the CUDA libs **are** passed into the Odysseus container. This
  satisfies the literal "GPU passthrough to the docker" ask and lets **Odysseus
  Cookbook** load an in-process model **later** (Cookbook can only detect GPUs Docker
  exposes to the container).
- **Day one: do NOT load an in-process Cookbook model concurrently with Ollama.** There
  is exactly **one** 8 GB GPU. Two ~5 GB models do not both fit — you get OOM / eviction
  churn / CUDA failures. One model serves from Ollama (zero contention) and the GPU stays
  physically available to Odysseus for future use.
- If/when in-process Cookbook serving is wanted, cap it to a 3–4B Q4 model **or**
  coordinate so only one consumer holds a model at a time (tracked by its own gh issue).

## 6. Capture the GPU-util proof

```bash
# Terminal A — watch the GPU on TrueNAS:
ssh truenas_admin@192.168.10.200 'watch -n1 nvidia-smi'
```

Then trigger **one** Odysseus chat completion (model `qwen2.5:7b` via Ollama). During the
request `nvidia-smi` must show the **ollama** process attached to GPU 0 with non-zero
GPU-Util (typically 60–99% during decode) and VRAM used jumping to ~5 GB. Total used must
stay **< 8192 MiB** (only Ollama holds a model). Paste the snapshot here:

```
<PASTE nvidia-smi SNAPSHOT HERE — GPU-Util >0%, ollama on GPU 0, VRAM ~5 GB>
```

Also confirm the ssh mount was dropped (RCE hardening):

```bash
ssh truenas_admin@192.168.10.200 'sudo docker exec <odysseus-cid> ls -la /app/.ssh'   # expect empty/absent
```

---

## 7. Authentik SSO wiring

> **Auth model.** Odysseus has **no** trusted-header / forward-auth integration (no
> `X-authentik-username` handling — unlike newtarr's `proxy_auth_bypass`). So Authentik
> forward-auth gates only the **perimeter**; it cannot SSO-login the Odysseus user.
> We therefore keep **both** gates:
>
> 1. **Authentik forward-auth** at Traefik (perimeter, group-gated), and
> 2. **Odysseus's own built-in bcrypt login** (`AUTH_ENABLED=true`).
>
> This is a **double login by design.** It is the correct posture because the raw host
> port `0.0.0.0:30070` is reachable on the LAN **bypassing Authentik entirely** — Odysseus's
> own login is the only gate there — and the agent shell tool makes any unauthenticated
> path RCE-grade. `AUTH_ENABLED=false` (rely on forward-auth alone) is **REJECTED**: it
> would leave the LAN-facing raw port open to RCE.

Hardening already baked into the compose: `AUTH_ENABLED=true`, `LOCALHOST_BYPASS=false`,
`SECURE_COOKIES=true`, `ALLOWED_ORIGINS=https://odysseus.epaflix.com`,
`ODYSSEUS_SCRIPT_HOST=localhost` (never a real host), and the `./data/ssh:/app/.ssh`
mount is **omitted entirely**. Post-deploy, **disable the agent `shell` tool** if a
tool allowlist exists, and record it here.

**Step 0 — admin API access (durable service-account token, #185):** the old
standing personal `authentik_admin_api_token` was **retired** (#175); there is no
longer a personal admin token at rest. Automation that mutates Authentik objects
now uses the durable **`ak-iac` service-account token** (credential store key
`authentik_iac_service_account_token`, created declaratively by the
`authentik-iac-blueprint`). The Authentik steps below can also be done entirely in
the UI. See the **Admin / Automation API tokens** runbook in
[../../../2-k3s/07.authentik-deployment/README.md](../../../2-k3s/07.authentik-deployment/README.md).
Confirm access (and that the outpost exists) with
`GET /api/v3/outposts/instances/209f71f9-95f8-4264-91c2-4b065bbd6b07/` returning
**200**.

1. **Group** — create a NEW group **`Odysseus users`** (mirrors `Servarr users`). Add the
   owner (spy) as founding member. Only this group can reach the perimeter.
2. **Proxy Provider** (Forward auth — single application). Name `odysseus`. External host
   `https://odysseus.epaflix.com`. Auth flow pk `847dc682-757c-4bf8-925f-c8c066a0be4f`,
   invalidation flow pk `436f6b06-d1a1-42db-af49-0eaac2bce88a`, cookie domain `epaflix.com`
   (as per the servarr provider).
3. **Application** — name `Odysseus`, slug `odysseus`, link the provider from step 2. Bind
   a group binding so **only** `Odysseus users` is authorized.
4. **Embedded outpost** — since #293/PR #544 this is **declarative**, not an imperative
   `GET`/`PUT`/`PATCH`. Add a `!Find [authentik_providers_proxy.proxyprovider, [name,
   "odysseus"]]` line to the `providers` list under the `authentik_outposts.outpost` entry
   (id `embedded-outpost-membership`) in
   `2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml`. That list is
   **authoritative-replace** on apply, so add the new line without dropping the existing
   ones (newtarr provider **pk 82** and the rest) — then let the blueprint apply (worker
   file-watcher picks it up, no restart needed) and confirm the outpost redeploys healthy.
   Do not PATCH the outpost over the API — the next blueprint apply would revert it.

## 8. Cloudflare / DNS

**No Cloudflare change required.** `odysseus.epaflix.com` is already covered by the proxied
wildcard `*.epaflix.com`. A DNS-only shadow record is a **rejected** anti-pattern here (only
for LAN-only services — none apply). Always re-run
`dig +short odysseus.epaflix.com @1.1.1.1` (must return Cloudflare proxy IPs, no conflicting
orphan A record) immediately before deploy (per the 2026-05-17 ArgoCD incident). Optional
LAN-hairpin nicety: a Pi-hole `address=/odysseus.epaflix.com/192.168.10.101` line in
`dnsmasq.d` only (never the web UI) — not required for function.

## 9. GitOps merge order (the single switch that opens the perimeter)

The merge of `2-k3s/05.traefik-deployment/ingress/odysseus-proxy.yaml` (+ the kustomization
entry) is what makes `odysseus.epaflix.com` route through forward-auth. Do it **LAST**, so
the perimeter only opens once Authentik is ready and the app is fully proven offline:

1. **Author + validate repo** (this PR) — `kustomize build 2-k3s/05.traefik-deployment` OK.
   Do **not** merge yet; do **not** create Authentik objects yet.
2. **TrueNAS deploy + local GPU verify** (steps 1–6 above), off the public path.
3. **Authentik admin API access** — no personal standing token exists (retired by
   #175). Use the durable `ak-iac` service-account token (#185) or do the step-7
   objects in the UI — see the **Admin / Automation API tokens** runbook in
   [../../../2-k3s/07.authentik-deployment/README.md](../../../2-k3s/07.authentik-deployment/README.md).
4. **Create Authentik objects** (step 7), confirm outpost healthy.
5. **Merge GitOps** — rebase branch onto `origin/main`, `push --force-with-lease`, wait for
   `validate`, `gh pr merge <n> --merge`. ArgoCD reconciles the Endpoints/Service/IngressRoutes.
6. **Verify SSO end-to-end** (clean-browser 302 → `auth.epaflix.com`; non-member denied;
   member reaches Odysseus's own login; `Set-Cookie` carries `Secure`; CORS rejects other
   origins; `/outpost.goauthentik.io/` routes to authentik-server, priority 15 > 10).

---

## Rollback (per surface)

| Surface | Rollback |
|---------|----------|
| **TrueNAS app** | Stop + delete the `odysseus` Custom App (UI or `midclt app.delete odysseus`). Optionally `sudo docker rmi odysseus:73673258`. `/mnt/pool1/odysseus` (SQLite db, logs) persists for re-deploy, or `pool.dataset.delete` if abandoning. Ollama, open-webui and the GPU are unaffected — we only reused Ollama's API. |
| **Ollama model** | If unwanted: `sudo docker exec ollama ollama rm qwen2.5:7b-instruct-q4_K_M`. Existing models untouched. |
| **k3s manifests** | `git revert` the merge (or revert PR) removing `odysseus-proxy.yaml` + the kustomization entry; rebase + force-with-lease + `validate` + merge. ArgoCD prunes the Endpoints/Service/IngressRoutes; `odysseus.epaflix.com` returns Traefik 404. No other route affected. |
| **Authentik** | Remove the `odysseus` Application + Proxy Provider; remove its `!Find` line from the embedded outpost's `providers` list in `authentik-iac-blueprint.enc.yaml` (id `embedded-outpost-membership`, declarative since #293/PR #544 — **preserve newtarr's entry** and any others) and let the blueprint apply; optionally delete the `Odysseus users` group. No personal standing admin token exists to revert (retired by #175); the durable `ak-iac` service-account token (#185) is unrelated to Odysseus and stays. |
| **Cloudflare / DNS** | Nothing to roll back (wildcard pre-existed; no DNS-only shadow record was created). If a Pi-hole LAN-hairpin line was added, remove the `address=/odysseus.epaflix.com` line from the `dnsmasq.d` file. |
