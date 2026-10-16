#!/usr/bin/env bash
set +x
set -euo pipefail

if [[ ! -t 0 || ! -t 1 ]]; then
  printf '%s\n' 'Run this script in your private interactive administrative terminal.' >&2
  exit 1
fi

TOOLS=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
case "${1:-}" in
  github)
    printf '%s\n' \
      'Stage 1/2: Create this backend a dedicated fine-grained GitHub PAT.' \
      'Open https://github.com/settings/personal-access-tokens/new in your browser.' \
      'Resource owner: the work organization.' \
      'Repository access: only needed organization repos, including a private repo.' \
      'Permissions: Metadata read + Contents read.' \
      'Expiration: short.' \
      'If GitHub asks, approve the token request in the organization settings.'
    printf '%s\n' 'Stage 2/2: Check the token and save it to the work grants Secret.'
    python3 "$TOOLS/provision.py" github
    ;;
  azure)
    printf '%s\n' \
      'Stage 1/2: Confirm the reviewed inputs pin exactly the AKS dev cluster.' \
      'The work pod must already be healthy after reviewed merge and manual sync.' \
      'Use the approved work Azure identity. Never select a production subscription or cluster.' \
      'No Azure credentials are copied from this terminal or from the mixed pod.'
    read -r -p 'Type dev to begin device-code login inside the work pod: ' target
    [[ "$target" == dev ]] || exit 1
    printf '%s\n' 'Stage 2/2: Follow the device-code instructions in this private terminal.'
    kubectl --context epaflix -n t3code-work exec -it work-0 -c t3 -- \
      env -u SSH_AUTH_SOCK -u AZURE_CONFIG_DIR /scripts/work-aks-auth.py login
    ;;
  *)
    printf '%s\n' 'Usage: bash owner-login.sh github|azure' >&2
    exit 2
    ;;
esac
