FROM docker.io/library/node:24-bookworm@sha256:64af3819f9275802414d7cdc38c27e9d82bd564dec4d4da87d008255d36c63b4
LABEL org.opencontainers.image.source="https://github.com/SpyrosPsarras/epaflix"

# Build from 2-k3s/13.t3code. Only public tool inputs enter the image.
COPY env/tools/os-packages.txt /tmp/os-packages.txt
RUN apt-get update && xargs -a /tmp/os-packages.txt apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
    && echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ bookworm main' > /etc/apt/sources.list.d/azure-cli.list \
    && curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.36/deb/Release.key | gpg --dearmor -o /usr/share/keyrings/kubernetes.gpg \
    && echo 'deb [signed-by=/usr/share/keyrings/kubernetes.gpg] https://pkgs.k8s.io/core:/stable:/v1.36/deb/ /' > /etc/apt/sources.list.d/kubernetes.list \
    && apt-get update && apt-get install -y --no-install-recommends azure-cli kubectl \
    && rm -rf /var/lib/apt/lists/*

COPY versions.env env/tools/install-cluster-tools.sh /tmp/cluster-tools/
RUN bash /tmp/cluster-tools/install-cluster-tools.sh
COPY env/tools/package.json env/tools/package-lock.json /tools/
# npm 11 drops T3's bundled native package on glibc; npm 10 installs it correctly.
RUN cd /tools && npx --yes npm@10.9.4 ci --no-fund --no-audit && npm cache clean --force
# Run the native server directly so container TERM reaches it without a CLI shim.
RUN ln -sf "../@t3code/t3-$(node -p process.platform)-$(node -p process.arch)/t3" /tools/node_modules/.bin/t3
ENV PATH="/tools/node_modules/.bin:${PATH}"
ENV HOME=/home/t3
RUN mkdir -p /home/t3 && chown 1000:1000 /home/t3
USER 1000:1000
WORKDIR /home/t3
CMD ["bash", "/scripts/entrypoint.sh"]
