FROM python:3.15-alpine

LABEL org.opencontainers.image.source=https://github.com/SpyrosPsarras/epaflix

RUN apk add --no-cache openssh-client git \
    && adduser -D -H -u 1000 -h /tmp homelab

# renovate: datasource=github-releases depName=gitleaks/gitleaks
ARG GITLEAKS_VERSION=8.30.1

RUN set -eu; \
    tar="gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"; \
    url="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}"; \
    mkdir /tmp/gl; cd /tmp/gl; \
    wget -q "$url/$tar" "$url/gitleaks_${GITLEAKS_VERSION}_checksums.txt"; \
    grep " $tar\$" "gitleaks_${GITLEAKS_VERSION}_checksums.txt" | sha256sum -c -; \
    tar -xzf "$tar" gitleaks; \
    install -m 0755 gitleaks /usr/local/bin/gitleaks; \
    cd /; rm -rf /tmp/gl

WORKDIR /app
COPY images/homelab-sync/build.py images/homelab-sync/sync.py images/homelab-sync/fixtures.py images/homelab-sync/test_build.py images/homelab-sync/test_sync.py /app/
COPY 1-proxmox/homelab-scan/homelab-scan.py /app/homelab_scan.py

USER 1000
ENTRYPOINT ["python3", "-u", "/app/sync.py"]
