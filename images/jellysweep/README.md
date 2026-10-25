# Jellysweep collection-members build

Temporary `ghcr.io/spyrospsarras/jellysweep` overlay, tracked in
[epaflix#2060](https://github.com/SpyrosPsarras/epaflix/issues/2060).
The deployment still pins the upstream v0.16.1 image. Once the main-branch
build publishes this overlay, a separate PR will pin its digest. Jellysweep
stays at `replicas: 0` and `JELLYSWEEP_DRY_RUN=true` until the owner unpauses it.

## Included fixes

- F1: upstream [#440](https://github.com/jon4hz/jellysweep/pull/440), commit
  [da58d64](https://github.com/jon4hz/jellysweep/commit/da58d642d64b349fc633a125254665b7b274ffda),
  explicitly sets `CollapseBoxSetItems(false)` on library scan pages. Jellyfin
  12 otherwise substitutes BoxSets for individual members, which can make
  queued media disappear from the library snapshot.
- F2: `collection-direct-members.patch`, from the
  [fork branch](https://github.com/SpyrosPsarras/jellysweep/tree/fix/collection-items-direct-members),
  restricts `GetCollectionItems` to `Movie` and `Series`. A read-only Jellyfin
  12.3 check returned 2,397 recursive TV collection items: 115 Series, 213
  Season and 2,069 Episode, versus 115 direct members. Movies returned 58
  Movie in either query. The extra 2,282 IDs caused 46 unnecessary removal
  batches. Jellyfin skips these non-members but saves and refreshes the
  collection per batch.

Recursion stays enabled for compatibility. On Jellyfin
[10.11](https://github.com/jellyfin/jellyfin/blob/v10.11.0/MediaBrowser.Controller/Entities/Folder.cs#L944-L1003)
and [12.3](https://github.com/jellyfin/jellyfin/blob/v12.3/MediaBrowser.Controller/Entities/Folder.cs#L1106-L1159),
non-recursive API-key queries take the `Children` path with no user. Modern
BoxSets can have empty physical children and store their members as linked
children. Keeping the existing recursive lookup and filtering types avoids
that compatibility risk. Jellysweep creates leaving collections from movies
and series, so excluding Season and Episode is sufficient for those collections.
The regression test asserts the outgoing parent, recursion and type parameters.

## Build

`Dockerfile` clones upstream at `da58d642d64b349fc633a125254665b7b274ffda`,
checks and applies the patch, runs `go test -p 1 ./...`, then builds a static
binary with the upstream commit and patch name in its version string.
Tests and build share `CGO_ENABLED=0` and serial package compilation to keep
builder memory use bounded.
Generated templates and web assets are already committed upstream.

The final stage overlays only `/usr/local/bin/jellysweep` onto
`ghcr.io/jon4hz/jellysweep:v0.16.1@sha256:b47881bc40ef32a0667384041351a762d584fcc2afb4297cef334f06391fc6cf`.
The source pin is exactly one query-and-test commit after v0.16.1, with no asset
changes. The base supplies the distroless runtime files, including CA certificates,
and the image configuration: entrypoint, command, healthcheck, user, environment,
working directory, volume and port. These come from the upstream image unchanged.
This intentional source/base difference is not Lingarr's same-revision pairing.
Future release moves must select the release commit and its matching image digest
together.

`.github/workflows/build-jellysweep.yml` builds and smoke-tests on PRs. It checks
the embedded patch marker, inherited image configuration, CLI help, and both
successful and failed healthchecks against a local fixture HTTP server. It does
not start Jellysweep's server or connect to media services. Only main can push
`<short-upstream>-collection-direct-members-<github.sha>` to GHCR and print the
digest for the follow-up pin. Renovate does not move the `golang:1.26` builder;
Go patch releases come through the moving `1.26` tag at build time. Minor and
major versions follow upstream's `go.mod` and move by hand with `UPSTREAM_COMMIT`.
The overlay image and upstream base cannot move independently.
`UPSTREAM_COMMIT` has no Renovate marker and is moved by hand.

## Return to upstream

The weekly `upstream-release-watch.yml` row reopens #2060 on a stable release
after the watch row's `pinnedVersion`. The issue stays closed while waiting.
A release notification is not proof that either fix shipped.

A1. Verify #440 with
`gh api repos/jon4hz/jellysweep/compare/da58d64...<tag> --jq .status`.
Only `ahead` or `identical` proves inclusion. For F2, compare its upstream
merge commit to the tag once the owner has submitted it and upstream has
merged it. Until then, inspect `GetCollectionItems` at the release tag for
equivalent filtering that preserves members on Jellyfin 10.11 and 12.

A2. If both fixes shipped, make one PR that pins
`ghcr.io/jon4hz/jellysweep:<tag>@<digest>` in
`2-k3s/08.servarr/kustomization.yaml`, removes `newName` and the TEMP OVERRIDE
comment if present, deletes this directory and `build-jellysweep.yml`, removes
the Jellysweep overlay, base and Go-builder Renovate rules,
the `ghcr.io/spyrospsarras/jellysweep` entry in the `disabled` set and the Jellysweep
policy cases in `.github/hooks/test-servarr-renovate.mjs`, and the watch row, and updates
`docs/servarr-image-pins.md`. Close #2060 after merge.

A3. If only #440 shipped, move `UPSTREAM_COMMIT` and `BASE_IMAGE` to that release.
Confirm the new base digest's `org.opencontainers.image.revision` label equals
the release commit. Move the `golang` builder to the new release's `go.mod` version.
In the same change, update the hardcoded `0.16.1` in the Dockerfile's ldflags
and OCI version label, the matching binary-marker grep in `build-jellysweep.yml`,
the Dockerfile header comment, the Jellysweep base and Go-builder rule descriptions
in `.github/renovate.json`, and the Jellysweep row in `docs/servarr-image-pins.md`.
Also update `images/jellysweep/README.md`: the introduction's upstream-image pin,
the Go-builder tag explanation, the Build section's source-commit literal and
base-image digest, and its
"exactly one query-and-test commit after v0.16.1" statement and
"This intentional source/base difference is not Lingarr's same-revision pairing"
sentence. Update the #2060 body's source commit and "over the pinned upstream
v0.16.1 image" line.
Keep the collection-members patch, check `git apply --check`, and rebuild.
Pin the resulting overlay digest in a follow-up PR. Move `pinnedVersion` in the
watch row to the assessed release and close the issue until another release
appears. Otherwise (neither fix, or only the collection-members fix), keep the
overlay, record the assessment, move `pinnedVersion` to the assessed release,
and close the signal.
In every case, leave replicas and dry-run unchanged.
