#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
hook="$repo_root/.github/hooks/check-sops-encrypted.sh"
tmp="$(mktemp -d)"
output="$(mktemp)"
trap 'rm -rf "$tmp"; rm -f "$output"' EXIT

pass_count=0
fail_count=0

pass() {
  pass_count=$((pass_count + 1))
  printf 'ok - %s\n' "$1"
}

fail() {
  fail_count=$((fail_count + 1))
  printf 'not ok - %s\n' "$1" >&2
  if [ -s "$output" ]; then
    sed 's/^/  /' "$output" >&2
  fi
}

expect_pass() {
  local name=$1
  shift
  : >"$output"
  if (cd "$tmp" && bash "$hook" "$@") >"$output" 2>&1; then
    pass "$name"
  else
    fail "$name"
  fi
}

expect_fail() {
  local name=$1
  shift
  : >"$output"
  if (cd "$tmp" && bash "$hook" "$@") >"$output" 2>&1; then
    fail "$name"
  else
    pass "$name"
  fi
}

reset_repo() {
  git -C "$tmp" reset --hard -q "$baseline"
  git -C "$tmp" clean -fdqx
  mkdir -p "$tmp/fixtures"
}

write_genuine_sops_fixture() {
  local path=$1
  mkdir -p "$(dirname "$path")"
  cat >"$path" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
    name: synthetic-encrypted
stringData:
    password: ENC[AES256_GCM,data:VuyzQHtUbNRmXVeG3RMTV309y4bNRu5f0+Bj0OwbmU2n0Q==,iv:RNDthdDhhzkizI5WOJSrSNdfCKZ8JegefvE7CcvatHY=,tag:xbScufs/KO1hh2W6eWw9pg==,type:str]
sops:
    age:
        - enc: |
            -----BEGIN AGE ENCRYPTED FILE-----
            YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+IFgyNTUxOSBHdjVRMCtMOVdja3Z5NjJm
            RENSUEk2djZERittczAvWGpRUUI2TzY3RkRZCkxhL2NDYTJZaWFKd3c3VGJsajRV
            UVF5aU91U3ZBL3g0Z3ZBRnlaRnl6eUUKLS0tIE0xRFJsUmo0QnFERHRvOHJ3c2px
            R1VSRCtKbGE2YTRnTXcwRjgzTEM5WnMKBETwaCH+fsQb3HGpkqZjlBqLvGPqTEyf
            41vlyVVWeHaXvYmoJo3Tf5aNV5iplKD4p118KfQ0Iu9lzY1jIUViLw==
            -----END AGE ENCRYPTED FILE-----
          recipient: age1586thf5vkcdf5lcn3zwjpu8ltkwyq8efrhj8lr0vdrrt9k5f3qgsxeg7gx
    encrypted_regex: ^(data|stringData)$
    lastmodified: "2026-08-06T15:38:53Z"
    mac: ENC[AES256_GCM,data:kXJ1/fMNQPmPA9BT1js6ZrtY7MwNCqqL73/lqWn7RCZw0mmV1tYmpn93XeHz/np9HRYd6Eeo4YNFwbfACV6FnwRU4sqbzGBViywx1XE17euMscO3ygmSblu3qASKvfW9xnemiWG+PW7FmhqyahozvNyqWk1Ls/a6U98kBz4KhbM=,iv:WEakktGWCcbnLfQozntwRhhwZRWozjJSlNCRhbei0Io=,tag:n5S44VKL//ZKZh0vzvENjg==,type:str]
    version: 3.13.3
YAML
}

plaintext_templates=(
  "2-k3s/05.traefik-deployment/certificates/cloudflare-origin-cert.yaml"
  "2-k3s/06.postgres/cluster/postgres-secret.yaml"
  "2-k3s/06.postgres/operator-kustomization/barman-manifest.yaml"
  "2-k3s/07.authentik-deployment/secret-app.yaml"
  "2-k3s/08.servarr/seerr/authentik-oidc-secret.yaml"
  "2-k3s/08.servarr/_shared/secrets/postgres-secret.yaml"
  "2-k3s/10.observability/grafana-admin-secret.yaml"
  "2-k3s/10.observability/grafana-config/oauth-secret.yaml"
  "2-k3s/10.observability/grafana-db-secret.yaml"
  "2-k3s/10.observability/pve-exporter/secret.yaml"
  "2-k3s/11.argocd/oidc-secret.yaml"
  "2-k3s/12.renovate/secret-app.yaml"
)

git -C "$tmp" init -q
git -C "$tmp" config user.name "Synthetic Fixture"
git -C "$tmp" config user.email "fixture.invalid@example.invalid"
printf 'charts/\n' >"$tmp/.gitignore"
for path in "${plaintext_templates[@]}"; do
  mkdir -p "$tmp/$(dirname "$path")"
  cp "$repo_root/$path" "$tmp/$path"
done
git -C "$tmp" add .
git -C "$tmp" commit -q --no-verify -m "fixture baseline"
baseline="$(git -C "$tmp" rev-parse HEAD)"

for path in "${plaintext_templates[@]}"; do
  printf '\n# fixture: comment-only edit\n' >>"$tmp/$path"
done
git -C "$tmp" add "${plaintext_templates[@]}"
expect_pass "comment-only edits to all 12 plaintext Secret files pass"

reset_repo
barman="2-k3s/06.postgres/operator-kustomization/barman-manifest.yaml"
printf '\n# fixture: comment-only barman edit\n' >>"$tmp/$barman"
git -C "$tmp" add "$barman"
expect_pass "active barman image Secret passes its narrow validator"

reset_repo
python3 - "$tmp/$barman" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text()
text = text.replace(
    "metadata:\n  name: plugin-barman-cloud-f998mh5292\n",
    "metadata:\n  annotations:\n    synthetic.example/review: github_pat_SYNTHETIC_BARMAN_ANNOTATION_000000\n  name: plugin-barman-cloud-f998mh5292\n",
    1,
)
path.write_text(text)
PY
git -C "$tmp" add "$barman"
expect_fail "barman image exception does not exempt credential-shaped annotations"

reset_repo
python3 - "$tmp/$barman" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text()
text = text.replace(
    "data:\n  SIDECAR_IMAGE: |\n",
    "data:\n  SYNTHETIC_EXTRA_FIELD: <SYNTHETIC_EXTRA_FIELD>\n  SIDECAR_IMAGE: |\n",
    1,
)
path.write_text(text)
PY
git -C "$tmp" add "$barman"
expect_fail "barman path does not grant blanket trust to extra payload fields"

reset_repo
postgres="2-k3s/06.postgres/cluster/postgres-secret.yaml"
printf '  optional-port: "5433"\n  optional-password: "<OPTIONAL_PASSWORD>"\n' >>"$tmp/$postgres"
git -C "$tmp" add "$postgres"
expect_pass "new safe template keys do not require a policy update"

reset_repo
mkdir -p "$tmp/fixtures"
cat >"$tmp/fixtures/concrete-secret.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: synthetic-concrete
stringData:
  username: "github_pat_SYNTHETIC_NOT_A_REAL_TOKEN_000000000000"
YAML
git -C "$tmp" add fixtures/concrete-secret.yaml
expect_fail "credential-shaped plaintext under a non-sensitive key fails"

reset_repo
cat >"$tmp/fixtures/punctuation-entropy.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: synthetic-punctuation-entropy
stringData:
  username: 'V7!mQ2@xR9#kL4$zN8%pT6^w'
YAML
git -C "$tmp" add fixtures/punctuation-entropy.yaml
expect_fail "punctuation-heavy high entropy under a non-sensitive key fails"

reset_repo
cat >"$tmp/fixtures/short-high-entropy.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: synthetic-short-high-entropy
stringData:
  value: 'aB3$dE5&gH7*jK9!mN'
YAML
git -C "$tmp" add fixtures/short-high-entropy.yaml
expect_fail "18-character high-entropy value under a non-sensitive key fails"

reset_repo
cat >"$tmp/fixtures/short-mixed-class.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: short-mixed-class-fixture
type: Opaque
stringData:
  bootstrap-code: "Passw0rdX7"
YAML
git -C "$tmp" add fixtures/short-mixed-class.yaml
expect_fail "short mixed-class value under a non-sensitive key fails"

reset_repo
servarr="2-k3s/08.servarr/_shared/secrets/postgres-secret.yaml"
printf '  optional-database: "jellyseerr"\n  optional-user: "prowlarr"\n  optional-instance: "sonarr2-database"\n' >>"$tmp/$servarr"
git -C "$tmp" add "$servarr"
expect_pass "short lowercase template identifiers still pass the new band"

reset_repo
python3 - "$tmp/fixtures/oversized-scalar.yaml" 2400 <<'PY'
import sys
from pathlib import Path

padding = "a1-b2_c3.d4/" * (int(sys.argv[2]) // 12)
Path(sys.argv[1]).write_text(
    "# Synthetic only: this padding is deliberately not a real credential.\n"
    "apiVersion: v1\n"
    "kind: Secret\n"
    "metadata:\n"
    "  name: oversized-scalar-fixture\n"
    "type: Opaque\n"
    "stringData:\n"
    f'  bootstrap-blob: "{padding}"\n'
)
PY
git -C "$tmp" add fixtures/oversized-scalar.yaml
expect_fail "opaque plaintext scalar above the length limit fails"

reset_repo
python3 - "$tmp/fixtures/bounded-scalar.yaml" 2040 <<'PY'
import sys
from pathlib import Path

padding = "a1-b2_c3.d4/" * (int(sys.argv[2]) // 12)
Path(sys.argv[1]).write_text(
    "# Synthetic only: this padding is deliberately not a real credential.\n"
    "apiVersion: v1\n"
    "kind: Secret\n"
    "metadata:\n"
    "  name: bounded-scalar-fixture\n"
    "type: Opaque\n"
    "stringData:\n"
    f'  bootstrap-blob: "{padding}"\n'
)
PY
git -C "$tmp" add fixtures/bounded-scalar.yaml
expect_pass "opaque plaintext scalar at the length limit is analysed and passes"

reset_repo
cat >"$tmp/fixtures/whitespace-unicode-entropy.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: synthetic-whitespace-unicode-entropy
stringData:
  spaced-label: 'V7!mQ2@xR9# kL4$zN8%pT6^w'
  unicode-label: 'αβγδεζηθικλμνξοπρστυφχψω'
YAML
git -C "$tmp" add fixtures/whitespace-unicode-entropy.yaml
expect_fail "internal whitespace and printable Unicode do not bypass entropy checks"

reset_repo
cat >"$tmp/fixtures/secret-list.yaml" <<'YAML'
apiVersion: v1
kind: List
items:
  - apiVersion: v1
    kind: Secret
    metadata:
      name: synthetic-nested-secret
    stringData:
      password: SYNTHETIC-CONCRETE-NESTED-PASSWORD
YAML
git -C "$tmp" add fixtures/secret-list.yaml
expect_fail "Secret nested inside a Kubernetes List cannot bypass validation"

reset_repo
cat >"$tmp/fixtures/scalar-password-ref.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: synthetic-scalar-reference
stringData:
  password-ref: violet-violet-violet-violet
YAML
git -C "$tmp" add fixtures/scalar-password-ref.yaml
expect_fail "scalar password-ref is not exempt from placeholder enforcement"

reset_repo
cat >"$tmp/fixtures/passphrase-placeholder.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: synthetic-passphrase-placeholder
stringData:
  password: <violet-violet-violet-violet>
YAML
git -C "$tmp" add fixtures/passphrase-placeholder.yaml
expect_fail "passphrase-shaped angle-bracket value is not a placeholder"

reset_repo
pve="2-k3s/10.observability/pve-exporter/secret.yaml"
python3 - "$tmp/$pve" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text()
text = text.replace(
    "<PROXMOX_API_TOKEN_VALUE>",
    "github_pat_SYNTHETIC_NESTED_NOT_A_REAL_TOKEN_000000000000",
)
path.write_text(text)
PY
git -C "$tmp" add "$pve"
expect_fail "credential-shaped value hidden in embedded pve.yml fails"

reset_repo
cat >"$tmp/fixtures/one-line-embedded.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: synthetic-one-line-embedded
stringData:
  config: |-
    password: SYNTHETIC-NOT-A-CREDENTIAL
YAML
git -C "$tmp" add fixtures/one-line-embedded.yaml
expect_fail "one-line embedded YAML with a sensitive field fails"

reset_repo
cat >"$tmp/fixtures/partial-placeholder.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: synthetic-partial-placeholder
stringData:
  password: "<PASSWORD>-SYNTHETIC-CONCRETE"
YAML
git -C "$tmp" add fixtures/partial-placeholder.yaml
expect_fail "placeholder alongside concrete text fails"

reset_repo
write_genuine_sops_fixture "$tmp/fixtures/encrypted.enc.yaml"
git -C "$tmp" add fixtures/encrypted.enc.yaml
expect_pass "genuinely SOPS-generated document-local encrypted Secret passes"

reset_repo
cat >"$tmp/fixtures/bare-enc.enc.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: synthetic-bare-enc
stringData:
  password: ENC[SYNTHETIC-PLAINTEXT-PASSWORD]
sops:
  marker: synthetic
YAML
git -C "$tmp" add fixtures/bare-enc.enc.yaml
expect_fail "bare ENC marker with stub SOPS metadata fails"

reset_repo
write_genuine_sops_fixture "$tmp/fixtures/mixed.enc.yaml"
cat >>"$tmp/fixtures/mixed.enc.yaml" <<'YAML'
---
apiVersion: v1
kind: Secret
metadata:
  name: synthetic-plaintext-second-document
stringData:
  password: "SYNTHETIC_CONCRETE_NOT_A_REAL_PASSWORD"
YAML
git -C "$tmp" add fixtures/mixed.enc.yaml
expect_fail "SOPS metadata in one document cannot bless another Secret document"

reset_repo
write_genuine_sops_fixture "$tmp/fixtures/wrong-suffix.yaml"
git -C "$tmp" add fixtures/wrong-suffix.yaml
expect_fail "SOPS Secret without the encrypted-file suffix fails"

reset_repo
cat >"$tmp/fixtures/configmap.yaml" <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata:
  name: ordinary-config
data:
  password: ordinary-configuration-text
YAML
git -C "$tmp" add fixtures/configmap.yaml
expect_pass "non-Secret YAML passes"

reset_repo
chart_dir="$tmp/vendor/charts/demo/templates"
mkdir -p "$chart_dir"
cat >"$chart_dir/configmap.yaml" <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata:
  name: "{{ .Release.Name }}-synthetic"
YAML
git -C "$tmp" add -f vendor/charts/demo/templates/configmap.yaml
expect_pass "non-Secret vendored chart template passes the Secret guard"

reset_repo
mkdir -p "$chart_dir"
cat >"$chart_dir/secret.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: "{{ .Release.Name }}-synthetic"
stringData:
  password: "{{ .Values.syntheticPassword }}"
YAML
git -C "$tmp" add -f vendor/charts/demo/templates/secret.yaml
expect_fail "force-added chart Secret is not exempt by path"

reset_repo
mkdir -p "$tmp/fixtures"
cat >"$tmp/fixtures/bypassed.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: synthetic-bypassed
stringData:
  token: "SYNTHETIC_CONCRETE_NOT_A_REAL_TOKEN"
YAML
git -C "$tmp" add fixtures/bypassed.yaml
git -C "$tmp" commit -q --no-verify -m "synthetic bypass fixture"
expect_pass "cached mode ignores unchanged tracked files"
expect_fail "full-tree mode catches a bypassed plaintext Secret" --full-tree

printf '%s\n' "1..$((pass_count + fail_count))"
if [ "$fail_count" -ne 0 ]; then
  printf '%s fixture test(s) failed\n' "$fail_count" >&2
  exit 1
fi
printf 'All %s fixture tests passed.\n' "$pass_count"
