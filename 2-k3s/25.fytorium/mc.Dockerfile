FROM golang:1.24-bookworm AS client-build
RUN git clone --depth 1 --branch RELEASE.2025-04-16T18-13-26Z https://github.com/minio/mc.git /src
WORKDIR /src
RUN CGO_ENABLED=0 go build -o /mc .

FROM alpine:3.24 AS client
COPY --from=client-build /mc /usr/local/bin/mc
ENTRYPOINT ["mc"]
