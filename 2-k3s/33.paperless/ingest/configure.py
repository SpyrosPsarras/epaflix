import json
import sys


def main():
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from documents.models import CustomField, Tag, Workflow, WorkflowTrigger
    from paperless_mail.models import MailAccount

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
        CustomField.objects.get_or_create(name='Gmail thread', defaults={'data_type': CustomField.FieldDataType.STRING})
        names.append('Gmail thread')
        for trigger in triggers:
            trigger.filter_has_not_tags.add(tags['source:gmail'], tags['source:drive'])
        names.append('Tag new receipts for AI')
    print(f'Created or updated {len(names)} objects: {", ".join(names)}')


if __name__ in ('__main__', 'django.core.management.commands.shell'):
    try:
        main()
    except Exception:
        raise SystemExit('Paperless mail setup failed; check prerequisites and credentials') from None
