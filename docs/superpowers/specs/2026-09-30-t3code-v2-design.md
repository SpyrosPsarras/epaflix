# T3 Code v2 test environment

Status: written specification approved by Spyros. Implementation has not been approved.

Planning constraint: use the question UI tool for all questions to Spyros.

## Goal

Run the unreleased T3 Code orchestrator v2 alongside the current v1 deployment. Spyros wants a fresh environment with the tools, integrations and effective access available in v1. History migration is excluded. Native Linux access is required, but its packaging remains undecided.

## Agreed decisions

| Ref | Decision |
| --- | --- |
| D1 | Use StatefulSet `t3code-v2`, pod `t3code-v2-0`, in the existing `t3code` namespace. Keep v1's deployment and update policy unchanged. |
| D2 | Use separate writable storage, a fresh T3 environment identity and fresh client pairing. Do not copy v1 databases or full home volumes. |
| D3 | Use display name `T3 Code v2` and proposed endpoint `https://t3code-v2.epaflix.com`, following the existing internal ingress pattern. Verify DNS and reachability before deployment. |
| D4 | Inventory all current v1 projects and create fresh repository clones in v2. Do not copy worktrees or uncommitted files. This is an initial inventory, not ongoing project synchronization. |
| D5 | Keep tools, private configuration, instructions, skills, plugins and effective access aligned with v1 over time. Reuse maintained inputs where compatible rather than maintaining independent copies. |
| D6 | Give v2 a distinct MCP hub client identity with equivalent permissions. Reuse existing service credentials where appropriate through runtime secret references. |
| D7 | Check upstream branch `t3code/codex-turn-mapping` hourly. Build automatically when its revision or maintained runtime inputs change. Pin each candidate to an exact source revision and immutable image tag. |
| D8 | Automatically deploy validated candidates only when v2 has no connected clients and no active work. Use guarded best-effort checks; this is not a guarantee against a task starting between the final check and restart. |
| D9 | Failed builds or compatibility checks retain the running v2 build. Retry when source or configuration changes. Failures must be visible in CI. |
| D10 | Keep Spyros's current Arch native application installed. `/usr/bin/t3code-nightly` is owned by `t3code-nightly-bin 0.0.45_nightly.20260930.2468-1`. Decide v2 native packaging separately. |

## Tool and access parity

Parity means equivalent usable capabilities, not identical history or internal files. Before implementation, inventory the actual v1 configuration and identify each dependency's maintained source. Do not put secret values in this document, build inputs, images or CI logs.

The inventory must cover:

- A1. Provider CLIs, configured provider instances, CLIProxy endpoints, custom models and launch arguments. Preserve v1's enabled and disabled provider choices unless a change is approved.
- A2. MCP hub services and permissions, including thread-scoped T3 tools. Verify discovery and representative calls, not just configuration presence.
- A3. Jev routing and guards, OpenCode Loop, commands, private instructions, skills and plugins.
- A4. GitHub HTTPS and SSH access, kubeconfigs, cluster tooling, Azure CLI authentication and its helper scripts, and other existing authenticated tools.
- A5. T3 terminals, file access, previews, repository/worktree operations and pull request linking.

Fresh homes must receive necessary configuration and authentication state through a selective, documented provisioning process. Preserve required paths or adapt references deliberately. Provider configuration from the additional v1 homes must be inventoried; fresh storage does not mean those configurations can be omitted. Credentials retain their normal expiration and reauthentication requirements.

V2 has the same effective access to real services as v1. Separate storage isolates local files; it does not isolate remote actions performed with those credentials.

## Runtime and automatic updates

Reuse the existing image build, smoke-test and ArgoCD mechanisms where possible. Build v2 separately from v1. V2 publication must not overwrite v1 image tags or change its package pins.

The source build and packaging command must be verified before selecting the final CI implementation. Tracking the npm `preview` tag alone does not satisfy hourly branch tracking because upstream publishes previews separately.

An eligible rollout requires:

- A6. No connected clients, active provider runs, runnable queued work, pending approvals or questions, background agent/tool work, or running terminal commands.
- A7. A short sustained idle period and a final check immediately before replacement. The exact interval and connection-count mechanism must be established by investigation and tests.
- A8. Unknown state, query failure or incompatible activity schema blocks deployment. An idle shell alone does not block deployment, but a connected client does.

