# AirVPN Suite (Bluetit daemon + Goldcrest client) for use as a VPN sidecar.
#
# Why we build this at all: AirVPN ships only a tarball, there is no official
# image, and Bluetit is the only client that takes a server whitelist/blacklist
# and picks a recommended server. See
# docs/superpowers/specs/2026-07-30-airvpn-bluetit-vpn-layer-design.md
#
# install.sh from the tarball is deliberately NOT used: it prompts about systemd,
# boot-start and creating the airvpn user/group, which hangs a build. We place
# the files ourselves and skip the systemd units entirely.
FROM debian:13-slim

# Without this label GitHub does not auto-link the published package to this
# repo. The package then gets created unlinked on first publish, and
# GITHUB_TOKEN is denied write_package against it - the exact failure in #534,
# where the workflow had never once published successfully and the fix needed
# a manual package-settings change.
LABEL org.opencontainers.image.source=https://github.com/SpyrosPsarras/epaflix

ARG AIRVPN_SUITE_VERSION=2.1.0
ARG AIRVPN_SUITE_SHA512=e17add5769b50683a4d2e480995fbe83d9f4b05b9738de58de9ce922ea80b13317b502ad4a49ee01bd23bcf10b8df96a3242fa3e5f9d20138665373c2445720d

# libsystemd0 and libcap2 are NOT in the README's dependency list but both
# binaries link them - verified with ldd. busybox provides the syslogd that
# forwards Bluetit's logs to stdout; Bluetit has no logfile directive at all,
# so without it the container is completely silent.
#
# python3 is for the vpn-picker agent (agent.py, #608). The full package, not
# python3-minimal: the agent serves its metrics from http.server, which the
# minimal stdlib subset does not carry.
RUN apt-get update && apt-get install -y --no-install-recommends \
      dbus libdbus-1-3 libssl3 libstdc++6 libsystemd0 libcap2 \
      zlib1g libbrotli1 libzstd1 libxml2 python3 \
      iptables iproute2 iputils-ping busybox procps ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/suite.tar.gz \
      "https://gitlab.com/AirVPN/AirVPN-Suite/-/raw/master/binary/AirVPN-Suite-x86_64-${AIRVPN_SUITE_VERSION}.tar.gz" \
 && echo "${AIRVPN_SUITE_SHA512}  /tmp/suite.tar.gz" | sha512sum -c - \
 && tar xzf /tmp/suite.tar.gz -C /tmp \
 && install -m 0755 /tmp/AirVPN-Suite/bin/bluetit   /sbin/bluetit \
 && install -m 0755 /tmp/AirVPN-Suite/bin/goldcrest /usr/local/bin/goldcrest \
 && mkdir -p /etc/airvpn /etc/dbus-1/system.d \
 && cp /tmp/AirVPN-Suite/etc/airvpn/* /etc/airvpn/ \
 && cp /tmp/AirVPN-Suite/etc/dbus-1/system.d/*.conf /etc/dbus-1/system.d/ \
 && cp /tmp/AirVPN-Suite/etc/airvpn/bluetit.rc /etc/airvpn/bluetit.rc.shipped \
 && groupadd -f airvpn \
 && rm -rf /tmp/suite.tar.gz /tmp/AirVPN-Suite

COPY entrypoint.sh /entrypoint.sh
COPY entrypoint-render.sh /entrypoint-render.sh
# Second entrypoint - the vpn-picker agent (#608). Same image because goldcrest
# and the D-Bus client libraries are already here, and a second image to hold
# one binary buys nothing. The manifest picks it with `command`.
COPY agent-entrypoint.sh /agent-entrypoint.sh
COPY agent.py /agent.py
COPY agent_selftest.py /agent_selftest.py
RUN chmod 0755 /entrypoint.sh /entrypoint-render.sh /agent-entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
