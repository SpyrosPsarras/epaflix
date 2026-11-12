import base64
import imaplib
import json
import re
import sys


def all_mail_folder(list_lines: list[bytes]) -> str:
    for line in list_lines:
        if not isinstance(line, bytes):
            continue
        match = re.fullmatch(rb'\(([^)]*)\)\s+(?:"(?:\\.|[^"\\])*"|NIL)\s+(.+)', line)
        if match and b'\\all' in match[1].lower().split():
            mailbox = match[2]
            if mailbox.startswith(b'"') and mailbox.endswith(b'"'):
                mailbox = re.sub(rb'\\(["\\])', rb'\1', mailbox[1:-1])
            return re.sub(
                r'&([^-]*)-',
                lambda part: base64.b64decode(
                    part[1] + '=' * (-len(part[1]) % 4), altchars=b'+,', validate=True,
                ).decode('utf-16-be') if part[1] else '&',
                mailbox.decode('utf-8'),
            )
    raise ValueError('No IMAP mailbox has the All flag')


def main():
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from documents.models import Tag, Workflow, WorkflowTrigger
    from paperless_mail.models import MailAccount, MailRule

    config = json.load(sys.stdin)
    if not isinstance(config, dict) or any(
        not isinstance(config.get(key), str) or not config[key].strip()
        for key in ['owner', 'email', 'password']
    ):
        raise ValueError('Expected owner, email and password strings on stdin')
    owner = get_user_model().objects.get(username=config['owner'])
    workflow = Workflow.objects.get(name='Tag new receipts for AI')
    triggers = list(workflow.triggers.all())
    if not triggers or any(
        trigger.type != WorkflowTrigger.WorkflowTriggerType.DOCUMENT_ADDED
        for trigger in triggers
    ):
        raise ValueError('The receipts workflow needs document-added triggers')
    ai_tag = Tag.objects.get(name='paperless-gpt-auto')

    with imaplib.IMAP4_SSL('imap.gmail.com', 993) as mailbox:
        mailbox.login(config['email'], config['password'])
        status, lines = mailbox.list()
        if status != 'OK':
            raise ValueError('IMAP folder discovery failed')
        folder = all_mail_folder(lines)

    names = []
    with transaction.atomic():
        tags = {}
        for name in ['source:gmail', 'source:drive']:
            tags[name], _ = Tag.objects.update_or_create(
                name=name,
                defaults={'owner': None, 'matching_algorithm': Tag.MATCH_NONE},
            )
            names.append(name)
        account, _ = MailAccount.objects.update_or_create(
            name='gmail',
            defaults={
                'owner': owner,
                'imap_server': 'imap.gmail.com',
                'imap_port': 993,
                'imap_security': MailAccount.ImapSecurity.SSL,
                'account_type': MailAccount.MailAccountType.IMAP,
                'username': config['email'],
                'password': config['password'],
                'is_token': False,
            },
        )
        names.append('gmail')
        rules = [
            ('gmail-receipt-bodies', 'paperless-receipt', MailRule.ConsumptionScope.EML_ONLY, 'paperless-done-eml'),
            ('gmail-attachments', folder, MailRule.ConsumptionScope.ATTACHMENTS_ONLY, 'paperless-done-att'),
        ]
        for order, (name, mail_folder, scope, label) in enumerate(rules, 1):
            rule, _ = MailRule.objects.update_or_create(
                name=name,
                defaults={
                    'owner': owner,
                    'account': account,
                    'order': order,
                    'enabled': True,
                    'folder': mail_folder,
                    'maximum_age': 0,
                    'action': MailRule.MailAction.TAG,
                    'action_parameter': label,
                    'assign_title_from': MailRule.TitleSource.FROM_SUBJECT,
                    'assign_correspondent_from': MailRule.CorrespondentSource.FROM_NOTHING,
                    'assign_correspondent': None,
                    'assign_owner_from_rule': True,
                    'attachment_type': MailRule.AttachmentProcessing.ATTACHMENTS_ONLY,
                    'consumption_scope': scope,
                    'stop_processing': False,
                },
            )
            rule.assign_tags.set(
                [tags['source:gmail'], ai_tag] if order == 1 else [tags['source:gmail']],
            )
            names.append(name)
        for trigger in triggers:
            trigger.filter_has_not_tags.add(tags['source:gmail'], tags['source:drive'])
        names.append('Tag new receipts for AI')
    print(f'Created or updated {len(names)} objects: {", ".join(names)}')


if __name__ in ('__main__', 'django.core.management.commands.shell'):
    try:
        main()
    except Exception:
        raise SystemExit('Paperless mail setup failed; check prerequisites and credentials') from None
