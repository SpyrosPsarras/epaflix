# Accepted risks

Findings we know about and decided not to act on. This file exists so a "we know, and no" answer has a durable home that is not an open GitHub issue (see the follow-up triage rule in `CLAUDE.md`).

Reopening means: open a `gh issue` that references the entry, and delete the entry in the same PR.

## Format

```
### YYYY-MM-DD short title
- Finding: what was observed, with a link to the PR/issue/session where it surfaced.
- Why no action: the reason, stated plainly.
- Reopens when: the concrete condition that would turn this back into work.
```

## Entries

### 2026-08-24 Sonarr stays on v4 despite a known upstream cache fix
- Finding: Sonarr `2f9e12a` fixes the stale tracked-download cache that blinded our queue on 2026-08-07 (#834). It is contained in `v5-develop` and absent from `main` and `develop`. No v5 release tag exists, and no update channel or LinuxServer image we checked offered a v5 build. It would fix only the population whose newest `DownloadHistory` row is `DownloadGrabbed`, not the 648 whose newest row is `DownloadFailed`, which v5 re-pins to `Failed` on start exactly as v4 does. Full evaluation, including what each check can and cannot prove: `docs/superpowers/plans/2026-08-24-sonarr-v5-upgrade-evaluation.md`.
- Why no action: running it today means building and owning a Sonarr image for one event handler, for a fault that self-healed in about 71 minutes when it occurred (15 grabs 07:00:57Z to 07:01:31Z, 15 imports 08:12:34Z to 08:13:09Z). The census guard change on #834 covers the half that does not self-heal.
- Reopens when: a v5 build becomes reachable by any path we track, which means a `v5.x` tag on `Sonarr/Sonarr` releases, a v5 answer from `services.sonarr.tv` on any channel, or a LinuxServer `latest`/`develop`/version tag resolving to 5.x. Also reopens if the recovering population causes an incident that does not self-heal. Nothing watches Sonarr releases, so the trigger is a manual check, not an alert. #1129 must land first, or v5 arrives on its own as an auto-merged digest bump.

### 2026-08-24 A Copilot subscription is consumed by something that is not an editor
- Finding: the `cliproxyapi-copilot` plugin authenticates with the public VS Code Copilot device-flow client ID `Iv1.b507a08c87ecfe98` and sends the recognised `Copilot-Integration-Id` headers (`vscode-chat` / `copilot-chat`), because Copilot rejects unrecognised ones. GitHub's terms contemplate those editor integrations, not a self-hosted relay re-exposing the subscription as an OpenAI- and Anthropic-compatible API. Deployed under `2-k3s/17.remote-pi/README.md` -> "GitHub Copilot provider".
- Why no action: that is the feature, and the owner accepted it explicitly when it was raised before the plugin was configured. There is no compliant variant that still yields `gpt-5.6-sol`. One personal account is exposed, and a suspension costs that account and the models it serves, not the cluster.
- Reopens when: GitHub rate-limits, warns, or suspends the account, or Copilot starts rejecting the integration headers. The symptom is Copilot models vanishing from `/v1/models`, or `401`/`403` from the upstream, so the proxy's own model list is the check.

### 2026-08-24 One maintainer's binary sits in the cliproxy request path
- Finding: `cliproxyapi-copilot` is a third-party Go plugin (`arthur-sommer-etc/cliproxyapi-copilot-plugin`, MIT, one author) loaded in-process by the proxy, so it sees every prompt routed to a Copilot model. The release `.so` is 37.3 MiB of compiled code; its version and release-zip sha256 are pinned in the `fetch-copilot-plugin` initContainer args.
- Why no action: the checksum pin means we get the artifact that was reviewed rather than a substituted one, the source was read for the parts that decide behaviour (config defaults, OAuth endpoints, token handling), and the alternative is no Copilot access at all. This proxy is already a plaintext-at-relay trust boundary by design, documented in the same README, so the plugin adds a party rather than a new class of exposure.
- Reopens when: the repository changes hands or goes unmaintained, a release ships without a matching `checksums.txt`, or upstream CLIProxyAPI gains native Copilot support in the OSS build, which would make the plugin removable.
