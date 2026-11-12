import contextlib
import importlib.util
import io
import json
import runpy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class IngestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.drive = load("drive_import")
        cls.configure = load("configure")

    def test_parse_page(self):
        text = '\n'.join([
            '- Name: "a.pdf" (ID: id1, Type: application/pdf, Size: 10, Created: ..., Modified: ..., Last Edited By: X <x@example.test>) Link: https://example.test',
            '- Name: "b.png" (ID: id2, Type: image/png, Created: ..., Modified: ..., Last Edited By: X <x@example.test>) Link: https://example.test',
            'nextPageToken: tok',
        ])
        files, token = self.drive.parse_page(text)
        self.assertEqual(files, [self.drive.DriveFile("id1", "a.pdf", "application/pdf", 10), self.drive.DriveFile("id2", "b.png", "image/png", None)])
        self.assertEqual(token, "tok")
        self.assertIsNone(self.drive.parse_page(text.split('nextPageToken:')[0])[1])

    def test_parse_page_shortcut_without_size(self):
        files, _ = self.drive.parse_page('- Name: "shortcut" (ID: shortcut-id, Type: application/vnd.google-apps.shortcut, Created: ..., Modified: ..., Last Edited By: X <x@example.test>) Link: https://example.test')
        self.assertEqual(files, [self.drive.DriveFile('shortcut-id', 'shortcut', 'application/vnd.google-apps.shortcut', None)])
        self.assertFalse(self.drive.supported(files[0].mime))
        self.assertTrue(self.drive.supported('image/png'))

    def test_parse_page_tricky_name(self):
        files, _ = self.drive.parse_page('- Name: "He said "hi" (ID: fake).pdf" (ID: id2, Type: application/pdf, Size: 10, Created: ..., Modified: ..., Last Edited By: X <x@example.test>) Link: https://example.test')
        self.assertEqual(files[0].id, "id2")
        self.assertEqual(files[0].name, 'He said "hi" (ID: fake).pdf')

    def test_parse_page_rejects_unparsed_file(self):
        with self.assertRaises(ValueError):
            self.drive.parse_page('- Name: "a.pdf" (ID: id1, Type: application/pdf)')

    def test_parse_download_url(self):
        url = 'http://h/drive/attachments/12345678-1234-4234-8234-123456789abc'
        self.assertEqual(self.drive.parse_download_url(f'File ready\n📎 Download URL: {url}\n'), url)
        with self.assertRaises(ValueError):
            self.drive.parse_download_url('Inline content is not a download link')

    def test_export_format_and_supported(self):
        for mime in ['document', 'spreadsheet', 'presentation']:
            self.assertEqual(self.drive.export_format('application/vnd.google-apps.' + mime), 'pdf')
            self.assertTrue(self.drive.supported('application/vnd.google-apps.' + mime))
        self.assertIsNone(self.drive.export_format('application/pdf'))
        for mime in ['application/pdf', 'image/jpeg', 'image/png', 'image/tiff', 'image/gif', 'image/webp', 'text/plain', 'text/csv', 'application/csv', 'message/rfc822', 'application/msword', 'application/vnd.ms-excel', 'application/vnd.ms-powerpoint', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'application/vnd.openxmlformats-officedocument.presentationml.presentation', 'application/vnd.oasis.opendocument.text', 'application/vnd.oasis.opendocument.spreadsheet', 'application/vnd.oasis.opendocument.presentation', 'text/rtf']:
            with self.subTest(mime=mime):
                self.assertTrue(self.drive.supported(mime))
        for mime in ['application/vnd.android.package-archive', 'application/zip', 'application/vnd.google-apps.folder', 'application/rtf']:
            self.assertFalse(self.drive.supported(mime))

    def test_budget_waits(self):
        budget = self.drive.Budget(100, 3600)
        budget.record(0, 80)
        self.assertEqual(budget.wait_seconds(10, 30), 3590)
        self.assertEqual(budget.wait_seconds(3601, 30), 0)

    def test_budget_multiple_reservations(self):
        budget = self.drive.Budget(100, 100)
        budget.record(0, 40)
        budget.record(10, 40)
        self.assertEqual(budget.wait_seconds(20, 70), 90)
        with self.assertRaises(ValueError):
            budget.wait_seconds(20, 101)

    def test_resume_skips_done(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.jsonl'
            self.assertEqual(self.drive.done_ids(path), set())
            path.write_text('{"id":"a","status":"uploaded"}\n{"id":"b","status":"failed"}\n')
            self.assertEqual(self.drive.done_ids(path), {'a'})

    def test_complete_download(self):
        self.assertTrue(self.drive.download_complete(10, 10))
        self.assertTrue(self.drive.download_complete(10, None))
        self.assertFalse(self.drive.download_complete(9, 10))
        self.assertFalse(self.drive.download_complete(11, 10))

    def test_all_mail_folder(self):
        lines = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\All) "/" "[Gmail]/Alle e-poster"']
        self.assertEqual(self.configure.all_mail_folder(lines), '[Gmail]/Alle e-poster')
        self.assertEqual(self.configure.all_mail_folder([b'(\\All) "/" "a\\"b\\\\c"']), 'a"b\\c')
        self.assertEqual(self.configure.all_mail_folder([b'(\\All) "/" AllMail']), 'AllMail')
        with self.assertRaises(ValueError):
            self.configure.all_mail_folder([lines[0]])

    def test_all_mail_folder_modified_utf7(self):
        fixtures = [
            (b'[Gmail]/&AMk-l&AOk-ments', '[Gmail]/Éléments'),
            ('[Gmail]/Éléments'.encode('utf-8'), '[Gmail]/Éléments'),
            (b'&-', '&'),
            (b'[Gmail]/&A4wDuwOx- &A8QDsQ- &A7wDtwO9A80DvAOxA8QDsQ-', '[Gmail]/Όλα τα μηνύματα'),
            (b'&,,8-', '\uffff'),
        ]
        for encoded, expected in fixtures:
            with self.subTest(encoded=encoded):
                self.assertEqual(self.configure.all_mail_folder([b'(\\All) "/" "' + encoded + b'"']), expected)


class ConfigureRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.configure = load('configure')

    def setUp(self):
        class Relation:
            def __init__(self):
                self.items = []

            def add(self, *items):
                for item in items:
                    if item not in self.items:
                        self.items.append(item)

            def set(self, items):
                self.items = list(items)

            def all(self):
                return self.items

        class Manager:
            def __init__(self, rows=None):
                self.rows = rows or {}

            def get(self, **kwargs):
                key = kwargs.get('name', kwargs.get('username'))
                if key not in self.rows:
                    raise ValueError('missing prerequisite')
                return self.rows[key]

            def update_or_create(self, name, defaults):
                created = name not in self.rows
                row = self.rows.setdefault(name, SimpleNamespace(name=name, assign_tags=Relation()))
                for key, value in defaults.items():
                    setattr(row, key, value)
                return row, created

        self.owner = SimpleNamespace(username='owner@example.test')
        self.trigger = SimpleNamespace(type=2, filter_has_not_tags=Relation())
        triggers = Relation()
        triggers.add(self.trigger)
        self.workflow = SimpleNamespace(triggers=triggers)
        self.users = Manager({'owner@example.test': self.owner})
        self.tags = Manager({'paperless-gpt-auto': SimpleNamespace(name='paperless-gpt-auto')})
        self.accounts = Manager()
        self.rules = Manager()
        self.workflows = Manager({'Tag new receipts for AI': self.workflow})
        self.modules = {
            'django': SimpleNamespace(),
            'django.contrib': SimpleNamespace(),
            'django.contrib.auth': SimpleNamespace(get_user_model=lambda: SimpleNamespace(objects=self.users)),
            'django.db': SimpleNamespace(transaction=SimpleNamespace(atomic=contextlib.nullcontext)),
            'documents': SimpleNamespace(),
            'documents.models': SimpleNamespace(Tag=SimpleNamespace(objects=self.tags, MATCH_NONE=0), Workflow=SimpleNamespace(objects=self.workflows), WorkflowTrigger=SimpleNamespace(WorkflowTriggerType=SimpleNamespace(DOCUMENT_ADDED=2))),
            'paperless_mail': SimpleNamespace(),
            'paperless_mail.models': SimpleNamespace(
                MailAccount=SimpleNamespace(objects=self.accounts, ImapSecurity=SimpleNamespace(SSL=2), MailAccountType=SimpleNamespace(IMAP=1)),
                MailRule=SimpleNamespace(objects=self.rules, ConsumptionScope=SimpleNamespace(ATTACHMENTS_ONLY=1, EML_ONLY=2), AttachmentProcessing=SimpleNamespace(ATTACHMENTS_ONLY=1), MailAction=SimpleNamespace(TAG=5), TitleSource=SimpleNamespace(FROM_SUBJECT=1), CorrespondentSource=SimpleNamespace(FROM_NOTHING=1)),
            ),
        }
        class Mailbox:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def login(self, username, password):
                if username != 'mail@example.test' or password != 'synthetic-password':
                    raise ValueError('wrong credentials')

            def list(self):
                return 'OK', [b'(\\All) "/" "[Gmail]/Alle e-poster"']

        self.mailbox = Mailbox

    def run_configure(self, shell=False):
        data = json.dumps({'owner': 'owner@example.test', 'email': 'mail@example.test', 'password': 'synthetic-password'})
        with patch.dict(sys.modules, self.modules), patch.object(self.configure.imaplib, 'IMAP4_SSL', self.mailbox), patch.object(sys, 'stdin', io.StringIO(data)), contextlib.redirect_stdout(io.StringIO()) as output:
            if shell:
                runpy.run_path(str(HERE / 'configure.py'), run_name='django.core.management.commands.shell')
            else:
                self.configure.main()
        self.assertNotIn('example.test', output.getvalue())
        self.assertNotIn('synthetic-password', output.getvalue())

    def test_configuration_is_private_and_idempotent(self):
        self.run_configure()
        self.run_configure(shell=True)
        self.assertEqual(len(self.accounts.rows), 1)
        self.assertEqual(len(self.rules.rows), 2)
        self.assertEqual(len(self.tags.rows), 3)
        account = self.accounts.rows['gmail']
        self.assertIs(account.owner, self.owner)
        self.assertEqual((account.imap_server, account.imap_port, account.imap_security, account.account_type), ('imap.gmail.com', 993, 2, 1))
        for name in ['source:gmail', 'source:drive']:
            self.assertIsNone(self.tags.rows[name].owner)
            self.assertEqual(self.tags.rows[name].matching_algorithm, 0)
        receipt = self.rules.rows['gmail-receipt-bodies']
        attachments = self.rules.rows['gmail-attachments']
        self.assertEqual((receipt.order, receipt.folder, receipt.consumption_scope, receipt.action_parameter), (1, 'paperless-receipt', 2, 'paperless-done-eml'))
        self.assertEqual((attachments.order, attachments.folder, attachments.consumption_scope, attachments.action_parameter), (2, '[Gmail]/Alle e-poster', 1, 'paperless-done-att'))
        for rule in [receipt, attachments]:
            self.assertIs(rule.owner, self.owner)
            self.assertTrue(rule.assign_owner_from_rule)
            self.assertEqual((rule.maximum_age, rule.action, rule.assign_title_from, rule.assign_correspondent_from, rule.attachment_type), (0, 5, 1, 1, 1))
        self.assertEqual([tag.name for tag in receipt.assign_tags.items], ['source:gmail', 'paperless-gpt-auto'])
        self.assertEqual([tag.name for tag in attachments.assign_tags.items], ['source:gmail'])
        self.assertEqual([tag.name for tag in self.trigger.filter_has_not_tags.items], ['source:gmail', 'source:drive'])

    def test_missing_prerequisite_changes_nothing(self):
        for manager in [self.users, self.workflows]:
            with self.subTest(manager=manager):
                previous = manager.rows
                manager.rows = {}
                with self.assertRaises(ValueError):
                    self.run_configure()
                self.assertEqual(self.accounts.rows, {})
                manager.rows = previous


class DriveRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.drive = load('drive_import')

    def run_import(self, directory, *, plan=False, bodies=None, lengths=None, errors=None, post_status=200, download_text=None, mime='application/pdf', free=10**12, nontext=False, paperless_token=True, sizes=(3, 3), listing_error=None):
        bodies = bodies or [b'abc', b'def']
        lengths = lengths or [3, 3]
        errors = errors or set()
        self.downloads = []
        self.uploads = []
        self.searches = []
        self.exports = []
        self.bearers = []
        self.delays = []
        clock = [10000.0]

        async def sleep(delay):
            self.delays.append(delay)
            clock[0] += delay

        test = self

        class Response:
            status_code = 200
            headers = {}

            def __init__(self, index=None):
                self.index = index
                if index is not None:
                    self.headers = {} if lengths[index] is None else {'content-length': str(lengths[index])}

            def json(self):
                return {'results': [{'id': 7, 'name': 'source:drive'}]} if self.index is None else '12345678-1234-4234-8234-123456789abc'

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError('sensitive response must not escape')

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def aiter_raw(self):
                yield bodies[self.index]
                if self.index in errors:
                    raise OSError('synthetic transport failure with sensitive data')

        class Client:
            def __init__(self, **kwargs):
                self.headers = kwargs.get('headers', {})
                test.bearers.append(self.headers)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, **kwargs):
                return Response()

            def stream(self, method, url, **kwargs):
                test.downloads.append(url)
                return Response(len(test.downloads) - 1)

            async def post(self, url, **kwargs):
                test.uploads.append((url, kwargs['data'], kwargs['files']['document'][0], kwargs['files']['document'][1].read()))
                response = Response(0)
                response.status_code = post_status
                return response

        @contextlib.asynccontextmanager
        async def transport(url, **kwargs):
            yield None, None

        class Session:
            def __init__(self, *args):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def initialize(self):
                pass

            async def call_tool(self, name, arguments):
                if name == 'search_drive_files':
                    test.searches.append(arguments)
                    if listing_error is not None:
                        raise listing_error
                    index = 0 if not arguments.get('page_token') else 1
                    size = '' if sizes[index] is None else f', Size: {sizes[index]}'
                    text = f'- Name: "file{index}.pdf" (ID: id{index}, Type: {mime}{size}, Created: ..., Modified: ..., Last Edited By: X <x@example.test>) Link: https://example.test'
                    if index == 0:
                        text += '\nnextPageToken: second'
                else:
                    test.exports.append(arguments)
                    text = download_text if download_text is not None else '📎 Download URL: http://h/drive/attachments/12345678-1234-4234-8234-123456789abc'
                    if nontext:
                        return SimpleNamespace(is_error=False, content=[SimpleNamespace(type='image', data='synthetic')])
                return SimpleNamespace(is_error=False, content=[SimpleNamespace(type='text', text=text)])

        modules = {
            'httpx': None,
            'httpx2': SimpleNamespace(AsyncClient=Client),
            'mcp': SimpleNamespace(ClientSession=Session),
            'mcp.client': SimpleNamespace(),
            'mcp.client.streamable_http': SimpleNamespace(streamable_http_client=transport),
        }
        argv = ['drive_import.py', '--out', str(directory), '--hub-url', 'http://h/drive']
        if plan:
            argv.append('--plan-only')
        tokens = {'MCP_HUB_TOKEN': 'synthetic-hub-token'}
        if paperless_token:
            tokens['VAULT_PL_TOKEN'] = 'synthetic-paperless-token'
        with patch.dict(sys.modules, modules), patch.dict('os.environ', tokens, clear=True), patch.object(sys, 'argv', argv), patch.object(self.drive.shutil, 'disk_usage', return_value=SimpleNamespace(free=free)), patch.object(self.drive.time, 'time', side_effect=lambda: clock[0]), patch.object(self.drive.asyncio, 'sleep', sleep), contextlib.redirect_stdout(io.StringIO()) as output:
            result = self.drive.main()
        self.assertNotIn('synthetic-', output.getvalue())
        return result, json.loads((Path(directory) / 'report.json').read_text())

    def test_plan_lists_pages_without_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            result, report = self.run_import(directory, plan=True, paperless_token=False)
            self.assertEqual(result, 0)
            self.assertEqual(self.downloads, [])
            self.assertEqual(self.uploads, [])
            self.assertEqual(len(self.searches), 2)
            self.assertEqual(self.searches[0]['query'], "'me' in owners and mimeType != 'application/vnd.google-apps.folder'")
            self.assertEqual(self.searches[0]['corpora'], 'user')
            self.assertEqual(self.searches[0]['page_size'], 1000)
            self.assertEqual(report['counts'], {'planned': 2})
            self.assertEqual(report['total_bytes'], 6)

    def test_uploads_and_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            _, report = self.run_import(directory)
            self.assertEqual(report['counts'], {'uploaded': 2})
            self.assertEqual(len(self.uploads), 2)
            self.assertEqual(self.bearers, [{'Authorization': 'Bearer synthetic-hub-token'}, {'Authorization': 'Token synthetic-paperless-token'}])
            self.assertEqual(self.uploads[0][1], {'title': 'file0.pdf', 'tags': '7'})
            self.assertEqual(self.uploads[0][3], b'abc')
            rows = [json.loads(line) for line in (Path(directory) / 'manifest.jsonl').read_text().splitlines()]
            self.assertEqual([row for row in rows if row['status'] == 'uploaded'][0]['task_uuid'], '12345678-1234-4234-8234-123456789abc')
            self.assertEqual(set(p.name for p in Path(directory).iterdir()), {'manifest.jsonl', 'report.json'})
            with (Path(directory) / 'manifest.jsonl').open('a') as manifest:
                manifest.write('{"id":"id0","status":"failed"}\n')
            self.run_import(directory)
            self.assertEqual(self.downloads, [])

    def test_short_download_fails_then_continues_and_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            _, report = self.run_import(directory, bodies=[b'a', b'def'])
            self.assertEqual(report['counts'], {'failed': 1, 'uploaded': 1})
            self.assertEqual(report['failed_ids'], ['id0'])
            self.assertEqual(len(self.uploads), 1)
            self.assertEqual(set(p.name for p in Path(directory).iterdir()), {'manifest.jsonl', 'report.json'})
            self.run_import(directory)
            self.assertEqual(len(self.downloads), 1)

    def test_midstream_failure_fails_and_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            _, report = self.run_import(directory, errors={0})
            self.assertEqual(report['failed_ids'], ['id0'])
            self.assertEqual(len(self.uploads), 1)
            self.assertEqual(set(p.name for p in Path(directory).iterdir()), {'manifest.jsonl', 'report.json'})

    def test_http_upload_failure_records_status(self):
        with tempfile.TemporaryDirectory() as directory:
            _, report = self.run_import(directory, post_status=503)
            self.assertEqual(report['counts'], {'failed': 2})
            rows = [json.loads(line) for line in (Path(directory) / 'manifest.jsonl').read_text().splitlines()]
            self.assertEqual([row for row in rows if row['status'] == 'failed'][0]['http_status'], 503)

    def test_unsupported_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            _, report = self.run_import(directory, mime='application/zip')
            self.assertEqual(report['counts'], {'skipped': 2})
            self.assertEqual(self.downloads, [])

    def test_shortcuts_without_size_are_skipped(self):
        for plan in [False, True]:
            with self.subTest(plan=plan), tempfile.TemporaryDirectory() as directory:
                result, report = self.run_import(directory, plan=plan, mime='application/vnd.google-apps.shortcut', sizes=(None, None))
                self.assertEqual(result, 0)
                self.assertEqual(report['counts'], {'skipped': 2})
                self.assertEqual(report['unknown_size_files'], 2)
                self.assertEqual(self.downloads, [])
                self.assertEqual(self.uploads, [])
                rows = [json.loads(line) for line in (Path(directory) / 'manifest.jsonl').read_text().splitlines()]
                self.assertEqual([row['id'] for row in rows], ['id0', 'id1'])
                self.assertTrue(all(row['status'] == 'skipped' for row in rows))

    def test_oversized_file_fails_and_later_file_uploads(self):
        with tempfile.TemporaryDirectory() as directory:
            result, report = self.run_import(directory, sizes=(1073741825, 3))
            self.assertEqual(report['counts'], {'failed': 1, 'uploaded': 1})
            self.assertEqual(report['failed_ids'], ['id0'])
            self.assertNotEqual(result, 0)
            self.assertFalse(report['stopped'])
            self.assertEqual(self.uploads[0][1]['title'], 'file1.pdf')
            rows = [json.loads(line) for line in (Path(directory) / 'manifest.jsonl').read_text().splitlines()]
            self.assertEqual(rows[0]['error'], 'budget_limit')
            self.assertEqual(len(self.exports), 1)

    def test_listing_failure_reports_only_exception_type(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()) as errors:
            result, report = self.run_import(directory, listing_error=RuntimeError('sensitive listing response'))
            self.assertEqual(report.get('stopped_error'), 'RuntimeError')
            self.assertTrue(report['stopped'])
            self.assertNotEqual(result, 0)
            self.assertNotIn('sensitive', json.dumps(report))
            self.assertEqual(errors.getvalue(), '')
            self.assertEqual(self.downloads, [])
            self.assertEqual(self.uploads, [])

    def test_low_disk_stops_before_getting_link(self):
        with tempfile.TemporaryDirectory() as directory:
            result, report = self.run_import(directory, free=5)
            self.assertNotEqual(result, 0)
            self.assertEqual(self.exports, [])
            self.assertEqual(report['counts'].get('failed'), 1)

    def test_foreign_download_link_stops_without_sending_bearer(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _ = self.run_import(directory, download_text='📎 Download URL: http://foreign.example.test/drive/attachments/12345678-1234-4234-8234-123456789abc')
            self.assertNotEqual(result, 0)
            self.assertEqual(self.downloads, [])

    def test_resume_restores_the_raw_server_rolling_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'manifest.jsonl').write_text(json.dumps({'id': 'previous', 'name': 'previous.pdf', 'mime': 'application/pdf', 'size': 3, 'status': 'uploaded', 'budget_bytes': 1073741824, 'budget_time': 9990}) + '\n')
            self.run_import(directory)
            self.assertEqual(self.delays, [3590])

    def test_native_exports_reserve_ten_mib_not_listed_size(self):
        for mime in ['document', 'spreadsheet', 'presentation']:
            with self.subTest(mime=mime), tempfile.TemporaryDirectory() as directory:
                _, report = self.run_import(directory, mime='application/vnd.google-apps.' + mime)
                self.assertEqual(report['counts'], {'uploaded': 2})
                self.assertEqual(self.exports[0]['export_format'], 'pdf')
                self.assertEqual(self.uploads[0][2], 'file0.pdf.pdf')
                rows = [json.loads(line) for line in (Path(directory) / 'manifest.jsonl').read_text().splitlines()]
                self.assertTrue(rows)
                self.assertTrue(all(row['budget_bytes'] == 10 * 1024 * 1024 for row in rows))

    def test_native_exports_fit_in_one_hour(self):
        with tempfile.TemporaryDirectory() as directory:
            result, report = self.run_import(directory, mime='application/vnd.google-apps.spreadsheet')
            self.assertEqual(result, 0)
            self.assertEqual(report['counts'], {'uploaded': 2})
            self.assertEqual(self.delays, [])
            rows = [json.loads(line) for line in (Path(directory) / 'manifest.jsonl').read_text().splitlines()]
            reservations = [row for row in rows if row['status'] == 'downloading']
            budget = self.drive.Budget(1024**3, 3600)
            budget.record(reservations[0]['budget_time'], reservations[0]['budget_bytes'])
            self.assertEqual(budget.wait_seconds(reservations[1]['budget_time'], reservations[1]['budget_bytes']), 0)

    def test_native_export_body_over_reservation_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            result, report = self.run_import(
                directory, mime='application/vnd.google-apps.document',
                bodies=[b'x' * (10 * 1024 * 1024 + 1), b'def'], lengths=[None, None],
            )
            self.assertNotEqual(result, 0)
            self.assertEqual(report['failed_ids'], ['id0'])
            self.assertEqual(report['counts'], {'failed': 1, 'uploaded': 1})
            self.assertEqual(len(self.uploads), 1)
            self.assertEqual(self.uploads[0][3], b'def')
            self.assertEqual(set(p.name for p in Path(directory).iterdir()), {'manifest.jsonl', 'report.json'})

    def test_interrupted_reservation_waits_a_full_hour(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'manifest.jsonl').write_text(json.dumps({'id': 'previous', 'name': 'previous.pdf', 'mime': 'application/pdf', 'size': 3, 'status': 'downloading', 'attempt_id': 'attempt0', 'budget_bytes': 1073741824, 'budget_time': 1}) + '\n')
            self.run_import(directory)
            self.assertEqual(self.delays, [3600])

    def test_download_without_length_can_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            _, report = self.run_import(directory, lengths=[None, None])
            self.assertEqual(report['counts'], {'uploaded': 2})

    def test_nontext_inline_content_stops_run(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _ = self.run_import(directory, nontext=True)
            self.assertNotEqual(result, 0)
            self.assertEqual(self.downloads, [])
            self.assertEqual(len(self.exports), 1)

    def test_inline_content_stops_run(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _ = self.run_import(directory, download_text='inline content')
            self.assertNotEqual(result, 0)
            self.assertEqual(self.downloads, [])
            self.assertEqual(len(self.exports), 1)


if __name__ == '__main__':
    unittest.main()
