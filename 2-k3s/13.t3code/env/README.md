# T3 runtime image inputs

The `t3code` pod in `../one/` runs the image built from this directory.

- Debian packages live in `tools/os-packages.txt`.
- Pi, Claude Code, Codex, T3 and Pi extension pins live in `tools/package.json` and its npm lockfile.
- Helm, Kustomize, Argo CD, SOPS and GitHub CLI versions live in `../versions.env`.

After changing image inputs, run `bash tools/sync-runtime-image.sh` here and include the `one/statefulset.yaml` change. `runtime-tag.sh` hashes the public image inputs, including the single npm lockfile. CI checks the image reference, builds and tests the image, and publishes it on main. The PreSync image-prepull Job waits for that image before ArgoCD replaces the pod. The old pod keeps serving while it waits. The pod has no self-update path, and manual package installs do not survive a restart.

Credentials arrive at runtime and are never baked into the image. MCP servers come from `2-k3s/23.mcp-hub`; the pod entrypoint registers Pi, Claude Code and Codex against the hub using environment references.

## Pi

T3 launches Pi through `/scripts/pi.sh`. `t3-pi-settings.py` registers that binary and selects Pi for migrated default and text-generation settings. `pi-setup.py` writes the package list, permission policy, CLIProxyAPI model aliases and agent instructions. The provider uses the proxy URL and key forwarded by `pi.sh`.

The packages supply CLIProxyAPI integration, permission enforcement, output redaction, rpiv-todo, superpowers and ponytail. The reviewed cc-safety-net Pi build is registered separately by `cc-safety-net-install.py`. The package's ponytail skill takes precedence over the private bundle copy. Safety layers are described in `../one/pi-safety.md`.

Startup runs `pi list` through `pi-setup.py check-packages` and aborts if a registered package is missing. CI runs `pi-setup.test.py`, `t3-pi-settings.test.py`, the other runtime Python tests and `rollout-hash.test.sh`.

`tools/smoke.sh` boots the built image without production credentials, using synthetic private configuration, OpenCode-era T3 settings and a local Git remote. It waits for T3 health and verifies migrated Pi selections, the private rollback backup and package check. `pi-parity.mjs` runs `/scripts/pi.sh --mode rpc --no-session` to check extension commands, a superpowers skill, package ponytail skill resolution and absence of extension load errors. Its sibling `pi-enforcement.mjs` uses the shipped permission-system pipeline against the written agent directory to assert environment dump denies, read-only allows and the Gmail send ask. `pi-models.mjs` checks `/scripts/pi.sh --list-models` against mock cliproxy, requiring only cliproxy provider models and both mock model IDs. These checks do not exercise model calls.

`files/entrypoint.sh`, `files/selftest.sh` and `tools/smoke.sh` serve CI. The pod runs `../one/files/entrypoint.sh`. Refresh shared files with `../one/tools/sync-shared.sh`; CI checks for drift.
