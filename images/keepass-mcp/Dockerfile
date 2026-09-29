# keepass-mcp - MCP tools over a DeltaSync-backed KeePass vault.
# Design: docs/superpowers/specs/2026-09-29-keepass-deltasync-design.md
#
# The build stage runs the tests, so a red test fails the image build and CI
# never publishes it.
FROM golang:1.26 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go test ./... \
 && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /keepass-mcp .

FROM gcr.io/distroless/static:nonroot

# Without this label GitHub does not auto-link the published package to this
# repo, and GITHUB_TOKEN is then denied write_package against it (see #534).
LABEL org.opencontainers.image.source=https://github.com/SpyrosPsarras/epaflix

COPY --from=build /keepass-mcp /keepass-mcp
USER nonroot:nonroot
ENTRYPOINT ["/keepass-mcp"]
