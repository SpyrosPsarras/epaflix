# Import all Gmail and Drive documents into Paperless

## Intent

Paperless becomes the owner's single source of truth for documents and
receipts. Everything he already keeps in his personal Gmail and Google
Drive moves in once, and new Gmail documents keep arriving on their own.
He confirmed that the personal account holds no work data.

This reverses one non-goal of
`2026-11-08-receipts-design.md`, "Email ingestion". Everything else in
that design stays.

## Goals

- G1. Every attachment of every Gmail message, from all years, lands in
  Paperless. New ones arrive within the 10-minute mail check.
- G2. Receipts that exist only as an email body land in Paperless as a
  PDF of the email.
- G3. Every Drive file the owner owns lands in Paperless once. Google
  Docs, Sheets and Slides arrive as PDFs.
- G4. The owner owns every imported document. Other Paperless users do
  not see them.
- G5. Each document shows where it came from: tag `source:gmail` or
  `source:drive`.
- G6. Gmail and Drive keep every original. The import only adds Gmail
  labels.
- G7. Only receipt emails go through paperless-gpt. Other imports keep
  their email subject or Drive name as title and create no shared
  correspondents.

## Non-goals

- Deleting or moving originals. That waits until files sit on the
  6×14 TB pool with an off-site copy.
- Ongoing Drive sync. New documents go to Paperless through the app,
  the web UI or email.
- Drive files shared with the owner by other people.
- Gmail Spam and Trash.
- File types Paperless cannot read, such as `.apk`, `.zip`, video and
  audio. They are skipped and listed.
- Removing the duplicate that appears when a receipt email has an
  attachment: its PDF email and its attachment both land.
- Other people's accounts.

## Anti-goals

- Any mail deleted or moved in Gmail, or any Drive file changed.
- An imported document visible to another Paperless user.
- The Gmail app password or a Paperless token in Git, chat or logs.
- Document content passing through the agent's model context or a
  cloud AI.

## Constraints

- Paperless-ngx 3.4.2. Turning an email into a PDF needs Tika and
  Gotenberg (`docs/configuration.md#tika` at tag v3.4.2). The deployment
  runs neither today.
- The NetworkPolicy limits only incoming traffic, so Paperless can reach
  `imap.gmail.com:993`.
- From outside the `paperless` namespace, Paperless is reachable only
  through `https://paperless.epaflix.com`.
- Mail accounts and mail rules live in the Paperless database, not in
  Git, like the custom field `Total`.
- A mail rule with `maximum_age = 0` has no age limit
  (`src/paperless_mail/mail.py`, `make_criterias`).
- The rule action "tag" sets a real Gmail label (`X-GM-LABELS`) and
  skips mail that already carries it. Two rules on the same mail need
  two different labels, or the first hides the mail from the second.
- Unsupported attachment types are logged and skipped. They do not
  stop the rule.
- Imported mail gets the rule's owner when the rule has "assign owner
  from rule" set.
- Checked over IMAP on 2026-11-10: the all-mail folder is
  `[Gmail]/All Mail`, and the server reports `X-GM-EXT-1`, so labels
  work.
- The Drive tool's download link points at `localhost:8001` inside the
  homelab Google pod (`2-k3s/23.mcp-hub/instances/google/`). Only the
  authenticated gateway on port 8000 is exposed, and it serves `/mcp`
  only. Checked on 2026-11-10: the link is unreachable from the agent
  pod and the gateway answers `401`.
- The homelab and work hubs run the same code from
  `2-k3s/23.mcp-hub/files/`. The work hub has no Google service.
- The raw Workspace server keeps each download in its `/tmp` emptyDir
  for one hour. That emptyDir has no size limit and sits on the node
  disk.
- The receipts workflow tags every new document `paperless-gpt-auto`.
  paperless-gpt then creates correspondents, and the
  `paperless-share-lists` CronJob makes them visible to every user.
  Paperless 3.4.2 workflows with the "document added" trigger support
  `filter_has_not_tags`.
- Measured on 2026-11-10: at least 500 Gmail messages carry
  attachments, 24 of them larger than 10 MB. Drive holds at least 100
  files, the largest seen 133 MB. The Paperless volume has 9.0 TB free.

