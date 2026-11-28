# One Paperless document per Gmail thread

## Intent

A receipt conversation in Gmail becomes one Paperless document. The first receipt mail creates it. Later files in the same conversation become new files of that document, and the text of every later message becomes a note on it. The owner sees one entry per purchase, not one per email.

This replaces the two Paperless mail rules from `2026-11-10-paperless-ingest-design.md` (Revision 2026-11-18). The Gmail filters and labels stay as they are.

## Problem

On 2026-11-27 a shop sent an invoice PDF. Paperless imported it as document 26843. Two replies followed, one from the owner and one from the shop. Both have "invoice" in the subject and no attachment, so the body filter labelled them and Paperless imported each as an email page: documents 26844 and 26845. A third reply was labelled and waiting. Out of 835 email-page imports, 64 are replies or forwards like these.

## Goals

- G1. One document per receipt conversation. Its first file is the first receipt file, or the email page when the first receipt mail has no file.
- G2. A later PDF or Office file in the conversation becomes a new file of that document. The preview, the phone app and full-text search cover every file.
- G3. The text of each later message, the owner's included, becomes a note on that document. Quoted history is cut.
- G4. PDFs sent inline or without a filename are imported. Today the attachment rule skips them (15 mails).
- G5. Every handled message is visible: Gmail gets a done label, and each run logs one summary line.

## Non-goals

- Pictures inside reply text. Only text goes into notes. Attached files are kept.
- Rerunning paperless-gpt when a later file arrives. Title, shop and `Total` stay as set from the first file. The owner edits them by hand.
- Drive import, phone scans and uploads. They do not change.

## Findings that shape the design

- F1. Paperless does not record which mail a document came from. Its mail rules create documents, but nothing links a document to its Gmail message or thread. A separate script cannot attach a later message to a document a mail rule created. So the new script imports Gmail receipts itself and the two mail rules are disabled.
- F2. Paperless 3.4.2 can hold several files per document as versions. The newest version is the one Paperless shows and indexes. `merge_as_versions` takes older versions out of the search index. Plain versions would hide earlier files, so the script adds one more version that joins all files into one PDF.
- F3. Redis runs as a sidecar on `127.0.0.1` in the Paperless pod. Only containers in that pod can queue Paperless consume tasks.
- F4. Gmail groups mails by subject, so monthly invoices with the same subject from one sender can share a thread. Thread ID alone would merge different months into one document. When a thread has several documents, replies go to the newest one.

## Design

### Where it runs

A second container, `thread-sync`, in the Paperless pod. It uses the Paperless image, environment and volume, and runs `ingest/thread_sync.py` through `manage.py shell` every 10 minutes. A kustomize `configMapGenerator` ships the script. It reads the Gmail login from Paperless mail account `gmail`, so no new secret exists. It queues imports on Paperless's own task queue with `consume_file`, like an upload from the UI. It imports nothing while mail rules named `gmail-receipt-bodies` or `gmail-attachments` exist or an old-rule import from the last 24 hours is still queued. The migration renames the rules to `… (retired)` as its last step, so the sync starts only after existing documents carry `Gmail thread`. The 24-hour window skips 1,714 tasks from the 2026-11-18 backlog that stay pending while the queue is empty. It looks up the `Gmail thread` field and never creates it.

The script fails a message, not the run. A message that hits an error keeps no done label, is logged with its Gmail message ID and is retried next run. A message whose Paperless import failed gets `paperless-failed` and waits until the owner removes that label. An error outside a message, such as a lost IMAP connection, exits the container, and Kubernetes restarts it.

### State

- Gmail thread to document: custom field `Gmail thread` (string) on the document, set at import.
- Message handled: Gmail label `paperless-done` on the message. Import failed: `paperless-failed`. Old mail keeps `paperless-done-att` and `paperless-done-eml`, and the script skips those too.
- File already imported: Paperless checksum, SHA-256 of the original file. A file whose checksum exists on any document or version, trash included, is not imported again.
- File still queued: a pending or started `consume_file` task from the last 6 hours with the same filename in `PaperlessTask.input_data`. The script waits for it. Queued files are named `<first 12 hex of SHA-256>-<name>`, so two mails with `invoice.pdf` never collide. A failed task for that file in the last 24 hours puts `paperless-failed` on the message.
- Note already written: a note on the document with the same text.

Every step checks this state first, so a run can stop anywhere and the next run picks up.

### Which messages each run reads

From `[Gmail]/All Mail`, oldest first, without `paperless-done`, `paperless-failed`, `paperless-done-att` or `paperless-done-eml`:

1. `label:paperless-receipt OR label:paperless-receipt-body`, any age. This covers labels the owner adds by hand to old mail.
2. `newer_than:7d`, for later messages in conversations that already have a document. Unlabelled messages, such as the owner's own replies, are found this way.

