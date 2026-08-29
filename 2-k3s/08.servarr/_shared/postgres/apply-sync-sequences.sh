#!/usr/bin/env bash

: "${KUBECTL_CONTEXT:=epaflix}"
kubectl() { command kubectl --context "$KUBECTL_CONTEXT" "$@"; }

set -euo pipefail

NAMESPACE="${NAMESPACE:-servarr}"
SECRET="${SECRET:-servarr-postgres}"
SQL_FILE="${SQL_FILE:-$(dirname "$0")/sync-sequences.sql}"
APPS=("${@:-sonarr sonarr2 radarr bazarr}")

if [[ ! -f "${SQL_FILE}" ]]; then
    echo "ERROR: SQL file not found: ${SQL_FILE}" >&2
    exit 1
fi

if ! command -v psql >/dev/null 2>&1; then
    echo "ERROR: psql not found in PATH" >&2
    exit 1
fi

get_key() {
    kubectl get secret "${SECRET}" -n "${NAMESPACE}" \
        -o jsonpath="{.data.$1}" | base64 -d
}

for app in ${APPS[@]}; do
    PGHOST=$(get_key "${app}-host")
    PGPORT=$(get_key "${app}-port")
    PGDATABASE=$(get_key "${app}-database")
    PGUSER=$(get_key "${app}-user")
    PGPASSWORD=$(get_key "${app}-password")
    export PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD

    echo
    echo "=== ${app} (${PGDATABASE} @ ${PGHOST}) ==="
    psql -v ON_ERROR_STOP=1 -f "${SQL_FILE}"
    unset PGPASSWORD
done

echo
echo "Done. Verify with:"
echo "  kubectl create job -n servarr sequence-audit-verify --from=cronjob/postgres-sequence-audit"
echo "  kubectl logs -n servarr -f job/sequence-audit-verify"
