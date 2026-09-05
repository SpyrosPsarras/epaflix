#!/usr/bin/env bash
#
# B2+B3 wizard — sops secrets, guest provisioning, provider logins.
# Run after wizard-b1.sh (reads <repo>/.env). Needs the age private key on this
# machine for sops. Secret values are captured hidden and never printed.
#
# Everything above the "STAGES" marker is the wizard library: do not hand-edit
# it. Author the per-step stages below the marker.

set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────
# Wizard library — delightful, consistent UX. Identical across every wizard.
# ──────────────────────────────────────────────────────────────────────────

if [[ -t 1 ]] && command -v tput >/dev/null 2>&1 && [[ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]]; then
  BOLD=$(tput bold); DIM=$(tput dim); RESET=$(tput sgr0)
  BLUE=$(tput setaf 4); GREEN=$(tput setaf 2); YELLOW=$(tput setaf 3); RED=$(tput setaf 1)
else
  BOLD=""; DIM=""; RESET=""; BLUE=""; GREEN=""; YELLOW=""; RED=""
fi

# Author sets this at the top of the stages section.
TOTAL_STAGES=0

_STAGE_INDEX=0
ENV_FILE="${ENV_FILE:-.env}"
WRITTEN_ENV=()    # KEYs written to ENV_FILE this run
WRITTEN_SECRET=() # secret NAMEs set this run
SKIPPED=()        # things we couldn't do (e.g. gh missing)

# _clear — wipe the terminal so only the current step is on screen. No-op when
# output isn't a terminal, so piped logs stay readable.
_clear() {
  [[ -t 1 ]] || return 0
  if command -v tput >/dev/null 2>&1; then tput clear; else printf '\033[2J\033[3J\033[H'; fi
}

# banner "Title" — opening frame: what this wizard does.
banner() {
  _clear
  printf '\n%s%s  %s%s\n' "$BOLD" "$BLUE" "$1" "$RESET"
  printf '%s  %s stages%s\n\n' "$DIM" "$TOTAL_STAGES" "$RESET"
  printf '%s  You drive the browser; this wizard tells you exactly what to do and\n' "$DIM"
  printf '  captures the values you copy back. Stop any time with Ctrl-C and re-run\n'
  printf '  later — it remembers values already saved.%s\n' "$RESET"
  pause "Ready to start?"
}

# stage "Name" — clear the screen, then announce a stage and show progress.
# Clearing keeps only the current step on screen.
stage() {
  _clear
  _STAGE_INDEX=$((_STAGE_INDEX + 1))
  printf '\n%s%s▸ Stage %s/%s · %s%s\n' \
    "$BOLD" "$BLUE" "$_STAGE_INDEX" "$TOTAL_STAGES" "$1" "$RESET"
}

# say "..." — a plain instruction line.
say()  { printf '  %s\n' "$1"; }
# step "..." — a numbered-feeling action the human takes in the browser.
step() { printf '  %s•%s %s\n' "$BLUE" "$RESET" "$1"; }
note() { printf '  %s%s%s\n' "$DIM" "$1" "$RESET"; }
warn() { printf '  %s⚠ %s%s\n' "$YELLOW" "$1" "$RESET"; }

# open_url URL — open in the human's browser, cross-platform incl. WSL.
open_url() {
  local url="$1"
  printf '  %s↗ opening%s %s\n' "$GREEN" "$RESET" "$url"
  { if   command -v wslview     >/dev/null 2>&1; then wslview "$url"
    elif command -v explorer.exe >/dev/null 2>&1; then explorer.exe "$url"
    elif command -v xdg-open    >/dev/null 2>&1; then xdg-open "$url"
    elif command -v open        >/dev/null 2>&1; then open "$url"
    else warn "couldn't open a browser — visit it manually: $url"; fi
  } >/dev/null 2>&1 || warn "couldn't open a browser — visit it manually: $url"
}

# pause "msg" — wait for the human to confirm they've done the manual part.
pause() {
  printf '  %s%s%s ' "$DIM" "${1:-Press Enter to continue}" "$RESET"
  read -r _ || true
}

