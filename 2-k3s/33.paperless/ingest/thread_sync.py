from collections import Counter
from datetime import timedelta
import email
import email.policy
import hashlib
from html.parser import HTMLParser
import io
from pathlib import Path
import re
import unicodedata
from typing import NamedTuple

RECEIPT = 'paperless-receipt'
BODY = 'paperless-receipt-body'
DONE = 'paperless-done'
FAILED = 'paperless-failed'
NOT_DONE = f'-label:{DONE} -label:{FAILED} -label:paperless-done-att -label:paperless-done-eml -in:drafts'
QUERIES = [f'(label:{RECEIPT} OR label:{BODY}) {NOT_DONE}', f'newer_than:7d {NOT_DONE}']
THREAD_FIELD = 'Gmail thread'
JOINED = 'All files: '
EXTENSIONS = {
    'application/pdf': 'pdf',
    'application/msword': 'doc',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
    'application/vnd.ms-excel': 'xls',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
    'application/vnd.oasis.opendocument.text': 'odt',
    'application/vnd.oasis.opendocument.spreadsheet': 'ods',
}
QUOTE = re.compile(r'(>|On\s.+\swrote:$|Den\s.+\sskrev|-+\s*Original Message\s*-+|(From|Fra|Von):\s)', re.IGNORECASE)


class Part(NamedTuple):
    name: str
    data: bytes


class Plan(NamedTuple):
    new: Part | None
    versions: list[Part]
    note: str | None
    done: bool
    failed: bool = False


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def key(part: Part) -> str:
    return checksum(part.data)[:12]


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', unicodedata.normalize('NFC', name)).strip(' .')
    return name[-120:] or 'attachment'


def eligible_parts(msg, subject: str) -> list[Part]:
    parts, names = [], set()
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename() or ''
        ext = EXTENSIONS.get(part.get_content_type())
        if ext is None and part.get_content_type() == 'application/octet-stream':
            suffix = Path(filename).suffix.lower().lstrip('.')
            ext = suffix if suffix in EXTENSIONS.values() else None
        data = part.get_payload(decode=True)
        if ext is None or not data:
            continue
        stem, number = safe_name(Path(filename).stem if filename else subject), 1
        name = safe_name(filename) if filename else f'{stem}.{ext}'
        while name in names:
            number += 1
            name = f'{stem} {number}.{ext}'
        names.add(name)
        parts.append(Part(name, data))
    return parts


class _Text(HTMLParser):
    SKIP = {'script', 'style', 'head', 'blockquote'}
    BREAK = {'br', 'p', 'div', 'tr', 'li', 'h1', 'h2', 'h3', 'table'}
    VOID = {'area', 'base', 'br', 'col', 'hr', 'img', 'input', 'link', 'meta', 'source', 'wbr'}

    def __init__(self):
        super().__init__()
        self.out, self.skipping = [], 0

    def handle_starttag(self, tag, attrs):
        if self.skipping:
            self.skipping += tag not in self.VOID
        elif tag in self.SKIP or 'gmail_quote' in (dict(attrs).get('class') or ''):
            self.skipping = 1
        elif tag in self.BREAK:
            self.out.append('\n')

    def handle_endtag(self, tag):
        if self.skipping:
            self.skipping -= 1
        elif tag in self.BREAK:
            self.out.append('\n')

    def handle_data(self, data):
        if not self.skipping:
            self.out.append(data)


def html_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    return '\n'.join(line.strip() for line in ''.join(parser.out).splitlines())


def cut_quote(text: str) -> str:
    lines = text.splitlines()
    kept = []
    for i, line in enumerate(lines):
        joined = f'{line.strip()} {lines[i + 1].strip()}' if i + 1 < len(lines) else line.strip()
        if QUOTE.match(line.strip()) or (line.startswith('On ') and QUOTE.match(joined)):
            break
        kept.append(line.rstrip())
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(kept)).strip()


def body_text(msg) -> str:
    part = msg.get_body(preferencelist=('plain', 'html'))
    if part is None:
        return ''
    content = part.get_content()
    return cut_quote(html_text(content) if part.get_content_subtype() == 'html' else content)


