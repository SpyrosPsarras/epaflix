# One Paperless document per Gmail thread: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gmail receipt conversations become one Paperless document each, with later files as versions plus a joined PDF, and later message text as notes.

**Architecture:** `ingest/thread_sync.py` runs every 10 minutes in a `thread-sync` sidecar of the Paperless pod through `manage.py shell`. Pure functions decide what to do. A thin Django and IMAP layer reads Gmail, queues `consume_file` and writes notes and labels. Both Paperless mail rules are disabled.

**Tech Stack:** Python 3.14 in `ghcr.io/paperless-ngx/paperless-ngx:3.4.2`, `imap_tools` 1.15, `pikepdf` 10.13, stdlib `unittest`, kustomize, PrometheusRule with promtool tests.

**Spec:** `docs/superpowers/specs/2026-11-28-paperless-gmail-threads-design.md`

## Global constraints

- Eligible files: PDF, DOC, DOCX, XLS, XLSX, ODT, ODS by MIME type, attached or inline, filename optional.
- Labels: read `paperless-receipt`, `paperless-receipt-body`; skip `paperless-done`, `paperless-done-att`, `paperless-done-eml`; set `paperless-done`.
- Custom field `Gmail thread`, type string. Joined version label `All files: <id>, <id>`.
- Follow-up window `newer_than:7d`. Queued window 6 hours, failed window 24 hours.
- Logs carry Gmail message IDs, document IDs and error types, never mail text or credentials.
- Tests run with `python3 2-k3s/33.paperless/ingest/ingest.test.py -v` and need no Django or network.

## Review focus

1. A run stopped halfway, then run again: no duplicate documents, versions or notes. Pinned by `test_plan_message_idempotent`.
2. Monthly invoices from one sender in one Gmail thread without reply headers: separate documents. Pinned by `test_classify_non_reply_in_known_thread`.
3. HTML-only mail: the note holds readable text, not tags. Pinned by `test_body_text_html_only`.
4. Inline PDF with no filename: imported with a generated name. Pinned by `test_eligible_parts_inline_nameless_pdf`.
5. A file whose import failed: skipped for 24 hours, no done label. Pinned by `test_plan_message_waits_on_failed`.

---

### Task 1: Decision functions

**Files:**
- Create: `2-k3s/33.paperless/ingest/thread_sync.py`
- Test: `2-k3s/33.paperless/ingest/ingest.test.py` (new class `ThreadSyncTests`)

**Interfaces:**
- Produces:
  - `Part(NamedTuple): name: str, mime: str, data: bytes`
  - `eligible_parts(msg: email.message.Message, subject: str) -> list[Part]`; nameless parts get `<subject>.<ext>`, sanitised, numbered when repeated.
  - `body_text(msg) -> str`; plain part first, else HTML stripped with `html.parser`, then `cut_quote`.
  - `cut_quote(text: str) -> str`; stops at `>` lines, `On ... wrote:`, `Den ... skrev`, `-----Original Message-----`, a `From:`/`Fra:` header line.
  - `note_text(sender: str, date: datetime, body: str) -> str` = `f"From {sender}, {date:%Y-%m-%d %H:%M}\n\n{body}"`.
  - `is_reply(msg) -> bool` from `In-Reply-To` or `References`.
  - `classify(labels: set[str], thread_has_document: bool, reply: bool) -> str | None` returning `followup`, `attachments`, `body` or `None`.
  - `Plan(NamedTuple): queue: list[Part], note: str | None, done: bool`
  - `plan_message(kind, parts, eml: Part, note, present: set[str], queued: set[str], failed: set[str], has_document: bool) -> Plan`; `present` holds SHA-256 hex of files in Paperless, `queued` and `failed` hold filenames.
  - `joined_label(ids: list[int]) -> str`, `needs_join(originals: list[tuple[int, bool]], newest_label: str | None) -> list[int] | None`.
  - `parse_gm_fetch(lines) -> dict[str, tuple[str, str, set[str]]]` mapping UID to `(msgid, thrid, labels)`.

- [ ] **Step 1: Write failing tests.** Assertions:
  - `test_eligible_parts_inline_nameless_pdf`: inline `application/pdf` part without filename, subject `Faktura 12` gives `[Part('Faktura 12.pdf', 'application/pdf', data)]`. An inline `image/png` logo gives nothing. `application/octet-stream` named `x.docx` is eligible.
  - `test_cut_quote`: each marker cuts; text without markers is unchanged and stripped.
  - `test_body_text_html_only`: `<p>Hi<br>there</p><blockquote>old</blockquote>` gives text containing `Hi` and `there`, no `<`.
  - `test_classify_non_reply_in_known_thread`: labelled `paperless-receipt`, known thread, not reply gives `attachments`. Reply in known thread with no label gives `followup`. Unlabelled non-reply gives `None`. Body label gives `body`.
  - `test_plan_message_idempotent`: all checksums present and note given gives `queue == []`, `done is True`. New thread with two parts and no document queues only the first, `done is False`.
  - `test_plan_message_waits_on_failed`: a part in `failed` is not queued and `done is False`; a part in `queued` likewise.
  - `test_plan_body_and_fallback`: kind `body` queues the eml when absent; kind `attachments` with no parts behaves like `body`; kind `followup` never queues the eml.
  - `test_needs_join`: one original gives `None`; two ready originals with a different newest label give both IDs; any original without PDF gives `None`; label already `All files: 1, 2` gives `None`.
  - `test_parse_gm_fetch`: a sample Gmail `FETCH` line with `X-GM-MSGID`, `X-GM-THRID` and `X-GM-LABELS ("\\Important" paperless-receipt)` parses to the tuple.
