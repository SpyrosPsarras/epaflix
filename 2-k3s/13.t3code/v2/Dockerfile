ARG BASE_IMAGE
FROM rust:1-bookworm@sha256:93ce27a88655056a51dbdd8f5f2d7ddc071c7b0070fb288a37b5a285fc83971e AS rust-toolchain
FROM node:26-bookworm@sha256:2aaae6d91f99fee84cfc92da9b52c22a185752d247746052bbc3f961e44478c6 AS build
ARG UPSTREAM_REVISION
WORKDIR /src
COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup
ENV CARGO_HOME=/usr/local/cargo RUSTUP_HOME=/usr/local/rustup
ENV PATH=/usr/local/cargo/bin:/src/node_modules/.bin:$PATH
RUN git clone --filter=blob:none --no-checkout https://github.com/pingdotgg/t3code.git . \
    && git checkout --detach "$UPSTREAM_REVISION" \
    && test "$(git rev-parse HEAD)" = "$UPSTREAM_REVISION"
RUN npm install --global pnpm@11.10.0 \
    && pnpm install --frozen-lockfile \
    && pnpm exec vp run --filter t3 build:bundle \
    && pnpm exec vp run --filter @t3tools/web build \
    && cp -a apps/web/dist apps/server/dist/client \
    && cargo build --locked --release --manifest-path native/resource-monitor/Cargo.toml \
    && mkdir -p /tmp/monitor/linux-x64 \
    && cp native/resource-monitor/target/release/t3-resource-monitor /tmp/monitor/linux-x64 \
    && node apps/server/scripts/cli.ts build-exe \
    && version=$(node -p 'JSON.parse(require("fs").readFileSync("apps/server/package.json")).version') \
    && node scripts/build-cli-archive.ts --platform linux --arch x64 --version "$version" --resource-monitor-dir /tmp/monitor --output-dir /tmp/archive \
    && mkdir /runtime \
    && tar -xzf "/tmp/archive/t3-$version-linux-x64.tar.gz" -C /runtime --strip-components=1

FROM ${BASE_IMAGE}
ARG CONFIG_INPUT_HASH
LABEL t3code.epaflix.com/config-input-hash=$CONFIG_INPUT_HASH
USER root
COPY --from=build /runtime /runtime
COPY one/files/ /scripts/
COPY v2/files/ /v2/
COPY v2/projects.json /v2/projects.json
COPY v2/probe/smoke.mjs /v2/smoke.mjs
COPY v2/tools/install-extra-tools.sh /tmp/install-extra-tools.sh
COPY v2/tools/full-smoke.py /v2/full-smoke.py
RUN bash /tmp/install-extra-tools.sh
RUN ln -sf /v2/t3 /tools/node_modules/.bin/t3 \
    && ln -sf /v2/t3 /usr/local/bin/t3 \
    && chmod 0755 /v2/t3 /v2/entrypoint.sh
ENV PATH=/v2:/usr/local/bin:/tools/node_modules/.bin:$PATH
USER 1000:1000
CMD ["bash", "/v2/entrypoint.sh"]