# confirm "question" — y/N gate; returns success on yes.
confirm() {
  local reply=""
  printf '  %s? %s [y/N] ' "$YELLOW" "$1"
  read -r reply || true
  [[ "$reply" =~ ^[Yy] ]]
}

# _existing KEY — current value of KEY in ENV_FILE, if any.
_existing() {
  [[ -f "$ENV_FILE" ]] || return 1
  local line; line=$(grep -E "^${1}=" "$ENV_FILE" | tail -n1) || return 1
  printf '%s' "${line#*=}"
}

# ask KEY "Prompt" — read a value into $KEY. Offers the existing .env value as
# a default on re-runs (Enter keeps it). Visible input (non-secret).
ask() {
  local key="$1" prompt="$2" current input
  current=$(_existing "$key" || true)
  if [[ -n "$current" ]]; then
    printf '  %s%s%s %s[Enter keeps current]%s ' "$BOLD" "$prompt" "$RESET" "$DIM" "$RESET"
  else
    printf '  %s%s%s ' "$BOLD" "$prompt" "$RESET"
  fi
  read -r input || true
  [[ -z "$input" && -n "$current" ]] && input="$current"
  printf -v "$key" '%s' "$input"
}

# ask_secret KEY "Prompt" — like ask, but input is hidden.
ask_secret() {
  local key="$1" prompt="$2" current input
  current=$(_existing "$key" || true)
  if [[ -n "$current" ]]; then
    printf '  %s%s%s %s[Enter keeps current]%s ' "$BOLD" "$prompt" "$RESET" "$DIM" "$RESET"
  else
    printf '  %s%s%s ' "$BOLD" "$prompt" "$RESET"
  fi
  read -rs input || true
  printf '\n'
  [[ -z "$input" && -n "$current" ]] && input="$current"
  printf -v "$key" '%s' "$input"
}

# write_env KEY VALUE — upsert KEY=VALUE into ENV_FILE (creates it; replaces
# any existing line). Idempotent.
write_env() {
  local key="$1" value="$2" tmp
  touch "$ENV_FILE"
  tmp=$(mktemp)
  grep -vE "^${key}=" "$ENV_FILE" > "$tmp" || true
  printf '%s=%s\n' "$key" "$value" >> "$tmp"
  mv "$tmp" "$ENV_FILE"
  WRITTEN_ENV+=("$key")
  printf '  %s✓ wrote%s %s → %s\n' "$GREEN" "$RESET" "$key" "$ENV_FILE"
}

# set_secret NAME VALUE — set a GitHub Actions repo secret via gh. Falls back
# to a warning (and records it) if gh is unavailable or unauthenticated.
set_secret() {
  local name="$1" value="$2"
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    if printf '%s' "$value" | gh secret set "$name" >/dev/null 2>&1; then
      WRITTEN_SECRET+=("$name")
      printf '  %s✓ set%s GitHub secret %s\n' "$GREEN" "$RESET" "$name"
      return
    fi
  fi
  SKIPPED+=("GitHub secret $name (set it manually: gh secret set $name)")
  warn "skipped GitHub secret $name — gh not ready; set it later"
}

# set_var NAME VALUE — set a GitHub Actions repo variable (non-secret).
set_var() {
  local name="$1" value="$2"
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    if gh variable set "$name" --body "$value" >/dev/null 2>&1; then
      printf '  %s✓ set%s GitHub variable %s\n' "$GREEN" "$RESET" "$name"
      return
    fi
  fi
  SKIPPED+=("GitHub variable $name")
  warn "skipped GitHub variable $name — gh not ready; set it later"
}

