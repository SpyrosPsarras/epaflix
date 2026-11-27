# llama-server from PrismML's llama.cpp fork. Only the fork runs the Ternary
# Bonsai PTQ1_0 files; stock llama.cpp and Ollama reject them. The release's
# CUDA 12.8 build needs a driver for CUDA 12.8 or newer and includes Turing
# (RTX 20) kernels since prism-b12220. Move the release and its checksum
# together by hand.
FROM nvidia/cuda:12.8.1-runtime-ubuntu24.04@sha256:ebef3c171eeef0298e4eb2e4be843105edf3b8b0ac45e0b43acee358e8046867

LABEL org.opencontainers.image.source=https://github.com/SpyrosPsarras/epaflix

ARG PRISM_RELEASE=prism-b12232-f702476
ARG PRISM_SHA256=5aeef1a6c543765a488920a771c4cde0931b95f4cb52dfa9aa3c7e0e106d329f

RUN set -eu; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl libgomp1; \
    rm -rf /var/lib/apt/lists/*; \
    curl -fsSL -o /tmp/llama.tgz "https://github.com/PrismML-Eng/llama.cpp/releases/download/${PRISM_RELEASE}/llama-${PRISM_RELEASE}-bin-linux-cuda-12.8-x64.tar.gz"; \
    echo "${PRISM_SHA256}  /tmp/llama.tgz" | sha256sum -c -; \
    mkdir /opt/llama; \
    tar -xzf /tmp/llama.tgz -C /opt/llama --strip-components=1; \
    rm /tmp/llama.tgz

ENV LD_LIBRARY_PATH=/opt/llama
USER 1000
EXPOSE 8080
ENTRYPOINT ["/opt/llama/llama-server"]
