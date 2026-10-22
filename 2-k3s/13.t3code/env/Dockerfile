FROM docker.io/library/node:24-bookworm@sha256:6449e19d8184d84062525b6a7524def03146c2cb04a7f08b076680d0e8e4240a
LABEL org.opencontainers.image.source="https://github.com/SpyrosPsarras/epaflix"
ARG T3_RUNTIME_VARIANT=mixed
RUN case "$T3_RUNTIME_VARIANT" in mixed|homelab|work) ;; *) exit 1 ;; esac
ENV T3_RUNTIME_VARIANT=$T3_RUNTIME_VARIANT

# Build from 2-k3s/13.t3code. Only public tool inputs enter the image.
COPY env/tools/os-packages.txt /tmp/os-packages.txt
# No SSH host private keys in the public image.
RUN apt-get update && xargs -a /tmp/os-packages.txt apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_*
RUN if [ "$T3_RUNTIME_VARIANT" != homelab ]; then \
      curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
      && echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ bookworm main' > /etc/apt/sources.list.d/azure-cli.list; \
    fi \
    && curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.36/deb/Release.key | gpg --dearmor -o /usr/share/keyrings/kubernetes.gpg \
    && echo 'deb [signed-by=/usr/share/keyrings/kubernetes.gpg] https://pkgs.k8s.io/core:/stable:/v1.36/deb/ /' > /etc/apt/sources.list.d/kubernetes.list \
    && apt-get update && apt-get install -y --no-install-recommends kubectl $([ "$T3_RUNTIME_VARIANT" = homelab ] || echo azure-cli) \
    && rm -rf /var/lib/apt/lists/*

COPY versions.env env/tools/install-cluster-tools.sh /tmp/cluster-tools/
RUN bash /tmp/cluster-tools/install-cluster-tools.sh
COPY env/tools/package.json env/tools/package-lock.json /tools/
# npm 11 drops T3's bundled native package on glibc; npm 10 installs it correctly.
RUN cd /tools && npx --no-audit --yes npm@10.9.4 ci --no-fund --no-audit \
    && npm cache clean --force
COPY one/files/cc-safety-net-install.py one/files/cc-safety-net.patch one/files/cc-safety-net-hashes.json /tmp/safety-net-build/
RUN /usr/bin/python3 -I -S /tmp/safety-net-build/cc-safety-net-install.py --image-build \
    && rm -rf /tmp/safety-net-build
# Run the native server directly so container TERM reaches it without a CLI shim.
RUN native="/tools/node_modules/@t3code/t3-$(node -p process.platform)-$(node -p process.arch)/t3"; \
    if [ ! -x "$native" ]; then \
      T3CODE_HOME=/opt/t3 /tools/node_modules/.bin/t3 --version \
      && chmod -R a+rX /opt/t3 \
      && native="/opt/t3/runtime/versions/$(node -p "require('/tools/node_modules/t3/package.json').version")/t3"; \
    fi \
    && test -x "$native" \
    && ln -sf "$native" /tools/node_modules/.bin/t3
ENV PATH="/tools/node_modules/.bin:${PATH}"
COPY env/files/isolated-guidance.md /opt/t3/isolated-guidance.md
COPY env/files/vault-client.py /tmp/vault-client.py
RUN if [ "$T3_RUNTIME_VARIANT" = homelab ] || [ "$T3_RUNTIME_VARIANT" = work ]; then install -m 0555 /tmp/vault-client.py /usr/local/bin/vault; fi \
    && rm /tmp/vault-client.py
ENV HOME=/home/t3
RUN mkdir -p /home/t3 && chown 1000:1000 /home/t3 \
    && if [ "$T3_RUNTIME_VARIANT" != mixed ]; then mkdir -m 0700 /etc/codex && chown 1000:1000 /etc/codex; fi
USER 1000:1000
WORKDIR /home/t3
CMD ["bash", "/scripts/entrypoint.sh"]
