# vpn-picker scorer - reads the AirVPN status API, ICMP-probes a shortlist from
# outside any VPN netns, and publishes a ranked top 5.
#
# Components 1-3 of
# docs/superpowers/specs/2026-08-01-vpn-picker-design.md
#
# Deliberately NOT the airvpn-bluetit image: the scorer never talks to Bluetit
# and needs no Suite binaries.
FROM python:3.14-alpine

# Without this label GitHub does not auto-link the published package to this
# repo. The package then gets created unlinked on first publish, and
# GITHUB_TOKEN is denied write_package against it - the exact failure in #534.
LABEL org.opencontainers.image.source=https://github.com/SpyrosPsarras/epaflix

# iputils-ping, not busybox ping: busybox prints a different summary block and
# has no -W. With `capabilities.drop: ["ALL"]` iputils cannot open a raw socket
# and falls back to an unprivileged SOCK_DGRAM ICMP socket, which the pod netns
# permits - see the ping_group_range comment in the Deployment.
#
# No pip install: the app is stdlib only, so there is no requirements layer and
# no wheel supply chain to watch.
RUN apk add --no-cache iputils-ping

WORKDIR /app
COPY vpn_picker.py selftest.py /app/

ENTRYPOINT ["python3", "-u", "/app/vpn_picker.py"]