def note_text(sender: str, date, body: str) -> str | None:
    return f'From {sender}, {date:%Y-%m-%d %H:%M}\n\n{body}' if body else None


def eml_bytes(raw: bytes) -> bytes:
    msg = email.message_from_bytes(raw)
    # Paperless picks the mail parser from libmagic, which needs From first.
    msg._headers.sort(key=lambda header: header[0].lower() != 'from')
    return msg.as_bytes()


def is_reply(msg) -> bool:
    return bool(msg['In-Reply-To'] or msg['References'])


def classify(labels: set[str], thread_has_document: bool, reply: bool) -> str | None:
    if thread_has_document and reply:
        return 'followup'
    return 'new' if labels & {RECEIPT, BODY} else None


def plan_message(kind, parts, eml, note, present, queued, failed, has_root) -> Plan:
    if kind == 'new' and not parts:
        files, note = [eml], None
    else:
        files = parts
    pending = [f for f in files if checksum(f.data) not in present]
    if any(key(f) in failed for f in pending):
        return Plan(None, [], None, False, True)
    ready = [f for f in pending if key(f) not in queued]
    if has_root:
        return Plan(None, ready, note, not pending)
    if not pending:
        return Plan(None, [], None, True)
    return Plan(None if len(ready) < len(pending) else ready[0], [], None, False)


def joined_label(ids: list[int]) -> str:
    label = JOINED + ', '.join(map(str, ids))
    return label if len(label) <= 64 else f'{JOINED}{len(ids)} files #{checksum(label.encode())[:8]}'


def needs_join(originals: list[tuple[int, bool]], newest_label: str | None) -> list[int] | None:
    ids = [doc_id for doc_id, _ in originals]
    if len(ids) < 2 or not all(ready for _, ready in originals) or newest_label == joined_label(ids):
        return None
    return ids


def _labels(text: str) -> set[str]:
    return {quoted.replace('\\\\', '\\').replace('\\"', '"') if quoted else bare
            for quoted, bare in re.findall(r'"((?:[^"\\]|\\.)*)"|([^\s"]+)', text)}


def parse_gm_fetch(lines) -> dict[str, tuple[str, str, set[str]]]:
    result = {}
    for line in lines:
        head = (line[0] if isinstance(line, tuple) else line).decode('utf-8', 'replace')
        uid = re.search(r'\bUID (\d+)', head)
        if not uid:
            continue
        labels = re.search(r'X-GM-LABELS \(((?:[^()"]|"(?:[^"\\]|\\.)*")*)\)', head)
        result[uid.group(1)] = (
            re.search(r'X-GM-MSGID (\d+)', head).group(1),
            re.search(r'X-GM-THRID (\d+)', head).group(1),
            _labels(labels.group(1)) if labels else set(),
        )
    return result


def handle_message(mailbox, store, uid, thrid, labels, thread_doc, state, counts):
    queued, queued_threads, failed = state
    raw = mailbox.message(uid)
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    kind = classify(labels, thread_doc is not None, is_reply(msg))
    if kind is None:
        return
    counts['read'] += 1
    subject = str(msg['Subject'] or '').strip() or 'Gmail'
    parts = eligible_parts(msg, subject)
    eml = Part(f'{safe_name(subject)}.eml', eml_bytes(raw))
    present = store.present({checksum(f.data) for f in [*parts, eml]})
    root = thread_doc if kind == 'followup' else store.root_with_thread(present, thrid)
    date = msg['Date'].datetime if msg['Date'] and msg['Date'].datetime else None
    note = note_text(str(msg['From'] or ''), store.local(date), body_text(msg)) if date else None
    plan = plan_message(kind, parts, eml, note, present, queued, failed, root is not None)
    if plan.new:
        store.queue(plan.new, root_id=None, thrid=thrid, title=subject)
        queued.add(key(plan.new))
        queued_threads.add(thrid)
        counts['created'] += 1
    for part in plan.versions:
        store.queue(part, root_id=root, thrid=thrid, title=subject)
        queued.add(key(part))
        counts['versions'] += 1
    if plan.note and store.add_note(root, plan.note):
        counts['notes'] += 1
    if plan.done:
        mailbox.mark(uid, DONE)
        counts['done'] += 1
    elif plan.failed:
        mailbox.mark(uid, FAILED)
        counts['import_failed'] += 1
    else:
        counts['waiting'] += 1


