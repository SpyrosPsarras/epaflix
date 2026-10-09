# Sunset KeePass: move agents and desktops to Vaultwarden

Date: 2026-10-03. Status: approved in chat, D1 to D6.

## Goal

Stop running KeePassXC on homePC and the laptop, and the KeePass MCP pod on
k3s, all in one cutover. Vaultwarden (`vaultwarden.epaflix.com`) becomes the
only password vault. Passwords, SSH keys and app secrets keep working for
Spyros and for agents. One archive copy of the KDBX files stays on homePC.

Success means:

- An agent on the t3code pod can list, read, add, update, trash and attach
  vault items through the hub, just as it can with `keepass_*` today.
- homePC and the laptop boot without KeePassXC. Bitwarden unlocks without
  waiting for another app. gh, docker, git, sops, Zen, pi and zed keep their
  credentials.
- No `keepass` pod, secret, hub route or client entry is left in the repo or
  the cluster.

## What KeePass does today

- `syncthing/keepass` pod (`2-k3s/15.syncthing/keepass.yaml`,
  `files/keepass_mcp.py`) serves `Passwords.kdbx` from the Syncthing folder
  `secrets-vault` as an MCP server with 7 `vault_*` tools. mcp-hub exposes it
  at `/keepass`. The t3code client configs register it, and the
  `keepassxc-secrets` skill tells agents to use it.
- On both desktops, KeePassXC autostarts and does three jobs:
  1. Secret Service provider (`[FdoSecrets] Enabled=true`). App tokens live
     in it: Bitwarden's `Bitwarden_biometric` key, gh, docker registries,
     Zen/Firefox storage, copilot, pi oauth, Claude, Slack, vscode,
     teams. Bitwarden asks the Secret Service for its unlock key at login,
     which is why it waited for KeePassXC.
  2. SSH agent. Bitwarden Desktop already replaced it.
  3. The password vault.
- Desktop scripts that read secrets from KeePassXC:
  - `~/.pi/profiles/epaflix/bin/sops-kpx` reads the sops age key from entry
    `sops-age-k3s-cluster`. homePC has no age key file.
  - `~/.local/bin/zed-launcher` (laptop) and three
    `pi-cliproxyapi/config.json` files run `secret-tool lookup Uuid <id>`.
    Only KeePassXC sets the `Uuid` attribute.
  - `dh-jira/scripts/jira` calls `kpx.sh`.
  - git uses `credential.helper = keepassxc --git-groups`.
- Syncthing syncs 11 folders. Only `secrets-vault` belongs to KeePass, so
  Syncthing stays.

Vaultwarden already holds the import: 337 items against 340 KeePass entries,
and 30 items with attachments on both sides. The 5 passkeys did not import.
The exact name diff waits for D1.

## D1. Vaultwarden MCP for agents

A new Deployment `vaultwarden-mcp` in namespace `mcp-hub`:

- Image `python:3.13-alpine` plus Node with `@bitwarden/cli@2026.8.0`. Use
  the same pin as t3code, because 2026.9.x breaks against Vaultwarden
  1.37.3 (see `.github/renovate.json`).
- Startup: `bw config server`, `bw login --apikey`, `bw unlock` with the
  master password from the environment, then `bw serve --hostname 127.0.0.1`
  inside the pod. `bw serve` is Bitwarden's own local REST API, so the MCP
  server never shells out per call.
- `vaultwarden_mcp.py` is a rewrite of `keepass_mcp.py` against `bw serve`:
  the same 7 tools, names, arguments and outputs. Its HTTP mode serves
  `/vaultwarden` with the `X-Hub-Secret` check.
- Path mapping: `/Folder/Name`. The KeePass import created folders named
  after the KeePass groups, so existing paths keep working. Custom properties
  map to custom fields, `vault_trash` maps to Bitwarden's soft delete (the
  item goes to Trash), and attachments map to attachments.
- Before each read, run `bw sync`, rate-limited to once per 60 s, so changes
  made in Bitwarden Desktop show up.
- Secrets (SOPS, ksops):
  - `vaultwarden-mcp` holds Spyros's personal API key (`client_id`,
    `client_secret`) and master password. Spyros creates it with a wizard
    and never pastes it in chat.
  - `vaultwarden-mcp-hub-secret` is the shared hub secret, replacing
    `keepass-hub-secret`.
- NetworkPolicy: only `mcp-hub` pods may connect. The pod's egress goes to
  Vaultwarden.
- `--selftest` runs in CI against a mocked `bw serve` and covers every tool
  and the HTTP wrapper.

The hub route `/keepass` becomes `/vaultwarden`. `hub_clients.py` (both
copies) registers `vaultwarden` and keeps `ask` on the 4 write tools.
jev-guard exempts `vaultwarden_*` the same way it exempts `keepass_*` now.

Rejected: the official `bitwarden/mcp-server`. It is stdio only, its README
says never to run it in a container, it needs a session token that expires,
and it adds dozens of tools (org admin, Send, device approval) we don't use.

