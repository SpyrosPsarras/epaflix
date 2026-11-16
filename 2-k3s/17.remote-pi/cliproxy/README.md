# CLIProxyAPI

Git owns the deployment only: the image, plugin binaries, env, mounts and secrets.
The proxy config and credentials live in the `cliproxy` Postgres database. Change
them in the management panel. To restore them, recover the CNPG cluster to a point
in time.

On a fresh database without a backup, set `plugins.enabled: true` in the panel.
The default is `false`, which loads no plugins.

Plugins come from the `fetch-*` initContainers in `deployment.yaml`, because the
plugins volume is an emptyDir and a plugin installed from the store is gone after
a restart. Each plugin's settings live in `plugins.configs.<id>` in the panel.

Plugin state that must survive a restart goes under `/var/lib/cliproxy-plugins`, a
persistent volume. For kpatdev-quota-router, set `state_dir` to
`/var/lib/cliproxy-plugins/kpatdev-quota-router`, then restart the cliproxy pod.
The plugin reads `state_dir` only at startup.
