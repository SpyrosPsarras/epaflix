ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG UPSTREAM_RELEASE
ARG UPSTREAM_ARCHIVE_SHA256
ARG CONFIG_INPUT_HASH
LABEL t3code.epaflix.com/config-input-hash=$CONFIG_INPUT_HASH
LABEL t3code.epaflix.com/upstream-release=$UPSTREAM_RELEASE
USER root
RUN test -n "$UPSTREAM_RELEASE" && test -n "$UPSTREAM_ARCHIVE_SHA256" \
    && curl -fsSL -o /tmp/t3.tar.gz "https://github.com/pingdotgg/t3code/releases/download/$UPSTREAM_RELEASE/t3-${UPSTREAM_RELEASE#v}-linux-x64.tar.gz" \
    && echo "$UPSTREAM_ARCHIVE_SHA256  /tmp/t3.tar.gz" | sha256sum -c - \
    && mkdir -p /runtime \
    && tar -xzf /tmp/t3.tar.gz -C /runtime --strip-components=1 --no-same-owner \
    && rm /tmp/t3.tar.gz
COPY one/files/ /scripts/
COPY v2/files/ /v2/
COPY v2/projects.json /v2/projects.json
COPY v2/tools/smoke.mjs /v2/smoke.mjs
COPY v2/tools/install-extra-tools.sh /tmp/install-extra-tools.sh
COPY v2/tools/full-smoke.py /v2/full-smoke.py
RUN bash /tmp/install-extra-tools.sh
RUN ln -sf /v2/t3 /tools/node_modules/.bin/t3 \
    && ln -sf /v2/t3 /usr/local/bin/t3 \
    && chmod 0755 /scripts/opencode-fast-version.sh \
    && chmod 0755 /v2/t3 /v2/entrypoint.sh
ENV PATH=/v2:/usr/local/bin:/tools/node_modules/.bin:$PATH
USER 1000:1000
CMD ["bash", "/v2/entrypoint.sh"]
