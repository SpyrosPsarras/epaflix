# Paperless Gmail and Drive import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import every Gmail attachment, every receipt email and every owned Drive file into Paperless, and keep Gmail flowing in.

**Architecture:** Paperless gets Tika and Gotenberg and reads Gmail over IMAP through two mail rules. The homelab MCP hub gets an authenticated download path for Drive links. A one-off script copies Drive files one at a time from that path into Paperless.

**Tech Stack:** Kubernetes manifests (Argo CD), Python 3 stdlib, `mcp==2.2.0` and `httpx` in a venv, Paperless-ngx 3.4.2 Django shell, promtool.

**Spec:** `docs/superpowers/specs/2026-11-10-paperless-ingest-design.md`

## Global Constraints

- Paperless-ngx `3.4.2`. Tika `docker.io/apache/tika:3.3.1.0` and Gotenberg `docker.io/gotenberg/gotenberg:8.38`, each pinned by tag and digest.
- No Gmail address, username, account ID or secret in Git. Scripts take them at run time.
- Secrets reach commands only through `vault run`. `vault run` cannot nest, and it drops stdout and stderr, so scripts write results to a private `mktemp -d` directory.
- Tags `source:gmail`, `source:drive`. Gmail labels `paperless-receipt`, `paperless-done-eml`, `paperless-done-att`. Mail account `gmail`. Rules `gmail-receipt-bodies` (order 1) and `gmail-attachments` (order 2).
- Vault items for the Gmail app password and the owner's Paperless token, named only in placeholders such as `<gmail-imap-item>` and `<paperless-token-item>`. Agent hub bearer is env `MCP_HUB_TOKEN`.
- Hub download path `/drive/attachments/{file_id}`. `file_id` is a canonical lowercase UUID.
- The work hub's routes and render stay unchanged.
- Nothing in Gmail is moved or deleted, and no Drive file changes.

## Review Focus

1. A request to `/drive/attachments/...` with no bearer, a work bearer or the wrong bearer must get `401` and never reach the gateway. Test: Task 1 `test_drive_attachment_auth`.
2. A file ID like `../mcp` or `%2e%2e` must get `404` without an upstream call. Test: Task 1 `test_attachment_rejects_non_uuid`.
3. A Drive file name containing quotes and `(ID: ` must still parse to the right ID. Test: Task 3 `test_parse_page_tricky_name`.
4. A rerun after a crash must not upload a file twice. Test: Task 3 `test_resume_skips_done`.
5. A burst of large files must not pass 1 GiB downloaded in any hour, so the raw container's 2 GiB `/tmp` never fills. Test: Task 3 `test_budget_waits`.

---

### Task 1: Hub download path for Drive links

**Files:**
- Modify: `2-k3s/23.mcp-hub/files/upstream.py`
- Modify: `2-k3s/23.mcp-hub/files/mcp_hub.py` (`build_app`, `instance_app`)
- Modify: `2-k3s/23.mcp-hub/instances/google/gateway.py`
- Modify: `2-k3s/23.mcp-hub/instances/google/workspace.yaml` (raw container env, `raw-tmp` volume)
- Modify: `2-k3s/23.mcp-hub/instances/README.md` (Google section)
- Test: `2-k3s/23.mcp-hub/tests/test_google.py` (`GatewayTests`), `2-k3s/23.mcp-hub/tests/test_instances.py`

**Interfaces:**
- Produces: `upstream.attachment_route(path: str, upstream: Upstream) -> Route`. `GET` only. Target URL is `upstream.url` with its trailing `/mcp` replaced by `/attachments/<file_id>`. Sends only the upstream credential headers. Streams the body back with `content-type`, `content-disposition` and `content-length`. Returns `404` without an upstream call when `file_id` is not a canonical UUID. Passes through upstream `200` and `404`. Upstream `401`/`403`, other statuses and network errors become a plain-text `502`.
- Produces: `build_app(..., extra_routes=())`, appended after the upstream routes, behind the same `BearerAuth`.
- Produces: the hub serves `/drive/attachments/{file_id}` only when the instance's services include `drive`. The gateway serves `/attachments/{file_id}`.

- [ ] **Step 1: Write the failing tests**