- [ ] **Step 2: Run tests, expect failures** (`load('thread_sync')` fails).
- [ ] **Step 3: Implement the functions.** No Django imports at module level.
- [ ] **Step 4: Run tests, expect PASS.**
- [ ] **Step 5: Commit** `feat(paperless): decide Gmail thread actions in pure functions`.

### Task 2: Paperless and Gmail glue

**Files:**
- Modify: `2-k3s/33.paperless/ingest/thread_sync.py`
- Test: `ingest.test.py` class `ThreadSyncRuntimeTests` with fake mailbox and fake Paperless store

**Interfaces:**
- Consumes: Task 1 functions.
- Produces: `main()` run by `manage.py shell` under `if __name__ in ('__main__', 'django.core.management.commands.shell')`, exiting non-zero on a run-level error. `sync(mailbox, store, now) -> Counter` where `store` exposes `document_for_thread(thrid) -> int | None`, `present(checksums) -> set[str]`, `queued(names) -> set[str]`, `failed(names) -> set[str]`, `queue(part, root_id, thrid, title)`, `add_note(doc_id, text)`, `join_candidates() -> list[int]`, `originals(doc_id)`, `newest_label(doc_id)`, `queue_join(doc_id, ids)`; `mailbox` exposes `search(query) -> list[str]`, `fetch(uids)`, `mark_done(uid)`.

- [ ] **Step 1: Failing runtime test** `test_sync_thread_end_to_end`: run 1 with message A (labelled, PDF) creates one queue call with `root_id=None` and the thread field; no done label. Run 2 with A's checksum present plus reply B (no label, `In-Reply-To`, PDF) labels A done, adds B's note, queues B's PDF with `root_id` set. Run 3 with B present labels B done and calls `queue_join` with both IDs once both originals report PDFs. A message raising inside handling increments `failed` and other messages still finish.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** `sync`, a `DjangoStore` using `Document.global_objects` checksums, `PaperlessTask` filename windows, `Note.objects.create(user=owner)`, `consume_file.apply_async` with `DocumentMetadataOverrides(title, owner_id, tag_ids, custom_fields, version_label)` and `ConsumableDocument(source=DocumentSource.MailFetch, original_file, root_document_id)`, and a `GmailBox` on `[Gmail]/All Mail` with `X-GM-RAW` searches and `+X-GM-LABELS (paperless-done)`. The joined PDF uses `pikepdf` with `static_id=True` so a rebuild has the same checksum. One summary line per run.
- [ ] **Step 4: Run tests, expect PASS.**
- [ ] **Step 5: Commit** `feat(paperless): sync Gmail threads into one document each`.

### Task 3: Deployment, setup and alert

**Files:**
- Modify: `2-k3s/33.paperless/paperless.yaml` (container `thread-sync`), `kustomization.yaml` (`configMapGenerator` `paperless-thread-sync` from `ingest/thread_sync.py`), `ingest/configure.py`, `ingest.test.py` (`ConfigureRuntimeTests`), `2-k3s/10.observability/alertmanager-config/custom-alerts.yaml` and `custom-alerts-tests.yaml`.

- [ ] **Step 1: Failing test** in `ConfigureRuntimeTests`: both rules get `enabled: False`; custom field `Gmail thread` is created with data type `string`.
- [ ] **Step 2: Implement** the configure change. Sidecar: same image, `workingDir`, env and volume as the main container, command `sh -c 'while python3 manage.py shell < /ingest/thread_sync.py; do sleep 600; done'`, requests 20m CPU and 128Mi, limit 512Mi, same securityContext as share-lists. Alert `PaperlessThreadSyncRestarting`: `increase(kube_pod_container_status_restarts_total{namespace="paperless",container="thread-sync"}[15m]) >= 3`, warning, with a promtool test.
- [ ] **Step 3: Verify** `kustomize build 2-k3s/33.paperless` renders, the ingest tests pass, and the CI promtool steps pass locally.
- [ ] **Step 4: Commit** `feat(paperless): run Gmail thread sync next to Paperless`.

### Task 4: Migration of existing documents

**Files:**
- Create: `2-k3s/33.paperless/ingest/thread_migrate.py`; test in `ingest.test.py`.

**Interfaces:**
- Produces: `match(rows, docs) -> dict[int, str]` mapping document ID to thread ID by original filename and one hour after `processed`; `plan(threads) -> list[tuple[str, int, list[int]]]` with action `note` or `version`, keeping the oldest document. `main()` prints the plan with IDs only; `--apply` performs it.

- [ ] **Step 1: Failing tests** `test_match_by_filename_and_time` and `test_plan_keeps_oldest`.
- [ ] **Step 2: Implement.** Apply sets `Gmail thread`, adds notes from the email documents' Gmail text, deletes those documents, and calls `merge_as_versions` for later files.
- [ ] **Step 3: Run tests, expect PASS. Commit** `feat(paperless): one-off migration of existing Gmail documents to threads`.

### Task 5: Docs

- [ ] Update README sections Email and Drive import, Mail rules and Configure Gmail, plus the revision note in `2026-11-10-paperless-ingest-design.md`. Commit `docs(paperless): Gmail thread sync`.
