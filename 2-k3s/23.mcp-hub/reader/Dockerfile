FROM docker.io/library/node:22.22.3-bookworm-slim@sha256:e21fc383b50d5347dc7a9f1cae45b8f4e2f0d39f7ade28e4eef7d2934522b752 AS client
WORKDIR /tools
COPY reader/package.json reader/package-lock.json ./
RUN npm ci --no-audit --no-fund --userconfig=/dev/null && npm cache clean --force
COPY files/reader_bw_bridge.cjs /tools/reader_bw_bridge.cjs
RUN chmod 0555 /tools/reader_bw_bridge.cjs && /tools/reader_bw_bridge.cjs --reader-capabilities

FROM docker.io/library/python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
LABEL org.opencontainers.image.source="https://github.com/SpyrosPsarras/epaflix"
COPY --from=client /usr/local/bin/node /usr/local/bin/node
COPY --from=client /tools /tools
COPY reader/requirements.txt /opt/requirements.txt
RUN python -m pip install --require-hashes --no-cache-dir --disable-pip-version-check -r /opt/requirements.txt \
    && groupadd --gid 1000 reader && useradd --uid 1000 --gid 1000 --no-create-home reader \
    && mkdir /run/vault-reader /run/vault-reader-inputs && chown 1000:1000 /run/vault-reader
COPY files/vaultwarden_reader.py files/vaultwarden_reader_http.py files/reader_startup.py /app/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/run/vault-reader/private/scratch TMPDIR=/run/vault-reader/private/scratch
USER 1000:1000
WORKDIR /app
ENTRYPOINT ["python", "/app/reader_startup.py"]