In `GatewayTests`, add `test_attachment_forward`. The fake raw app gets `Route("/attachments/{file_id}", ...)`, which returns bytes `b"%PDF-fixture"` with `content-disposition: attachment; filename="x.pdf"`. Assert:
- each bearer from the existing bad list gets `401` and the raw app records nothing;
- a valid bearer and the UUID `"0f8fc6d3-2f5e-4c1a-9b7e-3d2a1c0b9e8f"` gets `200`, the body `b"%PDF-fixture"` and the disposition header, and the forwarded request has no `authorization` or `cookie`;
- `POST` gets `405`.

Add `test_attachment_rejects_non_uuid`: `"..%2fmcp"`, `"x"`, the UUID in upper case and the UUID plus `"a"` each get `404`, and the raw app records nothing.

In `test_instances.py`, add `test_drive_attachment_auth`. The homelab instance has services `["searxng","gmail","drive"]` and a fake gateway transport.
- `/drive/attachments/<uuid>` with the homelab client bearer reaches the fake gateway at `/attachments/<uuid>` with `Authorization: Bearer <upstream token>`, and streams `200`.
- With no bearer, a `work.` bearer or a wrong bearer, it gets `401` and the fake gateway records nothing.
- `/gmail/attachments/<uuid>` gets `404`.
- A work instance with services `["searxng","notion"]` returns `404` for `/drive/attachments/<uuid>`.

- [ ] **Step 2: Run the tests and see them fail**

Run: `/tmp/mcp-hub/bin/python -m unittest discover -s 2-k3s/23.mcp-hub/tests -v -k attachment`
Expected: FAIL, routes missing.

Create the venv the way `.github/workflows/ci.yml` line 329 does if it is absent.

- [ ] **Step 3: Implement `attachment_route`, `extra_routes`, the gateway route and the instance wiring**

- Validate with `uuid.UUID(file_id)` plus `str(...) == file_id`.
- Stream the body the way `route()` does.
- `instance_app` passes `extra_routes=[attachment_route("/drive/attachments/{file_id}", drive_up)]` only when `drive` is in the services.
- `gateway.app` passes `extra_routes=[attachment_route("/attachments/{file_id}", up)]`.

- [ ] **Step 4: Set the link base and cap the download folder**

In `workspace.yaml`, on the `raw` container:
- add env `WORKSPACE_EXTERNAL_URL` = `http://mcp-hub.mcp-hub-homelab.svc.cluster.local:8000/drive`;
- set `raw-tmp` to `emptyDir: {sizeLimit: 2Gi}`.

Add one README paragraph: the path, its auth, the one-hour expiry and the 2 GiB cap.

- [ ] **Step 5: Run the full hub suite and the selftests**

Run: `/tmp/mcp-hub/bin/python -m unittest discover -s 2-k3s/23.mcp-hub/tests -v && /tmp/mcp-hub/bin/python 2-k3s/23.mcp-hub/files/mcp_hub.py --selftest && kustomize build 2-k3s/23.mcp-hub/instances/homelab >/dev/null && kustomize build 2-k3s/23.mcp-hub/instances/work >/dev/null`
Expected: all OK. `git diff` shows no change under `instances/work`.

- [ ] **Step 6: Commit**

Include the two spec files already in the worktree: `docs/superpowers/specs/2026-11-10-paperless-ingest-design.md` and the pointer in `2026-11-08-receipts-design.md`. Also include this plan.

`git commit -m "feat(mcp-hub): authenticated Drive download path on the homelab hub"`

---

### Task 2: Tika and Gotenberg in Paperless, with alert

**Files:**
- Modify: `2-k3s/33.paperless/paperless.yaml`
- Modify: `2-k3s/10.observability/alertmanager-config/custom-alerts.yaml` (Paperless group)
- Modify: `2-k3s/10.observability/alertmanager-config/custom-alerts-tests.yaml`

**Interfaces:**
- Produces: Tika on `http://127.0.0.1:9998`, Gotenberg on `http://127.0.0.1:3000`, in the `paperless` pod.

- [ ] **Step 1: Write the failing alert tests**

