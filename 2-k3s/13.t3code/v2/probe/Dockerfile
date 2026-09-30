FROM node:24-bookworm AS build
ARG UPSTREAM_REVISION
WORKDIR /src
RUN git clone --filter=blob:none --no-checkout https://github.com/pingdotgg/t3code.git . \
    && git checkout --detach "$UPSTREAM_REVISION" \
    && test "$(git rev-parse HEAD)" = "$UPSTREAM_REVISION"
RUN npx --yes pnpm@11.10.0 install --frozen-lockfile \
    && npx --yes pnpm@11.10.0 exec vp run --filter t3 build:bundle \
    && npx --yes pnpm@11.10.0 exec vp run --filter @t3tools/web build

# Feasibility probe only: retain the dependency tree to test native loading.
# Production packaging must separately prove a smaller locked runtime closure.
FROM node:24-bookworm
WORKDIR /src
COPY --from=build --chown=node:node /src /src
COPY smoke.mjs /probe/smoke.mjs
USER node
CMD ["node", "/probe/smoke.mjs"]
