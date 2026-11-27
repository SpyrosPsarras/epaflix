# GPU apps on k3s

Jellyfin belongs to the `servarr` Argo application. llama-server and Open WebUI belong to the `ai` application in this directory. Open WebUI's Valkey container runs in the same pod.

## Storage and scheduling

Jellyfin's `/config` and Open WebUI's data were copied from TrueNAS once on 2026-11-03. Size, file count and owners matched the source:

| App | Node directory | Container directory |
| --- | --- | --- |
| Jellyfin | `/srv/jellyfin/config` | `/config` |
| Open WebUI | `/srv/open-webui` | `/app/backend/data` |
| llama-server | `/srv/llama-server` | `/models` (read-only) |

## Local model

llama-server serves one model, Qwen 3.8 27B as PrismML's Ternary Bonsai 2.5 `PTQ1_0-mtp` file, under the name `qwen3.8-bonsai:27b`. The image `ghcr.io/spyrospsarras/llama-prism` (`images/llama-prism`) is PrismML's llama.cpp fork. Stock llama.cpp and Ollama cannot load `PTQ1_0`; on Ollama the 2-bit `Q2_0` file ran 21% on the CPU at about 5 tokens per second.

Settings in `llama-server.yaml` come from tests on this card with release `prism-b12232`: one request slot, a 32768-token context with a `q5_1` cache, and drafting with the file's built-in MTP head. That uses 7709 MiB of the 8192 MiB. Drafting raised generation from 51 to between 55 and 95 tokens per second, depending on the text. Against a 16-bit cache, `q5_1` changed the top token on 1.0% of positions (mean KL divergence 0.00078), `q4_0` on 2.1% (0.00285) and `q8_0` on 0.9% (0.00022). `q8_0` fits only 24576 tokens. PrismML's K-cache bias for `q4_0` requires turning off llama.cpp's K rotation and drifted more than plain `q4_0` (0.00401). The 0.59 GiB vision file does not fit. The model thinks before it answers unless a request turns thinking off.

The model file does not ship in Git. On an empty `/srv/llama-server`, download it on `k3s-worker-64` and check its SHA-256 against the Hugging Face file page:

```sh
sudo install -d -m 755 /srv/llama-server
sudo curl -fL -o /srv/llama-server/Ternary-Bonsai-2.5-27B-PTQ1_0-mtp.gguf https://huggingface.co/prism-ml/Ternary-Bonsai-2.5-27B-GGUF/resolve/28a7093cc25bc151cc2b284c5570f12ce343fdcf/Ternary-Bonsai-2.5-27B-PTQ1_0-mtp.gguf
echo 'e0fc5f3d47b9c926d52677d303b19df356a2376614b3e1cb9973865fc04ca431  /srv/llama-server/Ternary-Bonsai-2.5-27B-PTQ1_0-mtp.gguf' | sha256sum -c -
sudo chmod 644 /srv/llama-server/Ternary-Bonsai-2.5-27B-PTQ1_0-mtp.gguf
```

## Placement

Static local PVs retain this data and pin Jellyfin, llama-server and Open WebUI to `k3s-worker-64`. They cannot fail over to another node. The `gpu-local` no-provisioner StorageClass is owned by servarr's `jellyfin/storage.yaml`. No app uses dynamic local-path provisioning. Local PV capacity is a binding declaration, not a disk quota.

Jellyfin mounts `servarr-media` subdirectories for `/movies`, `/tvshows`, and `/animes`. Cache and transcodes use disk-backed `emptyDir` volumes. Valkey's `/data` is also `emptyDir`; its cache and sessions do not survive pod replacement.

Jellyfin and llama-server each request one NVIDIA time-sliced GPU allocation and require the `nvidia` RuntimeClass. Merge and deploy the separate GPU-node/device-plugin change first. It also makes Alloy and node-exporter tolerate the GPU taint and moves GPU monitoring off TrueNAS. Open WebUI requests no GPU and disables its image's embedded Ollama process.

Memory limits total 16.5 GiB: Jellyfin 4 GiB, llama-server 8 GiB, Open WebUI 4 GiB, and Valkey 512 MiB. CPU limits are 4 for llama-server and 2 each for Open WebUI and Valkey.

## Endpoints and cutover checks

Jellyfin keeps `https://jellyfin.epaflix.com` and its existing Traefik middlewares. The canonical cluster endpoint is `http://jellyfin.servarr.svc.cluster.local:8096`. Argo prunes the old `jellyfin-truenas` Service and EndpointSlice.

llama-server is cluster-only at `http://llama-server.ai.svc.cluster.local:8080/v1`, an OpenAI-compatible API without a key. Paperless-gpt calls it directly. CLIProxy's `ollama` provider, set in its management panel, points at it and offers the model as `ollama-qwen3.8-bonsai-27b` and as the low-priority `or-minimax-m3:free` failover. No unauthenticated LAN endpoint is added.

Open WebUI uses Traefik's LAN-only `internal` entry point at `https://open-webui.epaflix.com`, with its own login. Pi-hole resolves the hostname to `192.168.10.102`. It replaces the old TrueNAS `:31028` endpoint. Its env files are stored as two SOPS-encrypted Secrets and loaded through ksops. Deployment env entries select llama-server and loopback Valkey without changing the encrypted source values. Valkey authentication uses the password from its Secret.

Jellysweep and Seerr's encrypted seeds now use the canonical Jellyfin Service. Jellysweep reads its seed on startup; Seerr seeds only when its config file is absent.

Three apps keep the old address in their own data. Change each one once, by hand, at cutover, after a backup of the file or row:

| App | Stored setting | New value |
| --- | --- | --- |
| Seerr | `jellyfin.ip` and `jellyfin.port` in `/app/config/settings.json` | `jellyfin.servarr.svc.cluster.local`, `8096` |
| Streamystats | `servers.url` in its database | `http://jellyfin.servarr.svc.cluster.local:8096` |
| Open WebUI | `ollama.enable`, `openai.enable`, `openai.api_base_urls` and `ui.default_models` in the `config` table of `webui.db` | `false`, `true`, `["http://llama-server.ai.svc.cluster.local:8080/v1"]`, `"qwen3.8-bonsai:27b"` |

Open WebUI's stored settings override its environment, so the Deployment env alone does not move it.