## Approaches

The owner picked O1.

- O1, chosen. The Paperless mail fetcher reads Gmail itself, now and
  for new mail. A one-off agent script copies Drive files one at a time
  and deletes each local copy after upload. Local disk use stays near
  one file, and no content passes through the agent.
- O2. Google Takeout export, imported once, plus the mail fetcher for
  new mail. Rejected: the export may be tens of GB, does not fit in
  `/tmp` (20 GiB cap), and needs an mbox unpacking script.
- O3. The agent fetches every file through its Gmail and Drive tools.
  Rejected: every document passes through the agent, and it is slow and
  rate-limited.

For the Drive downloads the owner picked O7.

- O6. rclone with a read-only token the owner creates. A separate
  Google grant for a one-off job.
- O7, chosen. Make the Drive tool's download link reachable from the
  agent pod through the homelab hub, with the hub's existing auth.
- O8. Google Takeout of Drive only. Needs space for the whole export.

For paperless-gpt the owner picked O9.

- O9, chosen. Only receipt emails go through paperless-gpt.
- O10. All imports. Receipt prompts give contracts a shop and `Total`,
  and correspondent names such as banks and doctors become visible to
  every user.
- O11. No imports go through paperless-gpt.

## Design

### Tika and Gotenberg

- Two more containers in the `paperless` pod, reached on `localhost`
  like Valkey: `apache/tika` and `gotenberg/gotenberg`, versions from
  the upstream `docker-compose.sqlite-tika.yml` at v3.4.2, pinned by tag
  and digest.
- Gotenberg runs with `--chromium-disable-javascript=true` and
  `--chromium-allow-list=^file:///tmp/.*`, as upstream does, so tracking
  pixels and scripts in emails do not load.
- `PAPERLESS_TIKA_ENABLED=true`, `PAPERLESS_TIKA_ENDPOINT` and
  `PAPERLESS_TIKA_GOTENBERG_ENDPOINT` point at the two containers.
- This also lets Paperless read Office files from Gmail and Drive.

Tika and Gotenberg have liveness probes and no readiness probes, so a
failure in either does not take the web UI or uploads offline.

### Gmail account

- A vault item holds the Gmail address as username and the app password
  `paperless`. Done by the owner on 2026-11-10; an IMAP login test
  passed.
- The agent creates the Paperless mail account with `manage.py shell`
  inside `vault run`, piping the password to `kubectl exec -i` on stdin.
  The password never appears in arguments or output, as with the
  paperless-gpt token.
- Server `imap.gmail.com`, port 993, SSL, account type IMAP.
- Gmail OAuth (supported by Paperless) was not chosen: it needs a
  Google Cloud OAuth app and consent screen for the same mailbox access.

### Receipt label in Gmail

- A Gmail filter adds label `paperless-receipt` to new mail matching
  receipt words in the four receipt languages: receipt, invoice, order
  confirmation, kvittering, faktura, ordrebekreftelse, Rechnung,
  Quittung, Bestellbestätigung, απόδειξη, τιμολόγιο.
- The agent runs the same search once over existing mail and adds the
  label in batches through the Gmail API. It handles message IDs only,
  never content.
- The owner adds the label by hand to any receipt the filter missed.
- Creating the filter and labelling existing mail changes Gmail, so the
  agent asks the owner first.

### Mail rules

Both rules: owner is the owner's Paperless user, assign owner from
rule, `maximum_age` 0, action "tag", tag `source:gmail`, correspondent
"do not assign", title from subject. Rule 1 also
assigns `paperless-gpt-auto`, so receipt emails get AI fields.

| Order | Name | Folder | Scope | Gmail label set after import |
|---|---|---|---|---|
| 1 | `gmail-receipt-bodies` | `paperless-receipt` | email as `.eml` only | `paperless-done-eml` |
| 2 | `gmail-attachments` | the all-mail folder | attachments only, no inline files | `paperless-done-att` |

Skipping inline files keeps logos and signature images out. The first
mail check works through the whole backlog. Later checks only see new
mail, because each imported message carries its rule's label.

### paperless-gpt scope

The receipts workflow skips documents tagged `source:gmail` or
`source:drive`, through `filter_has_not_tags`. Phone and web uploads
keep working as today. If the live workflow uses a trigger without that
filter, the builder switches it to "document added" and checks that
phone uploads still get tagged.

