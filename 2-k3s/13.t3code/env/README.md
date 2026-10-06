# T3 runtime image inputs

The `t3code` pod (`../one/`) runs the image built from this directory:

- Debian packages go in `tools/os-packages.txt`.
- Agent CLI versions go in `tools/package.json` and its npm lockfile.
- Helm, Kustomize, Argo CD, SOPS and GitHub CLI versions go in `../versions.env`.

After changing image inputs, run `bash tools/sync-runtime-image.sh` from this directory and include the `one/statefulset.yaml` change. CI checks the image reference, builds the image, boots T3 with synthetic configuration, and publishes the tested image on main. Before ArgoCD replaces the pod, its PreSync Job `t3code-image-prepull` (`one/image-prepull.yaml`) pulls the new content-tagged image onto the pod's node, waiting up to 30 minutes for CI to publish it. If the image is still missing, the sync fails and ArgoCD retries it (`retry` in `11.argocd/apps/app-t3code.yaml`) until the image exists or a newer commit replaces it. The old pod keeps serving meanwhile, so an image update costs a normal restart instead of a download with T3 down. The pod has no self-update path, so the T3 UI shows "Manual update required". The only way its t3 version moves is a merged bump of `tools/package.json`. Manual apt installs in the pod do not survive a restart.

Azure CLI and kubectl use their vendor apt repositories. Adding a new vendor tool also requires its repository setup in the Dockerfile.

Credentials are supplied at runtime, never baked into the image. Existing GitHub and proxy Secret references remain in the StatefulSet. Installing a CLI does not create its authentication state.

T3 must manage OpenCode servers to attach its thread-scoped MCP tools, including `link_pull_request`. Leave the OpenCode Server URL blank. Startup migrates the old `http://127.0.0.1:4096` setting on existing PVCs. Other custom server URLs remain untouched and do not receive T3 tools. Existing sessions need to reconnect after migration.

MCP servers (vault, search, Gmail, Notion, Kubernetes) come from the MCP hub (`2-k3s/23.mcp-hub`); `../one/files/entrypoint.sh` registers them. The vault master password stays in the `syncthing` namespace.

## OpenCode 2

`tools/package.json` pins `@opencode/cli` (OpenCode 2; the package's postinstall copies the binary for this CPU into `bin/opencode.exe`) and `@opencode/client` at the same version, plus Codex 0.160.1, which T3's OpenCode 2 support needs (0.159 or later). T3 runs OpenCode through `../one/files/opencode-fast-version.sh`, which answers `--version` from the package and forwards everything else to that binary.

OpenCode 1 plugins do not run in OpenCode 2. The pod installs four ported plugins into `~/.config/opencode/plugins/` on every start: `cliproxy-models.js` (catalog, refetched every 15 minutes and reloaded when it changed), `jev-auto.js` (`../one/jev-auto.md`), `jev-guard.js` (`../one/jev-guard.md`) and `opencode-compat.js`, plus the reviewed cc-safety-net build through its OpenCode 2 package directory, and registers `superpowers@git+https://github.com/obra/superpowers.git` when the config lacks it. All six plugin IDs (`cliproxy-models`, `jev-auto`, `jev-guard`, `opencode-compat`, `cc-safety-net`, `superpowers`) are required.

Before T3 starts, `../one/files/opencode-plugin-check.mjs <opencode binary> <config dir> <plugin id>...` starts `opencode serve` on a copy of the config without its MCP servers, with private data and state directories, and asks it for its plugin list. A required ID that is missing or failed to load stops startup, with the plugin's error in the log. T3 starts OpenCode only when a thread needs it, so without this check a broken plugin would show up only inside a session.

What changed for agents:

- `bash` is now `shell` and `task` is now `subagent`; V1 `permission` rules are read and mapped by OpenCode 2.
- MCP tools stay direct tools named `<server>_<tool>`: `jev-guard.js` turns OpenCode 2's Code Mode off for every MCP server.
- `todowrite` (and `todoread`) come from `opencode-compat.js` and keep a session's list in plugin storage. T3 shows them as ordinary tool cards; OpenCode 2 has no todo list that T3's dedicated todo view could show.
- The `skill` tool takes `id`; `opencode-compat.js` also accepts the OpenCode 1 `name`.
- OpenCode 2 runs no language servers: no LSP tools or diagnostics. Use the project's lint, typecheck or compiler commands instead (accepted for this migration).
- OpenCode 2 reads only `AGENTS.md` for instructions, not a `CLAUDE.md` fallback.

The smoke's parity step, `node tools/opencode-parity.mjs <opencode binary> <config dir> [home]`, takes a provisioned `~/.config/opencode` and a HOME whose `.claude/skills` and `.agents/skills` it should also see. It copies them into a private HOME/XDG root under the temp directory, writes nothing outside it, and runs the real binary against mock CLIProxy, Jev and MCP servers: all six plugins load without errors, the catalog and `jev-auto/auto` are listed and a configured model overlay wins over the catalog, two Auto turns go out on their routed models while the session stays on Auto, an MCP tool with an `ask` rule is offered directly and makes no remote call after a reject, a T3-style MCP server added at runtime is offered directly, a private skill loads by `id` and `name` and a superpowers skill by `name`, the superpowers bootstrap reaches the first prompt, `todowrite` works, MCP output is masked and `rm -rf /` is blocked by cc-safety-net itself. `PARITY_KEEP=1` keeps the temporary root. On a workstation, point it at a scratch copy of the config and an OpenCode 2 binary; it never uses the real HOME.

`tools/smoke.sh` boots this directory's entrypoint on a synthetic OpenCode 1 store with rows only in its WAL: the backup runs first, then plugin provisioning, the plugin check (once more on a copy with a broken plugin, which must fail), T3, and the parity step on the provisioned config.

`files/entrypoint.sh`, `files/selftest.sh` and `tools/smoke.sh` are kept for CI. The smoke test boots the image with this entrypoint. The pod runs `../one/files/entrypoint.sh`.
