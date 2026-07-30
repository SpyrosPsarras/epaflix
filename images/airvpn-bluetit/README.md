# airvpn-bluetit

AirVPN Suite (Bluetit daemon + Goldcrest client) packaged as a container image, for
use as a VPN sidecar. Built from `Dockerfile` in this directory.

## Why build this at all

AirVPN ships the suite only as a tarball - there is no official image. Bluetit is
the only AirVPN client that takes a server whitelist/blacklist and picks a
recommended server; a plain WireGuard config cannot fail over to another server on
its own. See `docs/superpowers/specs/2026-07-30-airvpn-bluetit-vpn-layer-design.md`
for the full design.

## install.sh is bypassed

The tarball's `install.sh` is not run. It prompts interactively about installing
systemd units, enabling boot-start, and creating the `airvpn` user/group - any of
which hangs a non-interactive `docker build`. This Dockerfile places the two
binaries (`bluetit`, `goldcrest`) and the shipped `/etc/airvpn` and
`/etc/dbus-1/system.d` files itself, and skips the systemd units entirely - the
entrypoint runs `dbus-daemon` and `bluetit` directly in the foreground.

## The syslog forwarder is mandatory

Bluetit logs **only** to syslog - it has no `logfile` directive at all. Without a
syslog daemon forwarding to stdout, a perfectly healthy container produces zero
output and looks like a silent crash. `entrypoint.sh` runs
`busybox syslogd -n -O /dev/stdout` for exactly this reason. Do not remove it.

## Config rendering

`entrypoint-render.sh` builds `/etc/airvpn/bluetit.rc` on every start from:
1. The pristine shipped template (`bluetit.rc.shipped`, carries the required
   `bootserver`/`rsaexponent`/`rsamodulus` directives) - always start from this
   copy so a container restart never appends the fragment twice.
2. An optional config fragment at `/config/bluetit.conf` (or `$BLUETIT_CONFIG`).
3. `AIRVPN_USERNAME` / `AIRVPN_PASSWORD` as `airusername` / `airpassword`
   directives, only emitted when the env var is non-empty - Bluetit's rc parser
   rejects a directive with no value at all ("Error while parsing ... file.
   Exiting."), so a credential-less boot (as used by our own safety test) must
   omit them rather than emit them empty.

## How to bump the AirVPN Suite version

1. Get the new tarball's sha512 from AirVPN's published `<tarball>.sha512` file
   (not from the tarball itself - verify against the value AirVPN publishes).
2. Update `AIRVPN_SUITE_VERSION` and `AIRVPN_SUITE_SHA512` in the Dockerfile.
3. Rebuild and run `./test.sh` - check 1 catches a shared-library regression if
   the new binary links something not in the current package list.

There is a `customManager` regex on `ARG AIRVPN_SUITE_VERSION` in
`.github/renovate.json`, so version bumps ARE detected automatically. Since
`AirVPN/AirVPN-Suite` has no git tags or GitLab releases at all - only tarballs
committed straight to `master` under `binary/` (see
[#487](https://github.com/SpyrosPsarras/epaflix/issues/487)) - the datasource is
a `custom.airvpn-suite-x86_64` entry (also in `.github/renovate.json`) that
lists that directory via the GitLab repository-tree API and reads the version
straight out of the `AirVPN-Suite-x86_64-<version>.tar.gz` filename (the
non-legacy x86_64 build specifically - the directory also holds `-legacy` and
aarch64/armv7l/hummingbird-macos-* variants that must not be mistaken for a
version bump).

Renovate can still only ever bump the `AIRVPN_SUITE_VERSION` line - it cannot
compute or fetch the sha512, so `AIRVPN_SUITE_SHA512` is always left pointing
at the old tarball's checksum. The build step in the Dockerfile verifies the
checksum and fails closed rather than build against a mismatched binary, so
every Renovate-opened PR for this still needs step 1 above (fetch the new
sha512) done by hand before it can merge.

## Running the tests

```bash
cd images/airvpn-bluetit
docker build -t airvpn-bluetit:dev .
./test.sh
```

Expected: `ALL CHECKS PASSED`. `test.sh` never mounts real AirVPN credentials or a
config fragment with `airconnectatboot`, so it cannot dial out to AirVPN - safe to
run even while another instance holds the live session on the same `Default` key.