A message is a follow-up when its Gmail thread has a document and the message has an `In-Reply-To` or `References` header (F4). Other messages in group 2 are left alone. While a new document for a thread is still queued, every message of that thread waits, so a reply never lands on the previous document by mistake. A mail finds its own document by the document's own file, not by a version, so an unchanged terms PDF shared with last month's invoice does not pull the new invoice into last month's document.

### What happens to a message

| Message | Action |
|---|---|
| Follow-up | Text becomes a note. Each eligible file becomes a version, labelled with its filename. |
| Labelled `paperless-receipt` or `paperless-receipt-body`, not a follow-up | The first eligible file creates the document. The other files become versions on the next run, when the document exists. Text becomes a note. With no eligible file, the raw mail is imported as `.eml`, which Paperless turns into the email page as today. The Gmail filters check filenames, so a nameless inline PDF can carry the body label; the script still finds it. |

Eligible files are PDF, DOC, DOCX, XLS, XLSX, ODT and ODS, detected by MIME type, attached or inline, with or without a filename (G4). New documents get title from subject, owner, tags `source:gmail` and `paperless-gpt-auto`, and the `Gmail thread` field. Paperless skips workflows for versions (`documents/signals/handlers.py`, `run_workflows`), so versions never reach paperless-gpt.

A note starts with the sender and date, then the plain-text body with quoted history cut. The cut stops at the first quote marker: lines starting with `>`, `On ... wrote:`, `Den ... skrev`, `-----Original Message-----`, or a `From:`/`Fra:` header block.

The message gets `paperless-done` once every file is imported and the note exists. A message with an import still queued waits for the next run.

### Joined file

The join pass looks at documents with `Gmail thread` and versions whose modified time is in the last 2 days. Drive imports and uploads are never joined. When a document has two or more original files, each with a PDF, and its newest version does not already join all of them, the script builds one PDF from them in version order. Original PDFs are used as they are. Office files and email pages use Paperless's archive PDF. It imports that PDF as the newest version, labelled `All files: <document IDs>`, or `All files: <count> files #<hash>` when the IDs pass the 64-character label limit. The label shows which files it holds and tell the next run whether it is current. Originals stay as older versions.

### Existing documents

A one-off script, `ingest/thread_migrate.py`, dry-run by default:

1. It reads every Gmail message with `paperless-done-att` or `paperless-done-eml` and matches it to its documents by SHA-256. Paperless stored the attachment bytes and the email with `From` moved first, which is what `eligible_parts` and `eml_bytes` produce. A check on 2026-11-28 matched documents 26843, 26844 and 26845 exactly.
2. It applies the same rules as the sync, oldest message first. A message that starts a conversation keeps its document and gets `Gmail thread`. A reply's email page becomes a note on that document and goes to the trash. A reply's files become versions through `merge_as_versions`. The sync's join pass then builds the joined file, because merging updates the document's modified time.
3. The dry run prints the plan, document IDs only, and a fingerprint of its version and delete actions. `THREAD_MIGRATE_APPLY=<fingerprint>` turns the old rules off, waits for their queued imports, plans once more over all messages and applies only when the fingerprint matches what the owner approved. Then it renames the rules. Trashed documents stay restorable for 30 days.

### Mail rules and setup

`ingest/configure.py` creates the `Gmail thread` custom field and keeps the mail account setup. It no longer creates mail rules. The retired rules stay in Paperless so Processed Mails keeps its history.

### Observability

- Each run logs one line: messages read, documents created, versions, joined files, notes, done labels, failures.
- Each failure logs the Gmail message ID and the error type, never mail text.
- `PaperlessThreadSyncRestarting` warns when `thread-sync` restarts at least three times in 15 minutes, like `PaperlessConverterRestarting`.

### Docs

README sections Email and Drive import, Gmail threads, Migrate existing Gmail documents and Configure Gmail describe the new flow, the labels and how to re-import a message: remove `paperless-done`, delete the document or version, and the next run imports it again.

## Risks

- R1. Messages more than 7 days late after an outage miss group 2. Labelled ones still arrive through group 1. Fix by adding the label by hand, or widening the window for one run.
- R2. The quote cut misses a mail client's format, and a note carries the quoted history. Notes get longer but nothing is lost.
- R3. A reply that carries a new, unrelated receipt becomes a version of the old document. The owner splits it in the UI.
- R4. Documents the migration cannot match by checksum keep working as single documents.
- R5. The `thread-sync` container restarts Paperless's pod when its script changes, about one minute of downtime per change.

## Left to the builder

Checked in 3.4.2: `consume_file` accepts `root_document_id` and `custom_fields`, workflows skip versions, and the image ships `imap_tools` 1.15.0 and `pikepdf` 10.13.

- Tests cover message classification, eligible-file detection, quote cutting, joined-file decision and the done-label rule, with no network.
