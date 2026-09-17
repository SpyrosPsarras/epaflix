# Shared T3 tools

The LXC updater and both Kubernetes replicas consume the same tool inventory:

- Debian packages go in `tools/os-packages.txt`.
- Agent CLI versions go in `tools/package.json` and its npm lockfile.
- Helm, Kustomize, Argo CD and SOPS versions go in `../versions.env`.

After changing image inputs, run `bash tools/sync-runtime-image.sh` from this directory and include the StatefulSet change. CI checks the image reference, builds the image, boots T3 with synthetic configuration, and publishes the tested image on main. ArgoCD may briefly wait for publication before pulling the new content-tagged image. Each replica retains its own home PVC. The LXC updater timer (every 2 minutes) applies the shared inventory after pulling main. The replicas have no self-update path, so the T3 UI shows "Manual update required" for them. The only way their t3 version moves is a merged bump of `tools/package.json`, which ArgoCD rolls out. Manual apt installs on one server do not propagate.

Azure CLI and kubectl use their vendor apt repositories. Adding a new vendor tool also requires its repository setup in provisioning and the Dockerfile. Host services such as SSH and Syncthing remain LXC provisioning responsibilities.

Credentials are supplied at runtime, never baked into the image. Existing GitHub and proxy Secret references remain in the StatefulSet. Installing a CLI does not create its authentication state.

T3 must manage OpenCode servers to attach its thread-scoped MCP tools, including `link_pull_request`. Leave the OpenCode Server URL blank on the LXC and both Kubernetes replicas. Startup migrates the old Kubernetes `http://127.0.0.1:4096` setting on existing PVCs. Other custom server URLs remain untouched and do not receive T3 tools. Existing sessions need to reconnect after migration.

The vault bridge uses a dedicated SSH key restricted to `/usr/local/bin/keepass-mcp` on the LXC, with forwarding and PTY disabled. `tools/provision-vault-access.py`, run as the LXC service user from the repository root, generates the key, installs its restricted authorization, encrypts the Kubernetes Secret with SOPS and applies it. Include `vault-access.enc.yaml` in Git for ArgoCD recovery. The trusted host key comes directly from the LXC. No personal SSH key or vault master password is copied to the replicas.

## LXC follow-up

The image could run T3 on the LXC with a separately mounted home, runtime credentials and a service supervisor. The current LXC blocks nested container mounts, so that requires an explicit Proxmox/container-runtime decision and a tested session migration. This change keeps the existing LXC service.
