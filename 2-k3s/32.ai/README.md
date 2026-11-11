# GPU apps on k3s

Jellyfin belongs to the `servarr` Argo application. Ollama and Open WebUI belong to the `ai` application in this directory. Open WebUI's Valkey container runs in the same pod.

## Storage and scheduling

Jellyfin's `/config` and Open WebUI's data were copied from TrueNAS once on 2026-11-03. Size, file count and owners matched the source:

| App | Node directory | Container directory |
| --- | --- | --- |
| Jellyfin | `/srv/jellyfin/config` | `/config` |
| Open WebUI | `/srv/open-webui` | `/app/backend/data` |
| Ollama | `/srv/ollama` | `/root/.ollama` |

Ollama's directory starts empty. Re-download the required models before switching its consumers: `qwen3.5:9b`, `gemma4:e4b`, `lfm2.5:8b`, and `aya-expanse:8b`.

Static local PVs retain this data and pin all three apps to `k3s-worker-64`. They cannot fail over to another node. The `gpu-local` no-provisioner StorageClass is owned by servarr's `jellyfin/storage.yaml`. No app uses dynamic local-path provisioning. Local PV capacity is a binding declaration, not a disk quota.

Jellyfin mounts `servarr-media` subdirectories for `/movies`, `/tvshows`, and `/animes`. Cache and transcodes use disk-backed `emptyDir` volumes. Valkey's `/data` is also `emptyDir`; its cache and sessions do not survive pod replacement.

Jellyfin and Ollama each request one NVIDIA time-sliced GPU allocation and require the `nvidia` RuntimeClass. Merge and deploy the separate GPU-node/device-plugin change first. It also makes Alloy and node-exporter tolerate the GPU taint and moves GPU monitoring off TrueNAS. Open WebUI requests no GPU and disables its image's embedded Ollama process.

Memory limits total 16.5 GiB: Jellyfin 4 GiB, Ollama 8 GiB, Open WebUI 4 GiB, and Valkey 512 MiB. Ollama's former 16 GiB and Valkey's former 4 GiB compose limits were cut to these values. CPU limits remain 4 for Ollama and 2 each for Open WebUI and Valkey.

## Endpoints and cutover checks

Jellyfin keeps `https://jellyfin.epaflix.com` and its existing Traefik middlewares. The canonical cluster endpoint is `http://jellyfin.servarr.svc.cluster.local:8096`. Argo prunes the old `jellyfin-truenas` Service and EndpointSlice.

Ollama is cluster-only at `http://ollama.ai.svc.cluster.local:30068`. CLIProxy's boot SQL migrates the old endpoint in existing config as well as fresh seeds. Lingarr reaches local models through CLIProxy, not through a direct Ollama URL. No plaintext consumer outside the cluster was found, so no unauthenticated LAN Ollama endpoint is added.

Open WebUI uses Traefik's LAN-only `internal` entry point at `https://open-webui.epaflix.com`, with its own login. Pi-hole resolves the hostname to `192.168.10.102`. It replaces the old TrueNAS `:31028` endpoint. Its env files are stored as two SOPS-encrypted Secrets and loaded through ksops. Deployment env entries select cluster Ollama and loopback Valkey without changing the encrypted source values. Valkey authentication uses the password from its Secret.

Jellysweep and Seerr's encrypted seeds now use the canonical Jellyfin Service. Jellysweep reads its seed on startup; Seerr seeds only when its config file is absent.

Three apps keep the old address in their own data. Change each one once, by hand, at cutover, after a backup of the file or row:

| App | Stored setting | New value |
| --- | --- | --- |
| Seerr | `jellyfin.ip` and `jellyfin.port` in `/app/config/settings.json` | `jellyfin.servarr.svc.cluster.local`, `8096` |
| Streamystats | `servers.url` in its database | `http://jellyfin.servarr.svc.cluster.local:8096` |
| Open WebUI | `ollama.base_urls` in the `config` table of `webui.db` | `["http://ollama.ai.svc.cluster.local:30068"]` |

Open WebUI's stored settings override its environment, so the Deployment env alone does not move it.
