FROM python:3.15-alpine

LABEL org.opencontainers.image.source=https://github.com/SpyrosPsarras/epaflix

RUN apk add --no-cache openssh-client git \
    && adduser -D -H -u 1000 -h /tmp rackpeek

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
COPY images/rackpeek-sync/build.py images/rackpeek-sync/sync.py images/rackpeek-sync/fixtures.py images/rackpeek-sync/test_build.py images/rackpeek-sync/test_sync.py /app/
COPY 1-proxmox/rackpeek-scan/rackpeek-scan.py /app/rackpeek_scan.py

USER 1000
ENTRYPOINT ["python3", "-u", "/app/sync.py"]