Future scheduled work must be considered when establishing the restart check so a rollout does not knowingly overlap an imminent task. The investigation found no single upstream idle API that covers all these conditions. Read-only state checks are candidates, not approved implementation details. Test the connected-client count and background-work detection against the actual v2 runtime.

Keeping the laptop app connected can postpone updates indefinitely. Do not force a rollout after an arbitrary timeout. Preserve the previous image and take a recoverable v2 state backup before upgrades that may change its schema. Image rollback alone is not proof that an older binary can read an upgraded database.

## Native laptop connection

The laptop is Arch Linux x64. Spyros does not want an AppImage workflow and wants to keep both v1 and v2 native applications.

Source inspection and packaged-artifact inspection established that current v1 clients speak protocol 1, while v2 requires protocol 2. V2 is a branch build of the same T3 Code application, not a different product. Protocol compatibility is required; identical client/server commits are not, although features can differ within protocol 2.

The planned connection uses a v2-capable native client and a fresh pairing URL for the v2 HTTPS endpoint, entered through Settings → Connections → Add environment. Keep v2's local T3 home separate. Verify coexistence and the shared `t3code://` handler rather than assuming separate Electron profiles isolate everything.

Native package delivery, updates and installer coexistence remain open. Do not implement a custom pacman package or AppImage launcher without a separate decision. The server specification can be reviewed while this is open, but end-to-end native acceptance cannot pass until it is resolved.

## Acceptance checks

- T1. Both pods run in namespace `t3code`; v2's storage and environment identity are distinct. V1's manifests, runtime pins and endpoint remain unchanged.
- T2. V2 starts fresh, with no imported v1 threads or transcripts. Every repository in the initial inventory has a fresh usable clone. Unsupported or non-remote project entries are reported rather than silently skipped.
- T3. A parity checklist maps every inventoried capability to provisioning and verification evidence. Test authentication without exposing secrets or performing unapproved mutations to external systems.
- T4. OpenCode and Codex complete representative turns with the configured proxy and models. Verify relevant plugins, commands, MCP calls and T3 thread tools.
- T5. Hourly checking detects a branch change, produces a revision-pinned candidate and deploys it when eligible. A failed candidate keeps the existing build running.
- T6. Connected clients and each active-work category prevent deployment. Unknown state fails closed. Demonstrate deferred rollout becoming eligible after disconnection and completion of work. Record the remaining best-effort race.
- T7. Restart preserves v2 configuration and its own new history. Validate recovery from a failed upgrade using an appropriate state backup, not only an image switch.
- T8. Once laptop packaging is decided, native v1 and v2 coexist; the v2 client pairs to the v2 endpoint and performs a real agent task. Confirm launcher state isolation and link-handler behavior.

## Prerequisites before implementation

- P1. Inventory v1 projects, configuration, tools and authentication dependencies without copying history.
- P2. Verify branch build requirements, candidate validation and immutable publication.
- P3. Prove client-count and active-work checks against a running test instance. Choose the idle observation interval.
- P4. Check worker capacity, disk space, fresh-volume sizing and network/DNS requirements.
- P5. Resolve Arch native packaging and update delivery before claiming the full laptop workflow works.
- P6. Review this written specification, then create and approve an implementation plan.

## Sources

- [Upstream orchestrator v2 PR](https://github.com/pingdotgg/t3code/pull/2829).
- [Protocol constant on inspected v2 revision](https://github.com/pingdotgg/t3code/blob/8297ccf85d925d912caa85c8cbf0dd36625d7017/packages/contracts/src/environment.ts#L13-L16).
- [Client protocol compatibility check](https://github.com/pingdotgg/t3code/blob/8297ccf85d925d912caa85c8cbf0dd36625d7017/packages/client-runtime/src/connection/compatibility.ts#L9-L30).
- [Remote access documentation](https://github.com/pingdotgg/t3code/blob/t3code/codex-turn-mapping/docs/user/remote-access.md).
- [Restart recovery implementation](https://github.com/pingdotgg/t3code/blob/0a42dd3921b12ff9bbc89058a15e51c21764b430/apps/server/src/orchestration-v2/ProviderRuntimeRecoveryService.ts).
- Existing deployment: `2-k3s/13.t3code/one/statefulset.yaml`, `one/files/entrypoint.sh`, `one/kustomization.yaml` and `env/tools/smoke.sh`.
- Existing build pipeline: `.github/workflows/build-t3-runtime.yml`.
- Existing ingress: `2-k3s/05.traefik-deployment/ingress/t3code-proxy.yaml`.
