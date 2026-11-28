import email
import email.policy
import hashlib
from html.parser import HTMLParser
from pathlib import Path
import re
import unicodedata
from typing import NamedTuple

RECEIPT = 'paperless-receipt'
BODY = 'paperless-receipt-body'
DONE = 'paperless-done'
NOT_DONE = f'-label:{DONE} -label:paperless-done-att -label:paperless-done-eml -in:drafts'
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


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
    if RECEIPT in labels:
        return 'attachments'
    if BODY in labels:
        return 'body'
    return None


def plan_message(kind, parts, eml, note, present, queued, failed, has_root) -> Plan:
    if kind == 'body' or (kind == 'attachments' and not parts):
        files, note = [eml], None
    else:
        files = parts
    pending = [f for f in files if checksum(f.data) not in present]
    ready = [f for f in pending if f.name not in queued and f.name not in failed]
    if has_root:
        return Plan(None, ready, note, not pending)
    if not pending:
        return Plan(None, [], None, True)
    waiting = any(f.name in queued for f in pending)
    return Plan(None if waiting or not ready else ready[0], [], None, False)


def joined_label(ids: list[int]) -> str:
    return JOINED + ', '.join(map(str, ids))


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
