import email
import email.policy
import os
import sys
import time
from collections import Counter
from typing import NamedTuple

sys.path[:0] = ['/ingest']
from thread_sync import LEGACY_RULES, DjangoStore, GmailBox, body_text, checksum, eligible_parts, eml_bytes, is_reply, note_text  # noqa: E402

QUERY = 'label:paperless-done-att OR label:paperless-done-eml'


class Msg(NamedTuple):
    uid: str
    thrid: str
    date: object
    reply: bool
    kind: str
    doc_ids: list[int]
    note: str | None


def plan_threads(messages: list[Msg]) -> list[tuple]:
    threads = {}
    for msg in sorted(messages, key=lambda m: (m.date, int(m.uid))):
        threads.setdefault(msg.thrid, []).append(msg)
    actions = []
    for thrid, msgs in threads.items():
        root = None
        for msg in msgs:
            if not msg.doc_ids:
                continue
            if root is not None and msg.reply:
                if msg.kind == 'eml':
                    if msg.note:
                        actions.append(('note', root, msg.note))
                    if msg.doc_ids[0] != root:
                        actions.append(('delete', root, msg.doc_ids[0]))
                    continue
                actions += [('version', root, doc_id) for doc_id in msg.doc_ids if doc_id != root]
            else:
                root = msg.doc_ids[0]
                actions.append(('field', root, thrid))
                actions += [('version', root, doc_id) for doc_id in msg.doc_ids[1:] if doc_id != root]
            if msg.note and msg.kind == 'att':
                actions.append(('note', root, msg.note))
    return actions


def scan(box, store, query=QUERY) -> list[Msg]:
    messages = []
    meta = box.metadata(box.search([query]))
    for uid in sorted(meta, key=int):
        _, thrid, labels = meta[uid]
        raw = box.message(uid)
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        kind = 'eml' if 'paperless-done-eml' in labels else 'att'
        files = [eml_bytes(raw)] if kind == 'eml' else [p.data for p in eligible_parts(msg, 'x')]
        doc_ids = []
        for data in files:
            doc = store.Document.objects.filter(checksum=checksum(data)).first()
            if doc and doc.root_document_id and kind == 'eml':
                continue
            doc_id = doc and (doc.root_document_id or doc.id)
            if doc_id and doc_id not in doc_ids:
                doc_ids.append(doc_id)
        date = msg['Date'].datetime if msg['Date'] and msg['Date'].datetime else None
        note = note_text(str(msg['From'] or ''), store.local(date), body_text(msg)) if date else None
        messages.append(Msg(uid, thrid, date.timestamp() if date else 0, is_reply(msg), kind, doc_ids, note))
    return messages


def apply(actions, store):
    from documents.bulk_edit import merge_as_versions
    from documents.models import CustomFieldInstance
    for action, root, value in actions:
        if action == 'field':
            CustomFieldInstance.objects.get_or_create(document_id=root, field=store.field, defaults={'value_text': value})
        elif action == 'version':
            doc = store.Document.objects.get(pk=value)
            doc.version_label = (doc.original_filename or doc.title or '')[:64] or None
            doc.save(update_fields=['version_label'])
            merge_as_versions([root, value], root_document_id=root)
        elif action == 'note':
            store.add_note(root, value)
        elif action == 'delete':
            store.Document.objects.get(pk=value).delete()


def fingerprint(actions) -> str:
    return checksum(repr([a for a in actions if a[0] in ('version', 'delete')]).encode())[:12]


def report(messages, actions):
    print(f'messages={len(messages)} unmatched={sum(not m.doc_ids for m in messages)}', dict(Counter(a[0] for a in actions)), flush=True)
    for action, root, value in actions:
        if action in ('version', 'delete'):
            print(action, root, value)
    print(f'fingerprint={fingerprint(actions)}', flush=True)


def main():
    from django.utils import timezone
    store = DjangoStore().load()
    approved = os.environ.get('THREAD_MIGRATE_APPLY')
    rules = store.account.rules.filter(name__in=LEGACY_RULES)
    if approved:
        rules.update(enabled=False)
        print('old mail rules off, waiting for their queued imports', flush=True)
        while store.legacy_pending(timezone.now()):
            time.sleep(30)
    with GmailBox(store.account) as box:
        messages = scan(box, store)
    actions = plan_threads(messages)
    report(messages, actions)
    if not approved:
        return
    if fingerprint(actions) != approved:
        print('plan changed since the approved dry run: review the plan above, then run again with its fingerprint', flush=True)
        return
    apply(actions, store)
    for rule in rules:
        rule.name = f'{rule.name} (retired)'
        rule.save(update_fields=['name'])
    print('done; thread-sync starts on its next run', flush=True)


if __name__ in ('__main__', 'django.core.management.commands.shell'):
    main()