Risk: the cluster holds a full-vault credential. That matches today, since
the cluster already holds the KDBX master password.

## D2. Exact compare before removal

With D1 live, list all Vaultwarden items, diff them by path against the 340
KeePass entries from `vault_list`, and compare attachment names. Show Spyros
the missing items and wait for his OK. Expected gaps: the 5 passkeys
(re-register them by hand from Bitwarden) and the about 60 app-token items,
which belong in gnome-keyring (D3), not in Vaultwarden. Nothing gets deleted
before Spyros confirms.

## D3. Desktops: gnome-keyring replaces KeePassXC

Same steps on homePC and the laptop (Arch, niri, DMS, no KDE):

1. Install `gnome-keyring`. Its dependencies are gcr and glib, not the GNOME
   desktop. Add `pam_gnome_keyring` to the login PAM stack so it unlocks with
   the login password. Enable `gnome-keyring-daemon.socket` for the user.
   niri's portal config already names `gnome-keyring` for the Secret portal.
2. While KeePassXC still runs, copy every Secret Service item that has an
   app attribute (`service`, `application`, `xdg:schema`) into the new
   keyring with the same attributes and secret. Apps keep their logins.
3. Store the few script secrets with plain attributes, for example
   `secret-tool store service cliproxy key api`. Change zed-launcher, the
   three `pi-cliproxyapi/config.json` files and dh-jira to look them up by
   those attributes. The pi files sync through Syncthing, so one edit covers
   both machines.
4. homePC: write the age key to `~/.config/sops/age/k3s-cluster.txt` (0600),
   the way the laptop has it, and delete `sops-kpx` and its `sops` symlink.
5. git: `credential.helper = /usr/lib/git-core/git-credential-libsecret`.
6. Bitwarden Desktop: turn "unlock with system authentication" off and on
   again, so its key lands in gnome-keyring.
7. Remove KeePassXC autostart and uninstall `keepassxc` and
   `git-credential-keepassxc`.
8. `kwallet` stays installed only if another package depends on it. Its
   D-Bus activation file must not take the Secret Service name. Check that
   with `busctl --user status org.freedesktop.secrets` after a reboot.

Spyros reboots each machine himself. Reboot check: no KeePassXC process,
`org.freedesktop.secrets` owned by `gnome-keyring-daemon`, Bitwarden unlocks
by system authentication, and `gh auth status`, `ssh homepc true` and
`sops -d` on one repo file all work.

## D4. Repo PR

- Delete `2-k3s/15.syncthing/keepass.yaml`, `files/keepass_mcp.py`,
  `vault-passphrase.enc.yaml`, `keepass-hub-secret.enc.yaml` and their
  kustomization and ksops entries.
- `2-k3s/23.mcp-hub`: add `vaultwarden-mcp`, remove `/keepass`, change
  `sops_secret.py --keepass` to `--vaultwarden`, swap the CI selftest and
  update the README.
- `2-k3s/13.t3code`: update `hub_clients.py`, jev-guard and its tests,
  `ssh-policy.test.py`, the then-current Jev Auto documentation, `env/README.md` and
  `v2/readiness.md`.
- Fix the KeePassXC hints in `10.observability/deploy.sh`, the authentik
  blueprint hook, and the "never look in KeePass" lines in `homelab-ssh.md`
  (both copies).
- Skill: replace `keepassxc-secrets` (`vault.py`, `kpx.sh`) with
  `vaultwarden-secrets`. The pod path uses the hub tools. The desktop path
  uses `secret-tool` for app tokens and the hub for vault items. Rebuild
  `private-agent-config.enc.yaml`.
- Mark the 2026-09-29 DeltaSync spec and plan as superseded.

ArgoCD prune removes the `keepass` pod after merge.

## D5. Backup and cleanup

1. homePC: copy `Passwords.kdbx`, `kdewallet.kdbx`, `Passwords_backup.kdbx`
   and the sync-conflict file to `~/Documents/keepass-archive-2026-10-03/`,
   outside any Syncthing folder. Open-check with
   `keepassxc-cli ls` before uninstalling.
2. Remove the `secrets-vault` folder from Syncthing on all 3 devices, then
   delete `~/Documents/Secrets` (including `.stversions`) on homePC and the
   laptop and `data/secrets-vault` on the PVC.
3. Laptop: delete the stray copies in `~/Downloads` and `~/Documents`.
4. Remove `~/.config/keepassxc` and `~/.cache/keepassxc` on both desktops.

## D6. Order

D1 → D2 (Spyros confirms) → D3 → D4 → D5. KeePass keeps working until D2
passes. D4 merges after D3, so agents switch only when the new path works.
Each step is verified before the next one starts.

## Out of scope

- Servers: none run KeePassXC. Nothing changes there.
- Cleaning the junk app-token items out of Vaultwarden. Spyros can do that
  later.
- Upgrading Vaultwarden or the `bw` pin.