Add alert `PaperlessConverterRestarting` with three test groups:
- `kube_pod_container_status_restarts_total{namespace="paperless",container="tika"}` rising by 3 within 15 minutes fires, with labels `severity: warning`, `component: paperless`;
- the same for `container="gotenberg"`;
- a flat counter stays silent.

Copy the summary and description strings from the rule into the tests.

- [ ] **Step 2: Run promtool and see the tests fail**

Run the same commands as the `Alert rule tests (promtool)` step in `.github/workflows/ci.yml` around line 294.
Expected: FAIL, alert unknown.

- [ ] **Step 3: Add the rule**

Expression: `increase(kube_pod_container_status_restarts_total{namespace="paperless",container=~"tika|gotenberg"}[15m]) >= 3`, with `for: 0m`.

Summary: `"Paperless {{ $labels.container }} keeps restarting"`.

Description: `"{{ $labels.container }} restarted 3 times in 15 minutes. Emails and Office files wait in the Paperless task list until it recovers. Check the paperless pod in namespace paperless."`

- [ ] **Step 4: Add the containers and env**

- Two containers in the `paperless` pod:
  - `tika`, image `docker.io/apache/tika:3.3.1.0@<digest>`;
  - `gotenberg`, image `docker.io/gotenberg/gotenberg:8.38@<digest>`, with args `gotenberg --chromium-disable-javascript=true --chromium-allow-list=^file:///tmp/.* --api-port=3000`.
- Both get `allowPrivilegeEscalation: false`, drop all capabilities, a `livenessProbe` (Tika: `tcpSocket` 9998; Gotenberg: `httpGet /health` 3000), and **no** `readinessProbe`.
- Gotenberg gets an `emptyDir` on `/tmp`.
- Get the digests with `crane digest <image:tag>` or the registry API.
- Paperless env: `PAPERLESS_TIKA_ENABLED=true`, `PAPERLESS_TIKA_ENDPOINT=http://127.0.0.1:9998`, `PAPERLESS_TIKA_GOTENBERG_ENDPOINT=http://127.0.0.1:3000`.

- [ ] **Step 5: Verify**

Run promtool as in Step 2. Expected: PASS.

Run `kustomize build --enable-alpha-plugins --enable-exec 2-k3s/33.paperless >/dev/null`, or the repo's ksops render command.
Expected: renders. If ksops cannot decrypt here, run `kubectl apply --dry-run=client -f 2-k3s/33.paperless/paperless.yaml`.

- [ ] **Step 6: Commit**

`git commit -m "feat(paperless): Tika and Gotenberg for email and Office files"`

---

### Task 3: Ingest scripts

**Files:**
- Create: `2-k3s/33.paperless/ingest/configure.py`
- Create: `2-k3s/33.paperless/ingest/drive_import.py`
- Create: `2-k3s/33.paperless/ingest/ingest.test.py`
- Modify: `.github/workflows/ci.yml` (stdlib test list near line 80): add `python3 2-k3s/33.paperless/ingest/ingest.test.py -v`

**Interfaces:**
- `configure.py` runs as `python3 manage.py shell -c "$(cat configure.py)"` in the `paperless` container. It reads one JSON object from stdin: `{"owner": str, "email": str, "password": str}`. It prints only counts and names it created or updated, never the input. Pure helper: `all_mail_folder(list_lines: list[bytes]) -> str`, which returns the mailbox flagged `\All`. Django imports live inside `main()`.
- `drive_import.py` CLI:
  - `--out DIR` (required): manifest `DIR/manifest.jsonl` and report `DIR/report.json`;
  - `--paperless-url` (default `http://127.0.0.1:18000`);
  - `--hub-url` (default `http://mcp-hub.mcp-hub-homelab.svc.cluster.local:8000/drive`);
  - `--plan-only`: list and write the report, with no download.

  It reads env `MCP_HUB_TOKEN` and `VAULT_PL_TOKEN`. Pure helpers:
  - `parse_page(text: str) -> tuple[list[DriveFile], str | None]`, where `DriveFile = NamedTuple(id, name, mime, size: int | None)`;
  - `parse_download_url(text: str) -> str`;
  - `export_format(mime: str) -> str | None`: `"pdf"` for Google Docs, Sheets and Slides;
  - `supported(mime: str) -> bool`;
  - `Budget(limit: int, window: float)` with `.wait_seconds(now: float, size: int) -> float` and `.record(now, size)`;
  - `done_ids(manifest_path) -> set[str]`.