# finish — clear, then a closing summary of everything configured.
finish() {
  _clear
  printf '\n%s%s  ✓ Setup complete%s\n' "$BOLD" "$GREEN" "$RESET"
  (( ${#WRITTEN_ENV[@]} ))    && note "wrote ${#WRITTEN_ENV[@]} value(s) to $ENV_FILE: ${WRITTEN_ENV[*]}"
  (( ${#WRITTEN_SECRET[@]} )) && note "set ${#WRITTEN_SECRET[@]} GitHub secret(s): ${WRITTEN_SECRET[*]}"
  if (( ${#SKIPPED[@]} )); then
    printf '\n'; warn "still to do by hand:"
    for s in "${SKIPPED[@]}"; do note "  - $s"; done
  fi
  printf '\n'
}

# ──────────────────────────────────────────────────────────────────────────
# STAGES — author this section. One stage() per step the human takes.
# ──────────────────────────────────────────────────────────────────────────

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
ENV_FILE="$REPO_ROOT/.env"
PLAINTEXT="$REPO_ROOT/2-k3s/13.t3code/vault-passphrase.plaintext.yaml"
ENC="$REPO_ROOT/2-k3s/13.t3code/vault-passphrase.enc.yaml"
CLIPROXY_ENC="$REPO_ROOT/2-k3s/17.remote-pi/cliproxy/cliproxy-secrets.enc.yaml"
PROXY_YAML="$REPO_ROOT/2-k3s/05.traefik-deployment/ingress/t3code-proxy.yaml"
SECRETS_TMP=$(mktemp)
TMPENC=
chmod 0600 "$SECRETS_TMP"
trap 'rm -f "$SECRETS_TMP" "$TMPENC"' EXIT

TOTAL_STAGES=9

banner "t3code B2+B3 — sops secrets, provisioning, logins"

# ── Stage 1: preflight ────────────────────────────────────────────────────
stage "Preflight"
T3_CT_ID=$(_existing T3_CT_ID || true)
T3_CT_IP=$(_existing T3_CT_IP || true)
T3_PVE_HOST=$(_existing T3_PVE_HOST || true)
for pair in "T3_CT_ID:$T3_CT_ID" "T3_CT_IP:$T3_CT_IP" "T3_PVE_HOST:$T3_PVE_HOST"; do
  key=${pair%%:*}
  [[ -n ${pair#*:} ]] || { warn "$key missing from $ENV_FILE — run wizard-b1.sh first"; exit 1; }
done
say "Guest: CT $T3_CT_ID at $T3_CT_IP (via root@$T3_PVE_HOST for creation)."
command -v sops >/dev/null 2>&1 || { warn "sops not installed"; exit 1; }
command -v age  >/dev/null 2>&1 || warn "age CLI not found (sops may still work via its own keyring)"
if ssh -o BatchMode=yes -o ConnectTimeout=5 "root@$T3_CT_IP" true 2>/dev/null; then
  say "SSH to root@$T3_CT_IP works."
else
  warn "SSH to root@$T3_CT_IP failed (key not injected at create time?)"
  say "Fallback: ssh root@$T3_PVE_HOST then 'pct enter $T3_CT_ID' — stages 4-6 need adjusting if you go that way."
  confirm "Continue anyway?"
fi

# ── Stage 2: extract the cliproxy client key (B2, part 1) ─────────────────
stage "Cliproxy client key → hidden env"
ANTHROPIC_AUTH_TOKEN=$( (sops -d "$CLIPROXY_ENC" 2>/dev/null | grep -m1 'omp-api-key:' | sed 's/.*omp-api-key: *//') || true )
if [[ -n ${ANTHROPIC_AUTH_TOKEN:-} ]]; then
  say "omp-api-key extracted (value not shown)."
else
  warn "omp-api-key not found — decryption failed or key missing; provisioning will be skipped"
fi

# ── Stage 3: vault passphrase → sops (B2, part 2) ────────────────────────
stage "Vault passphrase → sops"
if [[ -s $ENC ]]; then
  say "vault-passphrase.enc.yaml already exists — reusing it (delete it to re-encrypt)."
else
  ask_secret T3_VAULT_PASSPHRASE "KeePassXC master passphrase (hidden):"
  if [[ -z ${T3_VAULT_PASSPHRASE:-} ]]; then
    warn "empty passphrase — encryption skipped"
  elif [[ ! -f $PLAINTEXT ]]; then
    warn "vault-passphrase.plaintext.yaml not found (gitignored) — recreate the draft on this machine to encrypt"
    SKIPPED+=("vault passphrase encryption (missing plaintext draft)")
  else
    TMPENC=$(mktemp --suffix=.enc.yaml)
    q=${T3_VAULT_PASSPHRASE//\'/\'\'}
    { grep -v 'VAULT_PASSPHRASE:' "$PLAINTEXT"; printf "  VAULT_PASSPHRASE: '%s'\n" "$q"; } > "$TMPENC"
    sops -e "$TMPENC" > "$ENC"
    if sops -d "$ENC" | grep -qF -- "$T3_VAULT_PASSPHRASE"; then
      say "Encrypted + decrypted round-trip OK → ${ENC#"$REPO_ROOT"/}"
    else
      warn "round-trip mismatch — inspect $ENC manually"
      SKIPPED+=("vault passphrase round-trip check")
    fi
  fi
fi

# ── Stage 4: wire the real IP into the Traefik proxy ──────────────────────
stage "Traefik proxy IP"
if grep -q "192.168.10.199" "$PROXY_YAML"; then
  sed -i "s|192.168.10.199|$T3_CT_IP|" "$PROXY_YAML"
  grep -q "ip: $T3_CT_IP" "$PROXY_YAML" && say "t3code-proxy.yaml Endpoints now point at $T3_CT_IP."
else
  say "No placeholder found in t3code-proxy.yaml — likely already wired. Leaving it."
fi
note "ArgoCD picks this up after the branch is committed and merged."

# ── Stage 5: provision the guest (runs ~5 minutes) ────────────────────────
stage "Provision the guest"
if [[ -z ${ANTHROPIC_AUTH_TOKEN:-} ]]; then
  warn "skipped — no cliproxy key from stage 2"
  SKIPPED+=("guest provisioning (no cliproxy key)")
elif [[ ! -s $ENC ]]; then
  warn "skipped — no vault-passphrase.enc.yaml (stage 3 skipped or failed)"
  SKIPPED+=("guest provisioning (no vault passphrase secret)")
else
  VAULT_PASSPHRASE=$( (sops -d "$ENC" 2>/dev/null | grep -m1 'VAULT_PASSPHRASE:' | sed 's/^ *VAULT_PASSPHRASE: *//') || true )
  case ${VAULT_PASSPHRASE:-} in
    "'"*"'") VAULT_PASSPHRASE=${VAULT_PASSPHRASE#\'}; VAULT_PASSPHRASE=${VAULT_PASSPHRASE%\'}; VAULT_PASSPHRASE=${VAULT_PASSPHRASE//\'\'/\'} ;;
    '"'*)    VAULT_PASSPHRASE=${VAULT_PASSPHRASE#\"}; VAULT_PASSPHRASE=${VAULT_PASSPHRASE%\"}; VAULT_PASSPHRASE=${VAULT_PASSPHRASE//\\\"/\"} ;;
  esac
  if [[ -z ${VAULT_PASSPHRASE:-} ]]; then
    warn "could not extract the passphrase from $ENC"
    SKIPPED+=("guest provisioning (passphrase extraction failed)")
  else
    ssh "root@$T3_CT_IP" "command -v git >/dev/null || (apt-get update -q && apt-get install -y -q git); test -d /opt/epaflix/.git || git clone -q https://github.com/SpyrosPsarras/epaflix.git /opt/epaflix; git -C /opt/epaflix pull --ff-only -q"
    {
      printf 'T3_GUEST_IP=%q\n' "$T3_CT_IP"
      printf 'ANTHROPIC_AUTH_TOKEN=%q\n' "$ANTHROPIC_AUTH_TOKEN"
      printf 'VAULT_PASSPHRASE=%q\n' "$VAULT_PASSPHRASE"
    } > "$SECRETS_TMP"
    scp -q "$SECRETS_TMP" "root@$T3_CT_IP:/root/t3secrets"
    rm -f "$SECRETS_TMP"
    ssh -t "root@$T3_CT_IP" "trap 'rm -f /root/t3secrets' EXIT; chmod 600 /root/t3secrets && set -a && . /root/t3secrets && rm -f /root/t3secrets && bash /opt/epaflix/2-k3s/13.t3code/provision.sh"
    say "Provisioning finished. Secrets file removed on the guest."
  fi
fi

# ── Stage 6: cluster access (kubeconfig + sops age key) ───────────────────
stage "Cluster access → guest"
KUBECFG="$REPO_ROOT/.kube/epaflix.kubeconfig"
AGE_KEY="$HOME/.config/sops/age/k3s-cluster.txt"
[[ -s $KUBECFG ]] || (cd "$REPO_ROOT" && bash .github/hooks/install-kubeconfig-epaflix.sh >/dev/null) || true
if [[ -s $KUBECFG && -s $AGE_KEY ]]; then
  if ssh "root@$T3_CT_IP" "sudo -iu spyros sh -c 'umask 077; cat > /home/spyros/.kube/config'" < "$KUBECFG" \
     && ssh "root@$T3_CT_IP" "sudo -iu spyros sh -c 'umask 077; cat > /home/spyros/.config/sops/age/k3s-cluster.txt'" < "$AGE_KEY" \
     && ssh "root@$T3_CT_IP" "sudo -iu spyros kubectl get nodes"; then
    say "kubeconfig (epaflix context only) + age key on the guest; kubectl reaches the cluster."
  else
    warn "copy or kubectl get nodes failed on the guest"
    SKIPPED+=("cluster access on guest (kubeconfig + age key)")
  fi
else
  warn "missing ${KUBECFG#"$REPO_ROOT"/} or $AGE_KEY — skipped (run .github/hooks/install-kubeconfig-epaflix.sh)"
  SKIPPED+=("cluster access on guest (kubeconfig + age key)")
fi

# ── Stage 7: gh auth login (B3, part 1) ───────────────────────────────────
stage "GitHub login (device flow)"
open_url "https://github.com/login/device"
ssh -t "root@$T3_CT_IP" "sudo -iu spyros gh auth login --hostname github.com --git-protocol https --web --skip-ssh-key" \
  || SKIPPED+=("gh auth login on guest")
pause "Press Enter once gh reports logged in."

# ── Stage 8: Azure login (B3, part 2) ─────────────────────────────────────
stage "Azure login (device code)"
open_url "https://microsoft.com/devicelogin"
ssh -t "root@$T3_CT_IP" "sudo -iu spyros az login --use-device-code" \
  || SKIPPED+=("az login on guest")
pause "Press Enter once az reports the subscription."

# ── Stage 9: verify + what remains ────────────────────────────────────────
stage "Verify"
if ssh -o ConnectTimeout=5 "root@$T3_CT_IP" "t3 service status; ss -tln | grep -q 3773 && echo PORT_3773_BOUND" 2>/dev/null; then
  say "Service and port check ran on the guest."
else
  warn "could not verify service/port over SSH — check manually: ssh root@$T3_CT_IP 't3 service status'"
  SKIPPED+=("t3 service verification")
fi
if curl -skI --max-time 5 https://t3code.epaflix.com | head -1; then
  say "Traefik is answering for t3code.epaflix.com."
else
  warn "https://t3code.epaflix.com not answering yet — needs DNS (stage 3 of B1) + the branch committed and merged so ArgoCD syncs the IngressRoute"
fi
SKIPPED+=("B4: pair the guest Syncthing folder with the PC holding the KDBX (desktop apps, both sides)")
SKIPPED+=("B5: pair a client over WireGuard against https://t3code.epaflix.com (one-time token from the guest)")
SKIPPED+=("B6 (optional): rotate the F0 GitHub PAT, store in KeePassXC, update Zed settings")
pause "All done here."

finish
