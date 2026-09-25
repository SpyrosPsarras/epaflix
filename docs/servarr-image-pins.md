# Servarr image update policy

Issue #1129 corrects #540's claim that every other servarr image was version-pinned or safe to roll back. Sonarr, Radarr, Prowlarr and Bazarr were digest-only. A digest identifies bytes, but Renovate resolves an untagged entry against `latest`, so an application major can arrive as an auto-merged digest update.

## Release pins

Registry checks on 2026-09-15 resolved each tag below to the existing digest in `2-k3s/08.servarr/kustomization.yaml`. This change preserves image contents. Use `newTag: version@sha256:...`, not separate `newTag` and `digest` fields: Renovate 43.33.2 marks that combination `invalid-dependency-specification`.

| Image | Tag | Existing digest prefix |
| --- | --- | --- |
| linuxserver/sonarr | 4.0.19 | 82172b363f9e |
| linuxserver/radarr | 6.3.0 | fe051413dfd9 |
| linuxserver/prowlarr | 2.5.2 | c7502a75b021 |
| linuxserver/bazarr | 1.6.1 | d24bd0048c75 |
| cleanuparr/cleanuparr | 2.10.6 | 8136c3beda7a |
| hotio/unpackerr | release-0.15.2 | 724c702e8172 |
| jellyfin/jellyfin | 12.1 | 78d3ea1207d1 |
| zelak312/bazarr_autotranslate | v1.0.0 | 5388463f8e45 |
| qbittorrentofficial/qbittorrent-nox | 5.2.3-1 | 9ebb534fe30ba |

LinuxServer publishes clean three-component tags alongside build tags. Docker versioning treats `-ls324` as a compatibility suffix, so `4.0.19.2979-ls324` would reject a candidate ending in `-ls400`. Clean tags avoid that filter and allow same-version rebuild digests. Unpackerr needs regex versioning for `release-`; qBittorrent needs it to treat its numeric image-build suffix as a patch instead of a fixed compatibility suffix. The regex excludes qBittorrent's separate `lt2` variant.

Minor and major releases require review throughout this images block. Patch releases and same-tag digest rebuilds still auto-merge, except for the manual-review images below. A patch can still contain an application migration; this policy preserves the patch behavior requested in #1129, rather than promising that patches are migration-free.

## Scope and exceptions

All twelve originally listed digest-only entries were assessed. CleanUparr and Jellyfin have persistent application config, so they are pinned rather than assumed safe. Unpackerr, Bazarr Autotranslate and qBittorrent are also pinned without relying on an absence-of-migrations claim. Neutarr already has a release pin. Homarr and Sonarr2 are no longer in this checkout.

| Entry left on a moving tag | Evidence and policy |
| --- | --- |
| Byparr | `byparr/byparr.yaml` mounts only `/dev/shm` from an `emptyDir`, with no persistent config or database connection. Its pinned image labels identify source commit `cb2a862386e92f141e8aa3b58f8532ef2fc36ed0`; neither published 2.0 release tag matches the digest. Keep digest updates enabled for this ephemeral browser service. |
| airvpn-bluetit | `images/airvpn-bluetit/Dockerfile` pins AirVPN Suite 2.1.0 with a checksum. `entrypoint.sh` regenerates daemon config and starts Bluetit. The bundled Python agent writes JSON observations and Prometheus output, not relational migrations. The registry publishes `latest` and commit hashes only. Keep digest updates; the vendored Suite has its own Renovate manager and checksum review. |
| vpn-picker | `images/vpn-picker/Dockerfile` installs Python and ping, with no database driver. `vpn_picker.py` persists validated JSON verdicts, rankings and measured bases through atomic file replacement. It does not run relational migrations. The registry publishes `latest` and commit hashes only. Keep digest updates. This is persistent JSON state, not a claim that the service is stateless. |
| Lingarr | Self-built batch-contract overlay (`ghcr.io/spyrospsarras/lingarr`, upstream main 28f6a19 + `images/lingarr/batch-contract.patch`) pinned by digest. Revert condition in the images block comment; excluded from Renovate, both the overlay image and the upstream base digest (`images/lingarr/Dockerfile` FROM - the upstream `:main` tag regressed twice, PRs #1368 and #1434). Update signal: upstream-release-watch row #270, baselined at release 1.3.0; the build guard fails closed on a commit/digest mismatch. |
| Streamystats AIO | Added since the original issue; owns PostgreSQL data. The current digest matches `latest` and commit `1a154af6e59b5f6a5407f1e18704cc6bd5ac15bf`, but none of the six published release tags, v2.18.0 through v2.20.0. Require review for every update until a matching release is adopted. |

Paths in the table are relative to `2-k3s/08.servarr/` unless they start with `images/`. The evidence is scoped to these deployed configurations and source versions. Reassess an exception if it gains a database or persistent migration mechanism.

## Regression check

Install the deployed Renovate version in a temporary directory, then run from the repository root:

```sh
npm install --prefix /tmp/servarr-renovate --ignore-scripts --no-audit --no-fund renovate@43.33.2
RENOVATE_ROOT=/tmp/servarr-renovate/node_modules/renovate node .github/hooks/test-servarr-renovate.mjs
```

The check invokes Renovate's actual Kustomize extractor, versioning APIs, update classifier and ordered package rules. It checks valid digest-bearing dependencies, Sonarr 4 to 5 classification, patch discovery, digest automerge, manual minor/major updates, and the development-image exceptions. Registry tag-to-digest checks are separate because tags can legitimately be rebuilt after this change.

The issue's withdrawn `imagePullPolicy` item does not require a manifest change. A changed digest changes the pod template and starts a rollout regardless of pull policy.
