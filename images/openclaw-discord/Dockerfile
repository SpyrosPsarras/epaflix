FROM ghcr.io/openclaw/openclaw:2026.11.3@sha256:e73f689d663567f0fe3c5a85c54beb3e102dc21f0814bf375e9039ee2d2df51b

# Links the GHCR package to this repo, so GITHUB_TOKEN may publish it.
LABEL org.opencontainers.image.source=https://github.com/SpyrosPsarras/epaflix

# renovate: datasource=github-releases depName=cli/cli
ARG GH_VERSION=2.104.0

USER root

RUN set -eu; \
    dir="gh_${GH_VERSION}_linux_$(dpkg --print-architecture)"; \
    url="https://github.com/cli/cli/releases/download/v${GH_VERSION}"; \
    mkdir /tmp/gh; cd /tmp/gh; \
    curl -fsSLO "$url/$dir.tar.gz"; \
    curl -fsSLO "$url/gh_${GH_VERSION}_checksums.txt"; \
    sha256sum --check --ignore-missing "gh_${GH_VERSION}_checksums.txt"; \
    tar -xzf "$dir.tar.gz"; \
    install -m 0755 "$dir/bin/gh" /usr/local/bin/gh; \
    cd /; rm -rf /tmp/gh; \
    gh --version

# The windmill agent runs the workspace tests in a venv, and Debian ships ensurepip separately.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-venv \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/openclaw-plugins
COPY package.json package-lock.json .npmrc ./
# npm ci rejects the uuid 14 that @openclaw/discord bundles for an SDK asking ^11, so install from the lock and fail if npm changed it.
RUN sha256sum package-lock.json > /tmp/lock.sha256 \
 && npm install --ignore-scripts --no-audit --no-fund --cache /tmp/npm-cache \
 && sha256sum --check /tmp/lock.sha256 \
 && rm /tmp/lock.sha256 \
 && ln -s /app node_modules/openclaw \
 && rm -rf /tmp/npm-cache

COPY render.mjs /opt/openclaw-discord/render.mjs

WORKDIR /app
USER 1000
