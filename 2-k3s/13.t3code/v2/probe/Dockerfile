FROM rust:1-bookworm AS rust-toolchain

FROM node:24-bookworm AS build
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
    && pnpm exec vp run --filter @t3tools/web build
RUN cp -a apps/web/dist apps/server/dist/client \
    && cargo build --locked --release --manifest-path native/resource-monitor/Cargo.toml \
    && mkdir -p /tmp/monitor/linux-x64 \
    && cp native/resource-monitor/target/release/t3-resource-monitor /tmp/monitor/linux-x64 \
    && npm exec --yes --package=node@26 -- node apps/server/scripts/cli.ts build-exe \
    && node scripts/build-cli-archive.ts --platform linux --arch x64 --version 0.0.44 \
       --resource-monitor-dir /tmp/monitor --output-dir /tmp/archive \
    && mkdir /runtime \
    && tar -xzf /tmp/archive/t3-0.0.44-linux-x64.tar.gz -C /runtime --strip-components=1

# Feasibility probe only: test upstream's assembled native archive.
FROM node:24-bookworm
WORKDIR /runtime
COPY --from=build --chown=node:node /runtime /runtime
COPY smoke.mjs /probe/smoke.mjs
ENV PROBE_EXECUTABLE=/runtime/t3
USER node
CMD ["node", "/probe/smoke.mjs"]
