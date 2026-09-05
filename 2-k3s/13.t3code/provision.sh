#!/usr/bin/env bash
# Provisions the t3code guest from a fresh Debian 13 LXC. Run as root INSIDE
# the guest, after create-lxc.sh (B1). Secrets are supplied via environment at
# run time (B2 decrypts them); the script is re-runnable.
#
# Required env:
#   T3_GUEST_IP          the guest's own static IP (t3 serve binds to it)
#   ANTHROPIC_AUTH_TOKEN existing cliproxy client api-key from 17.remote-pi sops secret
#   VAULT_PASSPHRASE     KeePassXC master passphrase from 2-k3s/13.t3code/vault-passphrase.enc.yaml
# Optional env:
#   T3_USER              default: spyros
#   CLIPROXY_BASE_URL    default: https://cliproxy.epaflix.com
#   KEEPASS_KDBX         default: /home/<user>/sync/keepass.kdbx (B4 pairs the Syncthing folder)
# Runs from the guest's clone of the repo (/opt/epaflix): the same dir holds
# versions.env, update.sh and files/keepass_mcp.py.
set -euo pipefail

T3_USER=${T3_USER:-spyros}
CLIPROXY_BASE_URL=${CLIPROXY_BASE_URL:-https://cliproxy.epaflix.com}
KEEPASS_KDBX=${KEEPASS_KDBX:-/home/$T3_USER/sync/keepass.kdbx}
T3_DIR=$(cd "$(dirname "$0")" && pwd)
SERVER_SRC=$T3_DIR/files/keepass_mcp.py
: "${T3_GUEST_IP:?set T3_GUEST_IP}"
: "${ANTHROPIC_AUTH_TOKEN:?set ANTHROPIC_AUTH_TOKEN}"
: "${VAULT_PASSPHRASE:?set VAULT_PASSPHRASE}"
[[ -f $SERVER_SRC ]] || { echo "missing $SERVER_SRC; run from the repo clone at /opt/epaflix"; exit 1; }

# sudo -u drops XDG_RUNTIME_DIR, which breaks every systemd --user call.
as_user() {
  sudo -u "$T3_USER" -H env XDG_RUNTIME_DIR="/run/user/$(id -u "$T3_USER")" "$@"
}

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  curl ca-certificates gnupg git sudo unzip openssh-server syncthing gh python3-venv \
  python3-yaml build-essential polkitd

systemctl enable --now ssh

# Node 24 (t3 code needs ^22.16 || ^23.11 || >=24.10)
curl -fsSL https://deb.nodesource.com/setup_24.x | bash -
DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs

curl -fsSL https://aka.ms/InstallAzureCLIDeb | bash

# kubectl apt repo tracks the cluster's minor (k3s v1.36).
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.36/deb/Release.key | gpg --dearmor --yes -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.36/deb/ /' >/etc/apt/sources.list.d/kubernetes.list
apt-get update

# Pinned tooling (t3, claude, opencode, helm, kustomize, argocd, sops) comes
# from update.sh, which a daily timer re-runs after Renovate's window so a
# merged bump lands on the guest the same morning.
bash "$T3_DIR/update.sh"
cat >/etc/systemd/system/t3code-update.service <<EOF
[Unit]
Description=Pull the repo and apply t3code pinned versions
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=T3_USER=$T3_USER
ExecStart=/usr/bin/git -C $T3_DIR pull --ff-only -q
ExecStart=/usr/bin/bash $T3_DIR/update.sh
EOF
cat >/etc/systemd/system/t3code-update.timer <<'EOF'
[Unit]
Description=Daily t3code update, after Renovate's 02:00-06:00 window

[Timer]
OnCalendar=*-*-* 06:30:00 Europe/Athens
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now t3code-update.timer

if ! id "$T3_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash -G sudo "$T3_USER"
fi

install -d -o "$T3_USER" -g "$T3_USER" /home/"$T3_USER"/projects /home/"$T3_USER"/sync

# wizard-b2 copies the epaflix-only kubeconfig and the sops age key here.
# keys.txt is sops' default key path, so plain `sops -d` works with no env.
as_user mkdir -p /home/"$T3_USER"/.kube /home/"$T3_USER"/.config/sops/age
chmod 0700 /home/"$T3_USER"/.kube /home/"$T3_USER"/.config/sops/age
as_user ln -sfn k3s-cluster.txt /home/"$T3_USER"/.config/sops/age/keys.txt

# One root-owned env file feeds the service and the keepass MCP wrapper.
# Values are single-quoted with '\'' escaping, safe for both the sh wrapper
# sourcing the file and systemd's EnvironmentFile parser.
sq() { local s=${1//\'/\'\\\'\'}; printf "'%s'" "$s"; }
for v in "$ANTHROPIC_AUTH_TOKEN" "$VAULT_PASSPHRASE"; do
  [[ $v != *$'\n'* ]] || { echo "passphrase/token must not contain newlines (env-file delivery)"; exit 1; }
done
install -d -m 0750 -o root -g "$T3_USER" /etc/t3code
install -m 0640 /dev/null /etc/t3code/t3code.env
cat >/etc/t3code/t3code.env <<EOF
ANTHROPIC_BASE_URL=$(sq "$CLIPROXY_BASE_URL")
ANTHROPIC_AUTH_TOKEN=$(sq "$ANTHROPIC_AUTH_TOKEN")
KEEPASS_DB=$(sq "$KEEPASS_KDBX")
KEEPASS_PASSPHRASE=$(sq "$VAULT_PASSPHRASE")
EOF
chown root:"$T3_USER" /etc/t3code/t3code.env

# T3 Code runs as a systemd user service via its own installer (creates the
# unit, owns updates). Linger first as root so the user manager exists even on
# a headless box with no active session; our drop-in binds the LAN IP
# (T3CODE_HOST) and feeds provider creds from the root-owned env file.
loginctl enable-linger "$T3_USER"
systemctl enable --now polkit
cat >/etc/polkit-1/rules.d/49-t3code-self-linger.rules <<EOF
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.login1.set-self-linger" && subject.user == "$T3_USER") {
        return polkit.Result.YES;
    }
});
EOF
as_user bash -c "cd /home/$T3_USER && exec t3 service install"
SERVICE_DIR=/home/$T3_USER/.config/systemd/user/t3code.service.d
install -d -o "$T3_USER" -g "$T3_USER" "$SERVICE_DIR"
cat >"$SERVICE_DIR/override.conf" <<EOF
[Service]
EnvironmentFile=/etc/t3code/t3code.env
Environment=T3CODE_HOST=$T3_GUEST_IP
EOF
chown "$T3_USER":"$T3_USER" "$SERVICE_DIR/override.conf"
as_user systemctl --user daemon-reload
as_user systemctl --user enable --now t3code.service

# Read-only keepass MCP server: pykeepass + official mcp SDK in a dedicated venv.
python3 -m venv /opt/keepass-mcp
/opt/keepass-mcp/bin/pip install -q pykeepass mcp
install -m 0755 "$SERVER_SRC" /opt/keepass-mcp/keepass_mcp.py
/opt/keepass-mcp/bin/python /opt/keepass-mcp/keepass_mcp.py --selftest
cat >/usr/local/bin/keepass-mcp <<'WRAPPER'
#!/bin/sh
. /etc/t3code/t3code.env
exec env KEEPASS_DB="$KEEPASS_DB" KEEPASS_PASSPHRASE="$KEEPASS_PASSPHRASE" \
  /opt/keepass-mcp/bin/python /opt/keepass-mcp/keepass_mcp.py "$@"
WRAPPER
chmod 0755 /usr/local/bin/keepass-mcp
sudo -u "$T3_USER" -H claude mcp remove -s user keepass >/dev/null 2>&1 || true
sudo -u "$T3_USER" -H claude mcp add -s user keepass -- /usr/local/bin/keepass-mcp

echo "Done. Next (B3): gh auth login + az login --use-device-code as $T3_USER, then verify with t3 service status."