- [ ] **Step 1: Write the failing tests in `ingest.test.py`** (stdlib `unittest`, loads both scripts with `importlib`)

- `test_parse_page`: two synthetic lines in the tool's format, `- Name: "a.pdf" (ID: id1, Type: application/pdf, Size: 10, Created: ..., Modified: ..., Last Edited By: X <x@example.test>) Link: https://example.test`, then `nextPageToken: tok`. Expect `[DriveFile("id1","a.pdf","application/pdf",10), ...]` and `"tok"`. With no token line, expect `None`.
- `test_parse_page_tricky_name`: name `He said "hi" (ID: fake).pdf` parses to ID `id2`.
- `test_parse_download_url`: text containing `📎 Download URL: http://h/drive/attachments/<uuid>` returns that URL. Text without one raises `ValueError`.
- `test_export_format_and_supported`: `application/vnd.google-apps.spreadsheet` gives `"pdf"`. `application/pdf` gives `None` and is supported. `application/vnd.android.package-archive` and `application/zip` are not supported.
- `test_budget_waits`: `Budget(100, 3600)`, record 80 at t=0. `wait_seconds(10, 30)` is `3590`, and `wait_seconds(3601, 30)` is `0`.
- `test_resume_skips_done`: a manifest with `{"id":"a","status":"uploaded"}` and `{"id":"b","status":"failed"}` gives `{"a"}`.
- `test_all_mail_folder`: `b'(\\HasNoChildren \\All) "/" "[Gmail]/Alle e-poster"'` among other lines gives `[Gmail]/Alle e-poster`. No `\All` raises `ValueError`.

Supported MIME set: PDF; JPEG, PNG, TIFF, GIF, WebP images; plain text and CSV; `message/rfc822`; Word, Excel and PowerPoint (old and OOXML); ODF text, spreadsheet and presentation; RTF; and the three Google native types. Check it against `src/documents/parsers.py` and `src/paperless_tika/` at tag v3.4.2 before you pin it.

- [ ] **Step 2: Run the tests and see them fail**

Run: `python3 2-k3s/33.paperless/ingest/ingest.test.py -v`
Expected: FAIL, modules missing.

- [ ] **Step 3: Implement `configure.py`**

Idempotent, using `update_or_create` by name:
- tags `source:gmail` and `source:drive`, with no owner, the same as the shared tags;
- `MailAccount` `gmail`: `imap.gmail.com`, port 993, SSL, IMAP type, owner = the `owner` user;
- `all_mail_folder` from an `imaplib` `LIST` with the same credentials;
- the two `MailRule`s from the spec table: `maximum_age=0`, action TAG with the label as parameter, `assign_title_from=FROM_SUBJECT`, `assign_correspondent_from=FROM_NOTHING`, `assign_owner_from_rule=True`, `attachment_type=ATTACHMENTS_ONLY`, and tags `source:gmail`, plus `paperless-gpt-auto` on rule 1. Rule 1 uses `consumption_scope=EML_ONLY` and folder `paperless-receipt`. Rule 2 uses `ATTACHMENTS_ONLY` and the all-mail folder;
- add both source tags to `filter_has_not_tags` on every trigger of workflow `Tag new receipts for AI`.

It fails loudly if the workflow or the owner user is missing.

- [ ] **Step 4: Implement `drive_import.py`**

- The MCP calls use the SDK's streamable HTTP client with header `Authorization: Bearer $MCP_HUB_TOKEN`. Imports stay inside `main()`, so tests run without the SDK.
- Listing: `search_drive_files` with query `'me' in owners and mimeType != 'application/vnd.google-apps.folder'`, `corpora="user"`, `page_size=1000`, following `nextPageToken`.
- Per file not in `done_ids`:
  - an unsupported type gets manifest status `skipped`;
  - for a supported file, stop the run if `shutil.disk_usage(out).free` is under twice the file size, then wait out `Budget(1 GiB, 3600)`, call `get_drive_file_download_url` with `export_format` when needed, and stream `GET` it with `httpx` and the hub bearer into `--out`;
  - `POST` it to `{paperless-url}/api/documents/post_document/` with `Authorization: Token $VAULT_PL_TOKEN`, fields `title=<name>` and `tags=<source:drive id>`;
  - write status `uploaded` with the task UUID, or `failed` with the HTTP status;
  - delete the local file in `finally`.
