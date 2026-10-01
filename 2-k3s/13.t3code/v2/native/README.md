# Arch native v2 client

The package uses separate `/opt/t3code-v2`, `/usr/bin/t3code-v2`, desktop entry and `~/.t3-v2` state. It declares no conflict with `t3code-nightly-bin`.

Build the desktop artifact from the server's recorded upstream revision, applying `patch-client.py` before compilation. Build upstream's `.deb`, extract its application directory, archive it as `client.tar.gz` with root directory `client`, and run makepkg with the wrapper and desktop entry alongside it. The workflow replaces PKGBUILD checksums with measured artifact checksums before packaging.

The packaging patch disables both Linux URI-registration paths when the wrapper sets `T3CODE_V2_NO_URI_REGISTRATION=1`. V1 keeps ownership of `t3code://` links. Pair v2 manually through Settings → Connections → Add environment. The embedded application's internal protocol is unchanged.

In-app updates are disabled; install reviewed package updates through pacman. Build, installation, simultaneous-client operation and v1 link-handler preservation must pass before native acceptance. This source directory is not an already-built package.
