# Lingarr with the batch-contract patch (see README.md and batch-contract.patch).
#
# Two stages. `build` publishes upstream Lingarr.Server at the pinned commit
# with the patch applied, using upstream's own restore/publish flags. `final`
# is the exact upstream image the cluster already runs, with the published
# backend copied over /app. wwwroot (the Vue client) is not touched: it is
# not part of the backend publish, and the base image already carries the
# build for this same commit. Entrypoint, user, env and everything else come
# from the base unchanged.
#
# UPSTREAM_COMMIT and the base digest must describe the same source: the base
# image's assembly InformationalVersion is 0.0.0-dev+<UPSTREAM_COMMIT>.
ARG UPSTREAM_COMMIT=28f6a190529187c70fddb2b1435157ed1635dccb
ARG BASE_IMAGE=ghcr.io/lingarr-translate/lingarr@sha256:12473f6f40beac1bb91ffadc7d8c7f1c2a0cd99ba336e4bf2c1dfa617c27a763

FROM --platform=$BUILDPLATFORM mcr.microsoft.com/dotnet/sdk:10.0 AS build
ARG UPSTREAM_COMMIT
ARG TARGETARCH
WORKDIR /src
RUN git clone --quiet https://github.com/lingarr-translate/lingarr.git . \
 && git checkout --quiet "$UPSTREAM_COMMIT"
COPY batch-contract.patch /tmp/batch-contract.patch
RUN git apply --check /tmp/batch-contract.patch && git apply /tmp/batch-contract.patch
# Same flags as upstream Lingarr.Server/Dockerfile steps 3-4. The version string
# keeps upstream's commit so /api/version and the logs still say where the
# source came from; the suffix marks the patch.
RUN dotnet restore "./Lingarr.Server/Lingarr.Server.csproj" --arch "$TARGETARCH" \
 && dotnet test "./Lingarr.Server.Tests/Lingarr.Server.Tests.csproj" -c Release --nologo -v q \
      --filter "FullyQualifiedName~LocalAiServiceTests|FullyQualifiedName~SubtitleTranslationServiceTests" \
 && dotnet publish "./Lingarr.Server/Lingarr.Server.csproj" -c Release -o /app/publish \
      /p:UseAppHost=false --arch "$TARGETARCH" \
      "/p:Version=0.0.0-dev+${UPSTREAM_COMMIT}.batch-contract" \
 # The client is not built here, so this publish carries an empty wwwroot and a
 # staticwebassets manifest describing it. Both must stay out of the overlay
 # or they would shadow the base image's real Vue build.
 && rm -rf /app/publish/wwwroot /app/publish/Lingarr.Server.staticwebassets.endpoints.json

FROM ${BASE_IMAGE} AS final
# Without this label GitHub does not auto-link the published package to this
# repo. The package then gets created unlinked on first publish, and
# GITHUB_TOKEN is denied write_package against it - the exact failure in #534,
# and the failure the first main-branch run of build-lingarr hit.
LABEL org.opencontainers.image.source=https://github.com/SpyrosPsarras/epaflix
WORKDIR /app
COPY --from=build /app/publish .
