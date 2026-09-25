# T3 runtime image inputs

The `t3code` pod (`../one/`) runs the image built from this directory:

- Debian packages go in `tools/os-packages.txt`.
- Agent CLI versions go in `tools/package.json` and its npm lockfile.
- Helm, Kustomize, Argo CD and SOPS versions go in `../versions.env`.

After changing image inputs, run `bash tools/sync-runtime-image.sh` from this directory and include the `one/statefulset.yaml` change. CI checks the image reference, builds the image, boots T3 with synthetic configuration, and publishes the tested image on main. Before ArgoCD replaces the pod, its PreSync Job `t3code-image-prepull` (`one/image-prepull.yaml`) pulls the new content-tagged image onto the pod's node, waiting up to 30 minutes for CI to publish it. The old pod keeps serving meanwhile, so an image update costs a normal restart instead of a download with T3 down. The pod has no self-update path, so the T3 UI shows "Manual update required". The only way its t3 version moves is a merged bump of `tools/package.json`. Manual apt installs in the pod do not survive a restart.

Azure CLI and kubectl use their vendor apt repositories. Adding a new vendor tool also requires its repository setup in the Dockerfile.

Credentials are supplied at runtime, never baked into the image. Existing GitHub and proxy Secret references remain in the StatefulSet. Installing a CLI does not create its authentication state.

T3 must manage OpenCode servers to attach its thread-scoped MCP tools, including `link_pull_request`. Leave the OpenCode Server URL blank. Startup migrates the old `http://127.0.0.1:4096` setting on existing PVCs. Other custom server URLs remain untouched and do not receive T3 tools. Existing sessions need to reconnect after migration.

MCP servers (vault, search, Gmail, Notion, Kubernetes) come from the MCP hub (`2-k3s/23.mcp-hub`); `../one/files/entrypoint.sh` registers them. The vault master password stays in the `syncthing` namespace.

`files/entrypoint.sh`, `files/selftest.sh` and `tools/smoke.sh` are kept for CI. The smoke test boots the image with this entrypoint. The pod runs `../one/files/entrypoint.sh`.