def sync(mailbox, store, now) -> Counter:
    counts = Counter()
    state = store.tasks(now)
    meta = mailbox.metadata(mailbox.search(QUERIES))
    for uid in sorted(meta, key=int):
        msgid, thrid, labels = meta[uid]
        thread_doc = store.document_for_thread(thrid)
        if thread_doc is None and not labels & {RECEIPT, BODY}:
            continue
        if thrid in state[1]:
            counts['waiting'] += 1
            continue
        try:
            handle_message(mailbox, store, uid, thrid, labels, thread_doc, state, counts)
        except Exception as error:
            counts['failed'] += 1
            print(f'thread-sync: message {msgid} failed: {type(error).__name__}', flush=True)
    queued, _, failed = state
    for root in store.join_candidates(now):
        ids = needs_join(store.originals(root), store.newest_label(root))
        if ids and f'join{root}' not in queued | failed:
            store.queue_join(root, ids)
            counts['joined'] += 1
    return counts


class DjangoStore:
    def __init__(self):
        from django.conf import settings
        from documents.models import CustomField, Document, Note, PaperlessTask, Tag
        from paperless_mail.models import MailAccount
        self.settings, self.Document, self.Note, self.Task = settings, Document, Note, PaperlessTask
        self.account = MailAccount.objects.get(name='gmail')
        self.owner = self.account.owner
        self.field = CustomField.objects.get(name=THREAD_FIELD)
        self.tag_ids = [Tag.objects.get(name=name).id for name in ['source:gmail', 'paperless-gpt-auto']]

    def old_rules_active(self, now):
        if self.account.rules.filter(enabled=True).exists():
            return True
        pending = self.Task.objects.filter(
            task_type=self.Task.TaskType.CONSUME_FILE, status__in=[self.Task.Status.PENDING, self.Task.Status.STARTED],
            date_created__gte=now - timedelta(hours=6),
        )
        return any((task.input_data or {}).get('mailrule_id') for task in pending)

    def tasks(self, now):
        queued, threads, failed = set(), set(), set()
        Status = self.Task.Status
        for task in self.Task.objects.filter(task_type=self.Task.TaskType.CONSUME_FILE, date_created__gte=now - timedelta(hours=24)):
            data = task.input_data or {}
            name = str(data.get('filename') or '').split('-', 1)[0]
            if task.status in (Status.PENDING, Status.STARTED) and task.date_created >= now - timedelta(hours=6):
                queued.add(name)
                threads.update(str(value) for value in ((data.get('overrides') or {}).get('custom_fields') or {}).values())
            elif task.status == Status.FAILURE:
                failed.add(name)
        return queued, threads, failed

    def _thread_roots(self, thrid):
        return self.field.fields.filter(value_text=thrid, document__deleted_at__isnull=True, document__root_document__isnull=True)

    def document_for_thread(self, thrid):
        return self._thread_roots(thrid).order_by('-document_id').values_list('document_id', flat=True).first()

    def present(self, sums):
        return set(self.Document.global_objects.filter(checksum__in=sums).values_list('checksum', flat=True))

    def root_with_thread(self, present, thrid):
        own = self.Document.objects.filter(checksum__in=present, root_document__isnull=True).values_list('id', flat=True)
        return self._thread_roots(thrid).filter(document_id__in=list(own)).values_list('document_id', flat=True).first()

    def queue(self, part, root_id, thrid, title, label=None, file_key=None):
        from documents.data_models import ConsumableDocument, DocumentMetadataOverrides, DocumentSource
        from documents.tasks import consume_file
        self.settings.SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        path = self.settings.SCRATCH_DIR / f'{file_key or key(part)}-{part.name}'
        path.write_bytes(part.data)
        if root_id is None:
            overrides = DocumentMetadataOverrides(title=title, filename=part.name, tag_ids=self.tag_ids, owner_id=self.owner.id, custom_fields={self.field.id: thrid})
        else:
            overrides = DocumentMetadataOverrides(filename=part.name, version_label=label or part.name[:64], owner_id=self.owner.id)
        consume_file.apply_async(
            kwargs={'input_doc': ConsumableDocument(source=DocumentSource.MailFetch, original_file=path, root_document_id=root_id), 'overrides': overrides},
            headers={'trigger_source': self.Task.TriggerSource.EMAIL_CONSUME},
        )

    def add_note(self, doc_id, text):
        if self.Note.objects.filter(document_id=doc_id, note=text).exists():
            return False
        self.Note.objects.create(document_id=doc_id, note=text, user=self.owner)
        return True

    def local(self, date):
        from django.utils import timezone
        return timezone.localtime(date) if timezone.is_aware(date) else date

    def join_candidates(self, now):
        return list(self.Document.objects.filter(
            root_document__isnull=True, modified__gte=now - timedelta(days=2), versions__isnull=False,
            custom_fields__field=self.field,
        ).values_list('id', flat=True).distinct())

    def _versions(self, root):
        return list(self.Document.objects.filter(root_document_id=root).order_by('version_index', 'id'))

    def _pdf(self, doc):
        path = doc.source_path if doc.mime_type == 'application/pdf' else doc.archive_path
        return path if path and Path(path).is_file() else None

    def originals(self, root):
        docs = [self.Document.objects.get(pk=root)] + [v for v in self._versions(root) if not (v.version_label or '').startswith(JOINED)]
        return [(doc.id, self._pdf(doc) is not None) for doc in docs]

    def newest_label(self, root):
        versions = self._versions(root)
        return versions[-1].version_label if versions else None

    def queue_join(self, root, ids):
        import pikepdf
        joined = pikepdf.new()
        sources = [pikepdf.open(self._pdf(self.Document.global_objects.get(pk=doc_id))) for doc_id in ids]
        try:
            for source in sources:
                joined.pages.extend(source.pages)
            buffer = io.BytesIO()
            joined.save(buffer, static_id=True)
        finally:
            for source in sources:
                source.close()
        self.queue(Part(f'All files {root}.pdf', buffer.getvalue()), root_id=root, thrid=None, title=None, label=joined_label(ids), file_key=f'join{root}')


