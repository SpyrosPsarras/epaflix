#!/usr/bin/env bash
set -euo pipefail
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fsSL https://releases.hashicorp.com/terraform/1.11.4/terraform_1.11.4_linux_amd64.zip -o "$work/terraform.zip"
curl -fsSL https://releases.hashicorp.com/terraform/1.11.4/terraform_1.11.4_SHA256SUMS -o "$work/checksums"
(cd "$work" && checksum=$(grep ' terraform_1.11.4_linux_amd64.zip$' checksums | cut -d' ' -f1) && printf '%s  terraform.zip\n' "$checksum" | sha256sum -c -)
unzip -q "$work/terraform.zip" terraform -d "$work"
install -m 0755 "$work/terraform" /usr/local/bin/terraform
curl -fsSL https://github.com/terraform-linters/tflint/releases/download/v0.64.0/tflint_linux_amd64.zip -o "$work/tflint.zip"
printf '%s  %s\n' cca9d13e2e1d7a2c627af60ff899a3c9b74212899416aeb96ec764d2ef954537 "$work/tflint.zip" | sha256sum -c -
unzip -q "$work/tflint.zip" -d "$work/tflint"
install -m 0755 "$work/tflint/tflint" /usr/local/bin/tflint
curl -fsSL https://github.com/Azure/kubelogin/releases/download/v0.2.19/kubelogin-linux-amd64.zip -o "$work/kubelogin.zip"
printf '%s  %s\n' ebaeff02aa899c5cae6a2b954b64fc02738185319df2570f7dc053451efa4b2f "$work/kubelogin.zip" | sha256sum -c -
unzip -q "$work/kubelogin.zip" -d "$work/kubelogin"
install -m 0755 "$work/kubelogin/bin/linux_amd64/kubelogin" /usr/local/bin/kubelogin
curl -fsSL https://github.com/astral-sh/uv/releases/download/0.12.19/uv-x86_64-unknown-linux-gnu.tar.gz -o "$work/uv.tar.gz"
printf '%s  %s\n' 23bf5552d220e0842b65c862097b2ebaeba0064b74eda5e565e77fd25969d8c8 "$work/uv.tar.gz" | sha256sum -c -
tar -xzf "$work/uv.tar.gz" -C "$work"
install -m 0755 "$work/uv-x86_64-unknown-linux-gnu/uv" "$work/uv-x86_64-unknown-linux-gnu/uvx" /usr/local/bin/
apt-get update
apt-get install -y --no-install-recommends shellcheck
rm -rf /var/lib/apt/lists/*
terraform --version
tflint --version
kubelogin --version
uv --version
shellcheck --version