### Drive download route in the hub

- The homelab hub gets a download path for Drive links, for example
  `/drive/attachments/<id>`. It uses the same client bearer auth as the
  hub's MCP paths. The hub forwards it to the Google gateway with the
  upstream bearer, and the gateway forwards it to
  `http://127.0.0.1:8001/attachments/<id>`.
- The raw server builds links with the hub's internal URL, so the tool
  prints a link the agent pod can open.
- Only the `drive` upstream gets the path. The work hub behaves exactly
  as before, and its tests prove it. Its pods restart once, because the
  shared code ConfigMap changes.
- The path accepts only the attachment ID format, `GET` only, and
  streams the file without buffering it in memory.
- The raw container's `/tmp` emptyDir gets a size limit. The Drive
  script keeps under half that limit downloaded in any rolling hour,
  because files stay there for one hour.
- Hub selftests cover: no bearer gets `401`, a work or wrong client
  gets `401`, a bad ID gets `400` or `404`, and a valid download
  streams.

### Drive import

A one-off script run by the agent:

1. List Drive files where the owner is an owner, not trashed, not
   folders. Write a manifest to a private `mktemp -d` directory with
   file ID, name, type, size and status.
2. Report the count, total size and skipped types to the owner before
   uploading.
3. For each file: get a download link from the Drive tool (Docs,
   Sheets and Slides exported as PDF), `curl` it through the hub path
   with the agent's hub bearer into the private directory, check free
   space,
   upload it to `https://paperless.epaflix.com/api/documents/post_document/`
   with tag `source:drive` and the Drive name as title, record the
   Paperless task ID and delete the local file.
4. A rerun skips files the manifest marks done.
5. Report counts of uploaded, skipped and failed files.

- The upload uses the owner's own Paperless API token, from the
  password of a vault item with no username. Checked on 2026-11-10: it
  belongs to the owner's superuser SSO account.
- The script reads only the link from the tool result and never
  returns file content to the model. If the tool returns content inline,
  the script stops and reports.

### Observability

- Tags `source:gmail` and `source:drive` on every document.
- Paperless "Processed Mail" lists each handled message per rule. The
  Paperless task list shows failed imports.
- Mail fetch and Tika or Gotenberg errors reach Loki through Alloy.
- The Drive manifest is the audit trail of the one-off import.
- Alert when the Tika or Gotenberg container restarts repeatedly,
  next to the existing `paperless` availability alert.

### Docs

- `2-k3s/33.paperless/README.md` gets an "Email and Drive import"
  section: the vault items, the two rules, the Gmail labels, how to
  re-import one mail, and how to rotate the app password.
- `2026-11-08-receipts-design.md` points at this file under Non-goals.

## Risks

- R1. All documents now live on one disk until the pool move. The
  sanoid snapshots undo deletions, not a dead disk. This weighs more
  now that Paperless is the source of truth.
- R2. Receipt emails from all years go through the shared Ollama GPU.
  The backlog can take hours and delays Lingarr and Open WebUI.
- R7. The hub change touches code shared with the work hub and the
  path that holds Google credentials. A mistake could expose Drive
  files to another client. The review checks auth on the new path
  and the work hub render.
- R3. The attachment rule also imports junk such as calendar files,
  banners and tiny images attached as real files. The owner deletes them
  in Paperless, or the builder adds a filename exclusion.
- R4. The receipt filter misses some receipts and catches some
  marketing mail.
- R5. The app password opens the whole mailbox. Paperless keeps it in
  its database, which CNPG backs up to MinIO.
- R6. The first mail check over all years runs long and may hit Gmail
  IMAP limits. The next check continues where it stopped, because done
  mail carries a label.

## Left to the builder

- Tika and Gotenberg resource requests and limits. The pod runs on a
  worker with about 22 GiB allocatable memory.
- The final receipt word list.
- The Drive script's language and layout.
- Whether Traefik or Cloudflare limits upload size below 133 MB. If
  so, upload through the LAN Traefik address.
- The `/tmp` size limit for the raw Workspace container.
- How the script reads the agent's hub bearer without printing it.