class GmailBox:
    def __init__(self, account):
        self.account = account

    def __enter__(self):
        from imap_tools import MailBox
        self.box = MailBox(self.account.imap_server, self.account.imap_port).login(self.account.username, self.account.password, '[Gmail]/All Mail')
        return self

    def __exit__(self, *exc):
        self.box.logout()

    def _ok(self, response):
        status, data = response
        if status != 'OK':
            raise OSError(f'IMAP {status}')
        return data

    def search(self, queries):
        uids = set()
        for query in queries:
            uids.update(self._ok(self.box.client.uid('search', None, 'X-GM-RAW', f'"{query}"'))[0].split())
        return sorted((uid.decode() for uid in uids), key=int)

    def metadata(self, uids):
        result = {}
        for start in range(0, len(uids), 500):
            chunk = ','.join(uids[start:start + 500])
            result.update(parse_gm_fetch(self._ok(self.box.client.uid('fetch', chunk, '(UID X-GM-MSGID X-GM-THRID X-GM-LABELS)'))))
        return result

    def message(self, uid):
        data = self._ok(self.box.client.uid('fetch', uid, '(BODY.PEEK[])'))
        return next(item[1] for item in data if isinstance(item, tuple))

    def mark(self, uid, label):
        self._ok(self.box.client.uid('store', uid, '+X-GM-LABELS', f'({label})'))


def main():
    from django.utils import timezone
    store = DjangoStore()
    if store.old_rules_active(timezone.now()):
        print('thread-sync: waiting until the old Gmail mail rules are disabled and their imports finish', flush=True)
        return
    with GmailBox(store.account) as mailbox:
        counts = sync(mailbox, store, timezone.now())
    print('thread-sync: ' + ' '.join(f'{key}={counts[key]}' for key in ['read', 'created', 'versions', 'joined', 'notes', 'done', 'waiting', 'import_failed', 'failed']), flush=True)


if __name__ in ('__main__', 'django.core.management.commands.shell'):
    try:
        main()
    except Exception as error:
        raise SystemExit(f'thread-sync failed: {type(error).__name__}') from None
