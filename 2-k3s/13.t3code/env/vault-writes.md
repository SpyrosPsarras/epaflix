# Scoped vault writes

Both isolated runtimes use the same `vault` client and their own `bw serve`
sidecar. The scope comes from `/run/t3-vault/scope.json`, not caller arguments.
`vault run`, `update`, `trash` and `attach` refuse items outside that org and
collection, and items with a non-null `deletedDate`. Exact-name reads ignore
trashed matches, then re-fetch the selected item by ID before using its values.

`vault list [filter]` discovers active items in that same org and collection.
The optional filter matches item names by case-insensitive substring. Output is
one JSON array sorted by case-insensitive name, then exact name. Each object
contains `name`, numeric `type`, `password` indicating whether a login password
exists, sorted `fields` names and sorted `attachments` filenames. It prints no
values, usernames, URIs, notes or TOTP. No matches returns `[]` with exit code
`0`; invalid arguments, scope or API failures use the usual refusal code `1`.

## Commands

Supply item JSON through a private regular file or a `VAULT_` environment
variable. Do not paste values into commands, tool arguments or shell history.
The following paths and UUID placeholder contain no values:

```sh
vault create --input-file /tmp/private-item.json
vault create --input-env VAULT_DOCUMENT
vault update --item "$ITEM_ID" --input-file /tmp/private-patch.json
vault update --item "$ITEM_ID" --input-env VAULT_DOCUMENT
vault attach --item "$ITEM_ID" --file /tmp/private-files/credential.bin
vault trash --item "$ITEM_ID"
```

Create requires `name` and `type`. Supported types are login `1`, secure note
`2`, card `3`, identity `4` and SSH key `5`. Accepted top-level fields are
`name`, `type`, `notes`, `login`, `fields`, `secureNote`, `card`, `identity`,
`sshKey`, `favorite` and `reprompt`. Bitwarden validates the type-specific
contents. Update replaces each supplied top-level field, not individual nested
properties. Omitted fields remain unchanged. For example, a supplied `login`
replaces the entire login object, so include its username and URLs if needed.

The client rejects ownership fields such as `organizationId`, `collectionIds`,
`folderId` and `id`. Create always submits the scoped org, exactly the scoped
collection and a null folder. Update retains the selected item's ownership.
Writes serialize through a private home-directory lock. Other stock-client
sessions and owner applications do not use that lock.

`bw serve` can acknowledge a write before its local cipher cache reflects it.
The client polls that cache for the expected item state before reporting success,
including attachment upload and deletion and the item's trash marker.
Confirmation validates item ID and scope. Update checks the submitted fields,
attachment confirmation checks ID, filename and size, and trash checks that
`deletedDate` is set. Replacement also checks that the old attachment ID is
absent. Unrelated server metadata does not prevent confirmation. Repeated
polling stops after five seconds; each HTTP request retains its own timeout.
Only reads are retried, including the CLI's HTTP 400 `Not found.` cache miss.
The client never replays a write. Once a write is acknowledged and a usable item
ID is known, any validation or confirmation failure exits with code `2`. This
includes read timeouts, connection failures, HTTP errors, scope mismatches and
missing attachment metadata, not just expiry of the polling deadline. It prints
`write sent, local cache not confirmed`, the known item ID and any known
attachment ID. This differs from refusal code `1`. Cancellation still exits with
code `130`. Inspect the item privately before retrying.

Files must be regular, non-symlink files, at most 12 MiB. The local API can
impose a smaller request limit. Attachment names come from the file basename
and must be printable ASCII without quotes or backslashes, at most 255
characters. Attach replaces an existing attachment with that exact basename.
Multiple existing matches are refused rather than guessed.

Replacement uploads the new file first, validates the returned item scope and
new attachment ID, then downloads it and compares its bytes. Only then does it
delete the old attachment. Upload or verification failure leaves the old
attachment untouched. A failed verification attempts to remove only the new
attachment. An uncertain upload response, cleanup failure or old-attachment
deletion error can leave duplicate attachments or an uncertain deletion result.
Inspect and resolve these privately before retrying.

Successful writes print JSON containing only `status`, `itemId` and, for
attachments, `attachmentId`. Errors print a refusal naming only the exception
type and the chain of client functions and lines it came through, a cancellation or timeout
status, or an uncertain-write status with the known IDs. The client does not
print item names, JSON, file contents, API responses, exception messages or
tracebacks.
`vault trash` is reversible. There is no general permanent-delete command in
the agent client.

## Uncertain create recovery

Use a unique name for each create. `vault create` validates the returned scope
after the API call. A validation or confirmation failure reports an uncertain
write with the known item ID. The item may already exist, even in the agent's
personal vault. A timeout or malformed response before a usable ID is returned
can leave the same uncertain result without an ID.

Before retrying, use a private operator session to search the account for that
unique name across all orgs, collections, the personal vault and trash.
Permanently delete only exact name matches. Do not delete owner items or publish
item contents. The lifecycle canary uses this exact-name cleanup when create
returns no usable ID, and checks both active and trashed items afterwards.

