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
# Expects files/keepass_mcp.py next to this script (scp both to the guest).
set -euo pipefail

T3_USER=${T3_USER:-spyros}
CLIPROXY_BASE_URL=${CLIPROXY_BASE_URL:-https://cliproxy.epaflix.com}
KEEPASS_KDBX=${KEEPASS_KDBX:-/home/$T3_USER/sync/keepass.kdbx}
SERVER_SRC=$(dirname "$0")/files/keepass_mcp.py
: "${T3_GUEST_IP:?set T3_GUEST_IP}"
: "${ANTHROPIC_AUTH_TOKEN:?set ANTHROPIC_AUTH_TOKEN}"
: "${VAULT_PASSPHRASE:?set VAULT_PASSPHRASE}"
[[ -f $SERVER_SRC ]] || { echo "missing $SERVER_SRC; scp the repo's files/ dir alongside this script"; exit 1; }

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

npm install -g t3 @anthropic-ai/claude-code

curl -fsSL https://aka.ms/InstallAzureCLIDeb | bash

# kubectl tracks the cluster's minor (k3s v1.36); helm/kustomize pins mirror
# ci.yml so what validates here is what the gate validates.
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.36/deb/Release.key | gpg --dearmor --yes -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.36/deb/ /' >/etc/apt/sources.list.d/kubernetes.list
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y kubectl

HELM_VERSION=3.21.4      # renovate: datasource=github-releases depName=helm/helm
KUSTOMIZE_VERSION=5.8.1  # renovate: datasource=github-releases depName=kubernetes-sigs/kustomize
tmp=$(mktemp -d)
helm_tgz="helm-v${HELM_VERSION}-linux-amd64.tar.gz"
curl -fsSL -o "$tmp/$helm_tgz" "https://get.helm.sh/$helm_tgz"
curl -fsSL -o "$tmp/$helm_tgz.sha256sum" "https://get.helm.sh/$helm_tgz.sha256sum"
(cd "$tmp" && sha256sum -c "$helm_tgz.sha256sum")
tar -xzf "$tmp/$helm_tgz" -C "$tmp"
install -m 0755 "$tmp/linux-amd64/helm" /usr/local/bin/helm
curl -fsSL "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv${KUSTOMIZE_VERSION}/kustomize_v${KUSTOMIZE_VERSION}_linux_amd64.tar.gz" | tar -xzf - -C "$tmp"
install -m 0755 "$tmp/kustomize" /usr/local/bin/kustomize
rm -rf "$tmp"
curl -fsSL -o /usr/local/bin/argocd https://github.com/argoproj/argo-cd/releases/latest/download/argocd-linux-amd64
chmod 0755 /usr/local/bin/argocd
SOPS_TAG=$(curl -fsSLo /dev/null -w '%{url_effective}' https://github.com/getsops/sops/releases/latest)
SOPS_TAG=${SOPS_TAG##*/}
curl -fsSL -o /usr/local/bin/sops "https://github.com/getsops/sops/releases/download/${SOPS_TAG}/sops-${SOPS_TAG}.linux.amd64"
chmod 0755 /usr/local/bin/sops

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
