# PegaProx auto-update

For Debian 13 trixie with PegaProx in `/opt/PegaProx`. The timer runs daily at 04:00 host time. The host uses UTC. `Persistent=true` catches missed runs after boot.

The root-run wrapper does nothing when the installed version is already the latest release. Otherwise, it updates only when main's version matches GitHub's latest release. It answers `y` to the existing updater, then requires the release version and an active service with the same MainPID after 30 seconds. Network, parse and updater errors fail the run. The APT drop-ins enable Debian security updates on APT's schedule.

`update.sh` downloads mutable main or its mirror, so main can change between the check and download. The final version check catches a version mismatch afterwards.

## Install

Copy this directory to the host. Run as root from that directory:

```sh
apt update
apt install unattended-upgrades
install -m 0755 pegaprox-auto-update.sh /usr/local/sbin/pegaprox-auto-update
install -m 0644 pegaprox-auto-update.service pegaprox-auto-update.timer /etc/systemd/system/
install -m 0644 20auto-upgrades 99pegaprox-security /etc/apt/apt.conf.d/
systemctl daemon-reload
systemctl enable --now pegaprox-auto-update.timer
```

## Check

```sh
systemctl list-timers --all pegaprox-auto-update.timer 'apt-daily*.timer'
systemctl status pegaprox-auto-update.service pegaprox.service
journalctl -u pegaprox-auto-update.service -n 100 --no-pager
unattended-upgrade --dry-run --debug
```

Confirm the dry run allows only the trixie security origin. Run the offline self-check with `bash pegaprox-auto-update.test.sh`.

## Roll back

Disable the timer with `systemctl disable --now pegaprox-auto-update.timer`. Let any running update finish, then stop `pegaprox.service`. Restore the chosen backup from `/opt/PegaProx/backups/` into `/opt/PegaProx`, preserving ownership, and restart `pegaprox.service`.
