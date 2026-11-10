# Jellysweep v0.16.2, which includes upstream #440, plus the collection-members patch.
# UPSTREAM_COMMIT is the v0.16.2 release commit, the revision of BASE_IMAGE.
# release.Dockerfile is unchanged. Static assets and templates are embedded
# in the binary. The base supplies the distroless runtime files, including CA
# certificates, and the image configuration: entrypoint, command, healthcheck,
# user, environment, working directory, volume and port.
# Move the commit/base pair by hand through upstream-release-watch issue #2060.
# Once a release contains both fixes, revert this overlay per README.md.
ARG UPSTREAM_COMMIT=0dc52d3bb69aee0872587427e86dce0f6d9c50fa
ARG BASE_IMAGE=ghcr.io/jon4hz/jellysweep:v0.16.2@sha256:9aecbb8d97b4be3149c3a3b07b96be0076503346af42ac784ab5e733c06b3d55

FROM golang:1.26 AS build
ARG UPSTREAM_COMMIT
ENV CGO_ENABLED=0
WORKDIR /src
RUN git clone --quiet https://github.com/jon4hz/jellysweep.git . \
 && git checkout --quiet "$UPSTREAM_COMMIT"
COPY collection-direct-members.patch /tmp/collection-direct-members.patch
RUN git apply --check /tmp/collection-direct-members.patch \
 && git apply /tmp/collection-direct-members.patch \
 && go test -p 1 ./... \
 && go build -p 1 -trimpath \
      -ldflags "-s -w -X github.com/jon4hz/jellysweep/internal/version.Version=0.16.2+${UPSTREAM_COMMIT}.collection-direct-members -X github.com/jon4hz/jellysweep/internal/version.Commit=${UPSTREAM_COMMIT}" \
      -o /out/jellysweep .

FROM ${BASE_IMAGE} AS final
ARG UPSTREAM_COMMIT
# Link the GHCR package to this repository so GITHUB_TOKEN can publish it (#534).
LABEL org.opencontainers.image.source=https://github.com/SpyrosPsarras/epaflix \
      org.opencontainers.image.revision=${UPSTREAM_COMMIT} \
      org.opencontainers.image.version=0.16.2+${UPSTREAM_COMMIT}.collection-direct-members
COPY --from=build /out/jellysweep /usr/local/bin/jellysweep