- Look up the tag ID once through `/api/tags/?name__iexact=source:drive`.
- The report holds counts by status and MIME type, total bytes, and a list of failed IDs.
- File content never goes to stdout.

- [ ] **Step 5: Run the tests and see them pass**

Run: `python3 2-k3s/33.paperless/ingest/ingest.test.py -v`
Expected: all OK.

- [ ] **Step 6: Commit**

`git commit -m "feat(paperless): Gmail rule setup and one-off Drive import scripts"`

---

### Task 4: Docs

**Files:**
- Modify: `2-k3s/33.paperless/README.md`, new section "Email and Drive import"

- [ ] **Step 1: Write the section**

Cover:
- the vault items;
- Tika and Gotenberg;
- the two rules and three Gmail labels;
- how the receipts workflow skips `source:*`;
- how to re-import one mail (delete it under Mail > Processed Mails);
- how to rotate the app password (rerun `configure.py`);
- how to rerun the Drive import (same `--out` resumes).

Show the exact commands from Task 5 with placeholders only.

- [ ] **Step 2: Commit**

`git commit -m "docs(paperless): email and Drive import"`

---

### Task 5: Rollout (live, each change confirmed with the owner)

- [ ] **Step 1:** Rebase onto `origin/main`, push, open the PR. Merge after the review gate passes and the owner approves.
- [ ] **Step 2:** Sync. Both hub Applications and `paperless` auto-sync. Check:
  - `kubectl -n paperless get pod -l app=paperless` shows 4/4 containers;
  - `kubectl -n mcp-hub-homelab rollout status deploy/workspace-mcp` succeeds;
  - one `get_drive_file_download_url` link now returns `200` from this pod through the hub with the hub bearer. Only the status code is printed.
- [ ] **Step 3: Gmail, after the owner confirms**
  - `manage_gmail_label` creates `paperless-receipt`;
  - `manage_gmail_filter` creates a filter with criteria `{"query": "{receipt invoice \"order confirmation\" kvittering faktura ordrebekreftelse Rechnung Quittung Bestellbestätigung απόδειξη τιμολόγιο}"}` and action `addLabelIds=[<label id>]`;
  - `apply` it with `dry_run=true` and tell the owner the count, then `apply` it for real.
- [ ] **Step 4: Paperless, after the owner confirms**

  Get the owner's username from `/api/profile/` with the owner token, written to a private file. Then run:

  1. `kubectl -n paperless cp 2-k3s/33.paperless/ingest/configure.py <pod>:/tmp/configure.py -c paperless`.
  2. Run `vault run --name <gmail-imap-item> --password-env VAULT_IMAP_PW -- sh -c '<python3 one-liner printing the stdin JSON from $OWNER, $EMAIL and $VAULT_IMAP_PW> | kubectl -n paperless exec -i <pod> -c paperless -- sh -c "cd /usr/src/paperless/src && python3 manage.py shell -c \"exec(open(\x27/tmp/configure.py\x27).read())\"" > "$D/configure.out" 2>&1'`.
  3. Read `$D/configure.out`.
  4. Run it a second time. The second run must report no creations.
  5. Delete `/tmp/configure.py` from the pod.
- [ ] **Step 5:** Wait for the first mail check. Check the Paperless log for mail errors in Loki, and check Mail > Processed Mails. Tell the owner the counts of documents tagged `source:gmail`, with and without `paperless-gpt-auto`.
- [ ] **Step 6:** `kubectl -n paperless port-forward svc/paperless 18000:8000 &`. Then run `vault run --name <paperless-token-item> --password-env VAULT_PL_TOKEN -- <venv>/bin/python drive_import.py --out "$D" --plan-only`. Show the owner the report.
- [ ] **Step 7:** After the owner confirms, run it without `--plan-only`. Rerun it until `report.json` shows no new uploads. Give the owner the failed list.
- [ ] **Step 8:** Report the live changes by resource name, and the final counts.
