#!/usr/bin/env bash
#
# B1 wizard — t3code LXC creation wall.
# Human-only steps: create the LXC on Proxmox, pihole DNS record, PBS backup job.
# Captured values land in <repo>/.env (gitignored); the implement agent reads
# them afterwards to finish the Traefik proxy wiring.
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
CREATE_LXC="$REPO_ROOT/1-proxmox/t3code/create-lxc.sh"

TOTAL_STAGES=5

banner "t3code B1 — LXC + DNS + PBS"

# ── Stage 1: capture the guest parameters ─────────────────────────────────
stage "Guest parameters"
say "Decide these now; they compose the pct create command and land in .env."
ask T3_CT_ID "Free CT id (e.g. 213):"
ask T3_CT_IP "Static guest IP in 192.168.10.x:"
ask T3_CT_GW "LAN gateway (e.g. 192.168.10.1):"
ask T3_CT_DNS "pihole IP the guest will use as resolver:"
ask T3_CT_TEMPLATE "Debian 13 template path (pveam list local, e.g. local:vztmpl/debian-13-standard_13.2-1_amd64.tar.zst):"
ask T3_SSH_KEYS "Public key file for root SSH (Enter = auto-detect ~/.ssh/*.pub):"
ask T3_PVE_HOST "Proxmox host reachable over SSH (e.g. pve.lan or 192.168.10.x):"
if [[ -z $T3_SSH_KEYS ]]; then
  T3_SSH_KEYS=$( (find "$HOME/.ssh" -maxdepth 1 -name 'id_*.pub' 2>/dev/null || true) | head -1)
fi
[[ -z $T3_SSH_KEYS || -f $T3_SSH_KEYS ]] || { warn "key file '$T3_SSH_KEYS' does not exist"; exit 1; }
[[ $T3_CT_ID =~ ^[0-9]+$ ]] || { warn "CT id must be numeric (got '$T3_CT_ID')"; exit 1; }
[[ $T3_CT_IP =~ ^192\.168\.10\.[0-9]+$ ]] || { warn "guest IP must be 192.168.10.x (got '$T3_CT_IP')"; exit 1; }
for v in "$T3_CT_GW" "$T3_CT_DNS" "$T3_CT_TEMPLATE" "$T3_PVE_HOST" "$T3_SSH_KEYS"; do
  [[ $v != *"'"* && $v != *"|"* ]] || { warn "value contains a quote or pipe: '$v'"; exit 1; }
done
write_env T3_CT_ID "$T3_CT_ID"
write_env T3_CT_IP "$T3_CT_IP"
write_env T3_CT_GW "$T3_CT_GW"
write_env T3_CT_DNS "$T3_CT_DNS"
write_env T3_CT_TEMPLATE "$T3_CT_TEMPLATE"
write_env T3_SSH_KEYS "$T3_SSH_KEYS"
write_env T3_PVE_HOST "$T3_PVE_HOST"
note "Template check: ssh root@$T3_PVE_HOST 'pveam list local' if unsure."

# ── Stage 2: create the LXC ───────────────────────────────────────────────
stage "Create the LXC"
say "Runs 1-proxmox/t3code/create-lxc.sh (unprivileged Debian 13, 4c/8G/64G, static IP)."
if confirm "Run it now over SSH as root@$T3_PVE_HOST?"; then
  if [[ -n $T3_SSH_KEYS ]]; then
    ssh "root@$T3_PVE_HOST" "cat > /root/t3code.keys" < "$T3_SSH_KEYS"
    REMOTE_KEYS=/root/t3code.keys
  else
    warn "no key file — the guest gets no SSH way in; pct enter from the PVE host still works"
    REMOTE_KEYS=
  fi
  ssh "root@$T3_PVE_HOST" \
    "CT_ID='$T3_CT_ID' CT_IP='$T3_CT_IP' CT_GW='$T3_CT_GW' CT_DNS='$T3_CT_DNS' CT_TEMPLATE='$T3_CT_TEMPLATE' CT_SSH_KEYS='$REMOTE_KEYS' bash -s" \
    < "$CREATE_LXC"
else
  say "Paste this into the PVE node shell instead:"
  say "  CT_ID=$T3_CT_ID CT_IP=$T3_CT_IP CT_GW=$T3_CT_GW CT_DNS=$T3_CT_DNS CT_TEMPLATE='$T3_CT_TEMPLATE' CT_SSH_KEYS=/root/t3code.keys bash -s < $CREATE_LXC"
  warn "the script and the key file must exist on the PVE host for the manual path; scp both over first"
  say "  scp $CREATE_LXC root@$T3_PVE_HOST:/root/"
  [[ -z $T3_SSH_KEYS ]] || say "  scp $T3_SSH_KEYS root@$T3_PVE_HOST:/root/t3code.keys"
fi
pause "Press Enter when the container is up."
if ping -c1 -W2 "$T3_CT_IP" >/dev/null 2>&1; then
  say "ping $T3_CT_IP — reachable."
else
  warn "ping $T3_CT_IP failed — check the console in the Proxmox UI before continuing."
fi

# ── Stage 3: pihole DNS record ────────────────────────────────────────────
stage "pihole DNS record"
say "Point t3code.epaflix.com at the Traefik internal LB (192.168.10.102)."
step "Open the pihole admin at http://$T3_CT_DNS/admin"
step "Local DNS → DNS Records → add: t3code.epaflix.com → 192.168.10.102"
say "Equivalent file-based path: copy $REPO_ROOT/1-proxmox/pihole/t3code-dns.conf"
say "to /etc/dnsmasq.d/ on the pihole host and restart pihole-FTL."
pause "Press Enter when the record is in."
if command -v dig >/dev/null 2>&1; then
  say "dig says: $(dig +short t3code.epaflix.com "@$T3_CT_DNS" | tr '\n' ' ')"
else
  warn "dig not installed — verify with: dig +short t3code.epaflix.com @$T3_CT_DNS"
fi

# ── Stage 4: PBS backup job ───────────────────────────────────────────────
stage "PBS backup job"
say "Whole-guest backup per Q7 — no file-level cron."
note "Open your PBS web UI manually."
step "PBS UI → your datastore → Backup Jobs → edit the existing job (or add one)"
step "Include guest id $T3_CT_ID (t3code) in the job's 'include' list"
if ! confirm "Is t3code covered by a PBS backup job now?"; then
  SKIPPED+=("PBS backup job coverage for guest $T3_CT_ID")
  warn "left undone — finish() will list it at the end"
fi

# ── Stage 5: recap + what happens next ────────────────────────────────────
stage "Recap"
say "Captured: CT $T3_CT_ID at $T3_CT_IP (gw $T3_CT_GW, dns $T3_CT_DNS, template $T3_CT_TEMPLATE)."
say "Agent follow-ups after this wizard: replace the placeholder IP in"
say "2-k3s/05.traefik-deployment/ingress/t3code-proxy.yaml and verify the"
say "IngressRoute lands on the cluster. Then the B2 wizard (sops secrets)."
pause "All done here."

finish
