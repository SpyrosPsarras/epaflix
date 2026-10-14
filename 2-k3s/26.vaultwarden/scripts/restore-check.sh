#!/usr/bin/env bash
# Run only in an isolated restore Job with an empty PVC and dedicated CNPG role.
# Does not create/start a server, send mail or alter ingress.
set -euo pipefail
umask 077
export PATH="/tools:$PATH"
snapshot=${1:-}
[[ $snapshot =~ ^[0-9a-f]{64}$ ]] || { printf 'An explicit full snapshot ID is required\n' >&2; exit 2; }
[[ ${PGDATABASE:-} =~ ^vaultwarden_restore_[a-z0-9_]+$ && ${PGUSER:-} == "$PGDATABASE" ]] || {
  printf 'Only an isolated vaultwarden_restore_* database and matching role are allowed\n' >&2; exit 2;
}
[[ ${PGHOST:-} == postgres-cluster-rw.postgres-system.svc && ${PGPORT:-5432} == 5432 ]] || {
  printf 'Use the explicit shared CNPG endpoint with an isolated database\n' >&2; exit 2;
}
[[ -z ${PGSERVICE:-} && -z ${PGOPTIONS:-} ]] || { printf 'Connection overrides refused\n' >&2; exit 2; }
[[ -z ${PGHOSTADDR:-} ]] || { printf 'Alternate host address refused\n' >&2; exit 2; }
export PGSSLMODE=require PGCONNECT_TIMEOUT=10
DATA_DIR=${RESTORE_DATA_DIR:-/isolated-data}
WORK_DIR=${RESTORE_WORK_DIR:-/work/restore}
[[ -d $DATA_DIR && -z $(ls -A "$DATA_DIR") ]] || { printf 'Restore PVC must be empty\n' >&2; exit 2; }
[[ ! -e $WORK_DIR ]] || { printf 'Restore staging must be absent\n' >&2; exit 2; }
mkdir -p "$WORK_DIR"
chmod 700 "$WORK_DIR"
trap 'rm -rf -- "$WORK_DIR"' EXIT
query() { psql -X --no-password --set=ON_ERROR_STOP=1 -Atc "$1" 2>/dev/null; }
# Check actual identity/ownership, privileges and emptiness, not just environment.
actual=$(query "SELECT current_database() || '|' || current_user || '|' || pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()")
[[ $actual == "$PGDATABASE|$PGUSER|$PGUSER" ]] || { printf 'Unexpected restore database identity\n' >&2; exit 1; }
[[ $(query "SELECT count(*) FROM pg_roles WHERE rolname=current_user AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication)") == 0 ]] || exit 1
[[ $(query "SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=current_user)") == 0 ]] || {
  printf 'Restore role must not inherit any other role\n' >&2; exit 1;
}
[[ $(query "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')") == 0 ]] || {
  printf 'Restore database must be empty\n' >&2; exit 1;
}
restic restore "$snapshot" --target "$WORK_DIR" --verify --quiet
set_dir=$WORK_DIR/work/private/backup-set
[[ -s $set_dir/database.dump && -d $set_dir/data && -s $set_dir/recovery-point.txt ]] || {
  printf 'Snapshot is not a matched recovery set\n' >&2; exit 1;
}
[[ $(< "$set_dir/server-image.txt") == "${SERVER_IMAGE:?exact server image required}" ]] || {
  printf 'Restore server image does not match the recovery set\n' >&2; exit 1;
}
# No --create, --clean, or connection string from the archive. The dedicated role
# must have no membership in the source role and no rights on its objects.
pg_restore --exit-on-error --single-transaction --no-owner --no-privileges \
  --dbname="$PGDATABASE" "$set_dir/database.dump" 2>/dev/null || {
  printf 'Database restore failed; discard isolated target before retry\n' >&2; exit 1;
}
# Preserve child metadata, not the root-owned PVC mount's mode/timestamps.
shopt -s dotglob nullglob
entries=("$set_dir/data/"*)
if ((${#entries[@]})); then cp -a -- "${entries[@]}" "$DATA_DIR/"; fi
shopt -u dotglob nullglob
diff -qr "$set_dir/data" "$DATA_DIR" >/dev/null || { printf 'Restored file bytes differ\n' >&2; exit 1; }
# File-held settings override env; require review before starting the isolated
# server with SMTP/push disabled and its own DB URL/domain. Never start here.
if [[ -e $DATA_DIR/config.json ]]; then
  printf 'Restored config.json requires private review before isolated startup\n'
fi
printf 'Snapshot %s restored; recovery point %s; file byte comparison passed\n' "$snapshot" "$(< "$set_dir/recovery-point.txt")"
printf 'Database imported. Native-client entry/attachment and MCP checks remain required.\n'