## Post-merge acceptance

No live rollout or acceptance is part of the code change. Wait for the reviewed
images to build and the approved idle rollout to install them. Do not sync Argo
applications or restart busy pods for this test. Run the following from an
administrative checkout with access to both namespaces. Disable shell tracing
and terminal recording before handling private runtime input. Never print
Secret data, CLI diagnostics, HTTP bodies, tokens or account IDs.

1. A1: Run the synthetic lifecycle once per org. The script loads the installed
   `/usr/local/bin/vault`, checks the runtime identity, creates a uniquely named
   synthetic item, updates its password, attaches a file, forces an HTTP 400 at
   the upload boundary and checks the old bytes remain, then performs a real
   successful replacement. Only the forced upload goes to a temporary local
   refusing HTTP server. All other requests use the real sidecar. It checks
   password, field and attachment reads through a quiet child, trashes the
   item, checks read refusal, and permanently deletes its own synthetic item in
   a `finally` block. It verifies the canary disappeared from active and trash
   lists. Cleanup waits for the item to become locally visible before permanent
   deletion, then forces a sync while checking both lists. Bitwarden's decrypted
   list cache can otherwise retain a permanently deleted item. Permanent deletion
   does not require trash first, so failed lifecycle steps can clean active items.
   If lifecycle and cleanup both fail, the canary reports both exception types,
   lifecycle first, and preserves the original lifecycle failure without printing
   exception messages or values.

   ```sh
   set -euo pipefail
   set +x
   for variant in homelab work; do
     kubectl --context epaflix -n "t3code-$variant" exec -i "$variant-0" -c t3 -- \
       python3 - "$variant" < 2-k3s/13.t3code/env/tools/vault-write-canary.py
   done
   ```

   Expect `vault canary: passed; synthetic item permanently deleted` twice.
   A failed cleanup remains a failed acceptance. Follow [create recovery](#uncertain-create-recovery)
   for that synthetic canary. The orgs may otherwise remain empty until the owner
   moves his folders.

2. A2: Prove the account boundary without `vault`. From the admin shell, pass
   only the current work scope to the homelab vault container. The homelab
   sidecar already has its own account's `vaultwarden-agent` Secret environment.
   The probe logs in with that environment through the stock `bw` CLI in a
   disposable, separate app-data directory. It never uses the running sidecar's
   data directory, the work credentials or an owner's login.

   ```sh
   set -euo pipefail
   set +x
   work_scope=$(kubectl --context epaflix -n t3code-work exec work-0 -c vault -- \
     python3 -c 'from pathlib import Path; print(Path("/run/t3-vault/scope.json").read_text())')
   kubectl --context epaflix -n t3code-homelab exec -i homelab-0 -c vault -- \
     env VAULT_DENIED_SCOPE="$work_scope" python3 - \
     < 2-k3s/13.t3code/env/tools/vault-org-denial.py
   unset work_scope
   ```

   The CLI tries to create a synthetic item with the work org and collection
   IDs, receiving the item document on stdin, not argv. A successful CLI create
   fails acceptance and triggers permanent deletion of that synthetic item.
   A CLI-only refusal, such as a missing org encryption key, is not enough to
   claim a Vaultwarden server rejection.

   The probe therefore also uses the same disposable CLI login's access token
   in memory to authenticate `/api/accounts/profile`, checks the account is not
   a work member, and submits a synthetic `/api/ciphers/create` request naming
   the work org and collection. It uses random synthetic ciphertext, not an
   owner value or foreign encryption key. Vaultwarden must return HTTP 400 or
   403 with a recognized organization or collection permission denial.
   Authentication failures, invalid collection IDs, malformed requests and
   unrelated server errors fail the probe. If the server unexpectedly accepts
   the write, the probe attempts permanent deletion and fails acceptance. If
   that cleanup fails, a private `vault-denial-cleanup-*.json` file in the vault
   container's home records only the created synthetic item ID for operator cleanup.
   The CLI cache and its credentials are removed when the probe exits.

   Expect only `vault org denial: account and server refused`. The probe fails
   closed if the installed CLI token-cache format or server error contract has
   changed. Record the outcome privately as status only. Do not call the
   cross-org criterion complete until this live probe passes.

## Offline checks

```sh
python3 2-k3s/13.t3code/env/tools/vault-client.test.py -v
python3 2-k3s/13.t3code/env/tools/vault-org-denial.test.py -v
python3 2-k3s/13.t3code/env/tools/vault-startup.test.py -v
bash 2-k3s/13.t3code/env/tools/sync-runtime-image.sh
bash 2-k3s/13.t3code/env/tools/runtime-variants.test.sh
bash 2-k3s/13.t3code/env/tools/rollout-hash.test.sh
bash 2-k3s/13.t3code/one/tools/sync-shared.sh --check
```

The fixture runs both lifecycle variants, including failure cleanup, against a
local HTTP API. It covers delayed local-cache visibility, stale decrypted lists,
read-only polling limits, and cleanup without replaying writes. It does not
establish live Vaultwarden account permissions.
