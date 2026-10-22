# T3 runtime image inputs

The `t3code` pod in `../one/` runs the image built from this directory.

- Debian packages live in `tools/os-packages.txt`.
- Pi, Claude Code, Codex, T3 and Pi extension pins live in `tools/package.json` and its npm lockfile.
- Helm, Kustomize, Argo CD, SOPS and GitHub CLI versions live in `../versions.env`.

The Dockerfile defaults to `mixed`. `--build-arg T3_RUNTIME_VARIANT=homelab` omits Azure CLI and Bitwarden CLI. `--build-arg T3_RUNTIME_VARIANT=work` includes Azure CLI but omits Bitwarden CLI. All three variants keep the pinned T3, Pi, Claude Code, Codex and cluster tools. Both isolated images provide `/etc/codex` for enforced requirements. See the [homelab backend](../../13.t3code-homelab/README.md) and [work backend](../../13.t3code-work/README.md) for required private inputs and remaining live acceptance.

After changing image inputs, run `bash tools/sync-runtime-image.sh` here and include all three StatefulSet image changes. `runtime-tag.sh [mixed|homelab|work]` hashes the public image inputs and variant, including the single npm lockfile. T3's downloaded executable is checked against `SHA256SUMS` from the same GitHub release, but its bytes are not covered by the `inputs-<hash>` tag. Mixed tags retain `inputs-<hash>` and `latest`; homelab tags use `homelab-inputs-<hash>` and `homelab-latest`; work tags use `work-inputs-<hash>` and `work-latest`. CI checks all image references, builds and tests all variants, and publishes only tested images on main. The PreSync image-prepull Job waits for that image before ArgoCD updates the StatefulSet. The StatefulSet uses `OnDelete`, so the running pod keeps serving: the `t3code-idle-rollout` CronJob deletes its runtime pod, `t3code-0` in namespace `t3code`, `homelab-0` in `t3code-homelab`, or `work-0` in `t3code-work`, once T3 and terminal input have been idle for 30 minutes, or sooner if it is crash-looping, and the StatefulSet recreates it on the new image. Deleting the pod by hand, after checking for active sessions, applies the update immediately. The pod has no self-update path, and manual package installs do not survive a restart.

The [isolated runtime contract](isolated-runtime.md) defines private policy inputs, homelab kubeconfig constraints, provider retention, Codex requirements and stopped input rotation.

The isolated clients also support [scoped item writes and attachment replacement](vault-writes.md), with synthetic post-merge canaries for both orgs.

Credentials arrive at runtime and are never baked into the image. MCP servers come from `2-k3s/23.mcp-hub`; the pod entrypoint registers Pi, Claude Code and Codex against the hub using environment references.

## Pi

T3 launches Pi through `/scripts/pi.sh`. `t3-pi-settings.py` registers that binary and selects Pi for migrated default and text-generation settings. `pi-setup.py` writes the package list, CLIProxyAPI model aliases and agent instructions, and `pi.sh` refreshes the aliases on every Pi start with a 5 s limit, keeping the previous file if the refresh fails. The provider uses the proxy URL and key forwarded by `pi.sh`.

The packages supply CLIProxyAPI integration, output redaction, rpiv-todo, rpiv-ask-user-question, superpowers, ponytail and retry of transient provider errors. The image builds the pinned, reviewed cc-safety-net artifact at `/opt/cc-safety-net/2.6.4`. At startup, `cc-safety-net-install.py` verifies it and copies it into a fresh home without network access, or verifies the existing home copy. Missing, changed, unexpected or untrusted artifacts stop startup before T3 launches. The package's ponytail skill takes precedence over the private bundle copy. Safety layers are described in `../one/pi-safety.md`.

T3 shows the select title twice and without line breaks, tracked in [T3 issue #17895](https://github.com/pingdotgg/t3code/issues/17895), which needs no change here.

Startup runs `pi list` through `pi-setup.py check-packages` and aborts if a registered package is missing. CI runs `pi-setup.test.py`, `t3-pi-settings.test.py`, the other runtime Python tests and `rollout-hash.test.sh`.

`tools/smoke.sh` boots the built image through the deployed startup scripts with networking disabled and no production credentials, using synthetic private configuration, hub grants, OpenCode-era T3 settings and a local Git remote. Mixed smoke covers first boot, restart, retained user edits, hub registration for Pi, Claude and Codex, migrated Pi selections, the private rollback backup, package checks and failure before launch on a changed Safety Net artifact. It also runs the offline installer tests. Isolated smoke runs each backend through first boot and restart with its own writable home. `pi-parity.mjs` runs `/scripts/pi.sh --mode rpc --no-session` to check extension commands, a superpowers skill, package ponytail skill resolution and absence of extension load errors. It also rejects any installed or registered pi-permission-system package or loaded permission-system command. `pi-models.mjs` checks `/scripts/pi.sh --list-models` against mock cliproxy, requiring only `cliproxy` rows plus exactly one `cliproxy-textgen` row for `claude/claude-haiku-5-5` and both mock model IDs. rpiv-ask-user-question has no registered command, so its startup coverage is package registration, its extension file and absence of load errors. These checks do not exercise model calls.

Mixed pods and smoke use `../one/files/entrypoint.sh`; isolated pods and smoke use `files/isolated-entrypoint.sh`. `files/selftest.sh` checks the credential helper and lock pins for CI. Refresh shared files with `../one/tools/sync-shared.sh`; CI checks for drift.
