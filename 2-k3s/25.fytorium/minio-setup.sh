#!/usr/bin/env bash
set +x
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
self="$here/minio-setup.sh"
mc_image='fytorium-mc:RELEASE.2025-04-16T18-13-26Z'

policy() {
  cat <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": ["arn:aws:s3:::fytorium-photos/*"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetBucketLocation"],
      "Resource": ["arn:aws:s3:::fytorium-photos"]
    }
  ]
}
JSON
}

mc() {
  # Use the image's mc entrypoint directly; no container shell or helper tools needed.
  docker run --rm -i -e MC_HOST_fytorium "$mc_image" "$@"
}

apply() {
  if ! docker image inspect "$mc_image" >/dev/null 2>&1; then
    docker build -q -t "$mc_image" -f "$here/mc.Dockerfile" "$here"
  fi
  # URL-encode credentials, including passwords containing @, : or /.
  MC_HOST_fytorium=$(python3 - <<'PY'
import os
from urllib.parse import quote, urlsplit, urlunsplit
url = urlsplit(os.environ.get("FYTORIUM_MINIO_URL", "https://minio.epaflix.com"))
if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
    raise ValueError("MinIO URL must be HTTP(S), without credentials")
user = quote(os.environ["MINIO_ADMIN_USERNAME"], safe="")
password = quote(os.environ["MINIO_ADMIN_PASSWORD"], safe="")
print(urlunsplit(url._replace(netloc=user + ":" + password + "@" + url.netloc)))
PY
  )
  export MC_HOST_fytorium
  mc mb --ignore-existing fytorium/fytorium-photos
  mc version suspend fytorium/fytorium-photos
  mc anonymous set none fytorium/fytorium-photos
  policy | mc admin policy create fytorium fytorium-photos-rw /dev/stdin
  # Bash's builtin printf sends both keys over stdin, never through process arguments.
  printf '%s\n%s\n' "$VAULT_USERNAME" "$VAULT_PASSWORD" | mc admin user add fytorium
  current_policies=$(mc --json admin user info fytorium "$VAULT_USERNAME" | python3 -c '
import json, sys
print(json.load(sys.stdin).get("policyName", ""))
')
  case ",$current_policies," in
    *,fytorium-photos-rw,*) ;;
    *) mc admin policy attach fytorium fytorium-photos-rw --user "$VAULT_USERNAME" ;;
  esac
}

if (( $# > 1 )); then
  echo 'Usage: minio-setup.sh [--dry-run]' >&2
  exit 2
fi

case "${1:-}" in
  --dry-run)
    # No vault access or Docker execution: safe to inspect on an offline machine.
    printf 'Docker image: %s; built from mc.Dockerfile if not cached locally\n' "$mc_image"
    printf 'Admin credentials in environment, app keys on stdin\n'
    cat <<'COMMANDS'
Bitwarden admin login: folder Personal / MinIO Object Store
Bitwarden app login: collection homelab / fytorium - minio
MinIO URL: https://minio.epaflix.com (FYTORIUM_MINIO_URL overrides for local tests)
mc mb --ignore-existing fytorium/fytorium-photos
mc version suspend fytorium/fytorium-photos
mc anonymous set none fytorium/fytorium-photos
mc admin policy create fytorium fytorium-photos-rw /dev/stdin < policy.json
printf '<masked-access-key>\n<masked-secret-key>\n' | mc admin user add fytorium
mc --json admin user info fytorium <masked-access-key>
mc admin policy attach fytorium fytorium-photos-rw --user <masked-access-key> # only if absent
Policy on stdin:
COMMANDS
    policy
    exit 0
    ;;
  '')
    exec python3 - "$self" <<'PY'
import json
import os
import shutil
import subprocess
import sys

if not shutil.which("bw") or not os.environ.get("BW_SESSION"):
    sys.exit("Bitwarden CLI must be installed and unlocked with BW_SESSION exported.")


def bw(*args):
    result = subprocess.run(["bw", "--nointeraction", *args], stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, check=True, timeout=30)
    return json.loads(result.stdout)


try:
    if bw("status").get("status") != "unlocked":
        sys.exit("Bitwarden CLI must be installed and unlocked with BW_SESSION exported.")
    env = os.environ.copy()
    for kind, container, name, prefix in (("folder", "Personal", "MinIO Object Store", "MINIO_ADMIN"),
                                          ("collection", "homelab", "fytorium - minio", "VAULT")):
        containers = bw("list", "folders" if kind == "folder" else "collections")
        matches = [entry for entry in containers if entry.get("name") == container]
        if len(matches) != 1 or not matches[0].get("id"):
            raise ValueError()
        container_id = matches[0]["id"]
        items = bw("list", "items", "--" + kind + "id", container_id, "--search", name)
        matches = [entry for entry in items if entry.get("name") == name
                   and (entry.get("folderId") == container_id if kind == "folder" else
                        container_id in (entry.get("collectionIds") or []))]
        if len(matches) != 1 or matches[0].get("type") != 1:
            raise ValueError()
        login = matches[0]["login"]
        for field in ("username", "password"):
            value = login[field]
            if not isinstance(value, str) or not value or "\u0000" in value:
                raise ValueError()
            env[prefix + "_" + field.upper()] = value
    os.execve(sys.argv[1], [sys.argv[1], "--apply"], env)
except Exception:
    sys.exit("Bitwarden credential lookup failed; expected one login per folder or collection and item.")
PY
    ;;
  --apply)
    : "${MINIO_ADMIN_USERNAME:?missing admin username}" "${MINIO_ADMIN_PASSWORD:?missing admin password}"
    : "${VAULT_USERNAME:?vault item needs a username}" "${VAULT_PASSWORD:?vault item needs a password}"
    # mc reads one line per key; reject values it would silently truncate.
    case "$VAULT_USERNAME$VAULT_PASSWORD" in
      *$'\n'*|*$'\r'*) echo 'MinIO keys must not contain line breaks.' >&2; exit 1 ;;
    esac
    # A subshell preserves errexit for every command, unlike an if-tested function.
    # Suppress provider output, which can contain the credential-bearing alias URL.
    set +e
    (set -e; apply) >/dev/null 2>&1
    status=$?
    set -e
    if (( status != 0 )); then
      echo 'MinIO setup failed; check LAN access, vault credentials and Docker. Provider output withheld.' >&2
      exit 1
    fi
    echo 'MinIO ready: private fytorium-photos bucket, versioning suspended, app policy attached.'
    ;;
  *)
    echo 'Usage: minio-setup.sh [--dry-run]' >&2
    exit 2
    ;;
esac
