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

            def get_or_create(self, name, defaults):
                if name in self.rows:
                    return self.rows[name], False
                return self.update_or_create(name, defaults)

            def update_or_create(self, name, defaults, create_defaults=None):
                created = name not in self.rows
                row = self.rows.setdefault(name, SimpleNamespace(name=name, assign_tags=Relation()))
                for key, value in (create_defaults or defaults).items() if created else defaults.items():
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
        self.fields = Manager()
        self.modules = {
            'django': SimpleNamespace(),
            'django.contrib': SimpleNamespace(),
            'django.contrib.auth': SimpleNamespace(get_user_model=lambda: SimpleNamespace(objects=self.users)),
            'django.db': SimpleNamespace(transaction=SimpleNamespace(atomic=contextlib.nullcontext)),
            'documents': SimpleNamespace(),
            'documents.models': SimpleNamespace(CustomField=SimpleNamespace(objects=self.fields, FieldDataType=SimpleNamespace(STRING='string')), Tag=SimpleNamespace(objects=self.tags, MATCH_NONE=0), Workflow=SimpleNamespace(objects=self.workflows), WorkflowTrigger=SimpleNamespace(WorkflowTriggerType=SimpleNamespace(DOCUMENT_ADDED=2))),
            'paperless_mail': SimpleNamespace(),
            'paperless_mail.models': SimpleNamespace(
                MailAccount=SimpleNamespace(objects=self.accounts, ImapSecurity=SimpleNamespace(SSL=2), MailAccountType=SimpleNamespace(IMAP=1)),
                MailRule=SimpleNamespace(objects=self.rules, ConsumptionScope=SimpleNamespace(ATTACHMENTS_ONLY=1, EML_ONLY=2), AttachmentProcessing=SimpleNamespace(ATTACHMENTS_ONLY=1), MailAction=SimpleNamespace(TAG=5), TitleSource=SimpleNamespace(FROM_SUBJECT=1), CorrespondentSource=SimpleNamespace(FROM_NOTHING=1)),
            ),
        }

    def run_configure(self, shell=False):
        data = json.dumps({'owner': 'owner@example.test', 'email': 'mail@example.test', 'password': 'synthetic-password'})
        with patch.dict(sys.modules, self.modules), patch.object(sys, 'stdin', io.StringIO(data)), contextlib.redirect_stdout(io.StringIO()) as output:
            if shell:
                runpy.run_path(str(HERE / 'configure.py'), run_name='django.core.management.commands.shell')
            else:
                self.configure.main()
        self.assertNotIn('example.test', output.getvalue())
        self.assertNotIn('synthetic-password', output.getvalue())

    def test_configuration_is_private_and_idempotent(self):
        self.run_configure()
        self.assertFalse(self.rules.rows['gmail-attachments'].enabled)
        self.rules.rows['gmail-attachments'].enabled = True
        self.run_configure(shell=True)
        self.assertTrue(self.rules.rows['gmail-attachments'].enabled)
        self.rules.rows['gmail-attachments'].enabled = False
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
        self.assertEqual((receipt.order, receipt.folder, receipt.consumption_scope, receipt.action_parameter), (1, 'paperless-receipt-body', 2, 'paperless-done-eml'))
        self.assertEqual((attachments.order, attachments.folder, attachments.consumption_scope, attachments.action_parameter), (2, 'paperless-receipt', 1, 'paperless-done-att'))
        self.assertIsNone(receipt.filter_attachment_filename_include)
        self.assertEqual(attachments.filter_attachment_filename_include, '*.pdf,*.doc,*.docx,*.xls,*.xlsx,*.odt,*.ods')
        for rule in [receipt, attachments]:
            self.assertIs(rule.owner, self.owner)
            self.assertTrue(rule.assign_owner_from_rule)
            self.assertEqual((rule.maximum_age, rule.action, rule.assign_title_from, rule.assign_correspondent_from, rule.attachment_type), (0, 5, 1, 1, 1))
            self.assertFalse(rule.enabled)
        self.assertEqual(self.fields.rows['Gmail thread'].data_type, 'string')
        self.assertEqual([tag.name for tag in receipt.assign_tags.items], ['source:gmail', 'paperless-gpt-auto'])
        self.assertEqual([tag.name for tag in attachments.assign_tags.items], ['source:gmail', 'paperless-gpt-auto'])
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


def mail(body='Hi', html=None, parts=(), headers=None):
    from email.message import EmailMessage
    msg = EmailMessage()
    msg['Subject'] = 'Faktura 12'
    msg['From'] = 'Shop <shop@example.test>'
    msg['To'] = 'owner@example.test'
    msg['Date'] = 'Fri, 27 Nov 2026 14:57:30 +0000'
    for key, value in (headers or {}).items():
        msg[key] = value
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype='html')
    for maintype, subtype, data, filename, disposition in parts:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename, disposition=disposition)
    if not body and html is not None:
        msg = EmailMessage()
        msg['Subject'] = 'Faktura 12'
        msg['From'] = 'Shop <shop@example.test>'
        msg.set_content(html, subtype='html')
    return msg


class ThreadSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = load('thread_sync')

    def test_eligible_parts_inline_nameless_pdf(self):
        msg = mail(parts=[('application', 'pdf', b'%PDF-1', None, 'inline'), ('image', 'png', b'png', 'logo.png', 'inline'), ('application', 'octet-stream', b'doc', 'x.docx', 'attachment'), ('application', 'pdf', b'%PDF-2', None, 'inline')])
        parts = self.ts.eligible_parts(msg, 'Faktura 12')
        self.assertEqual(parts, [
            self.ts.Part('Faktura 12.pdf', b'%PDF-1'),
            self.ts.Part('x.docx', b'doc'),
            self.ts.Part('Faktura 12 2.pdf', b'%PDF-2'),
        ])
        self.assertEqual(self.ts.eligible_parts(mail(parts=[('application', 'pdf', b'%PDF', 'a/b:c.pdf', 'attachment')]), 'x')[0].name, 'a_b_c.pdf')

    def test_cut_quote(self):
        for marker in ['> old', 'On Fri, 27 Nov 2026 Shop <shop@example.test> wrote:', 'Den 27. nov. 2026 skrev Shop:', '-----Original Message-----', 'From: Shop', 'Fra: Shop']:
            with self.subTest(marker=marker):
                self.assertEqual(self.ts.cut_quote(f'Thanks\n\n{marker}\nold text'), 'Thanks')
        self.assertEqual(self.ts.cut_quote('Hi\nOn Fri, 27 Nov 2026 at 15:57, Shop <\nshop@example.test> wrote:\nold'), 'Hi')
        self.assertEqual(self.ts.cut_quote('  Hi\n\n\n\nthere  '), 'Hi\n\nthere')

    def test_body_text_html_only(self):
        text = self.ts.body_text(mail(body='', html='<p>Hi<br>there</p><style>p{}</style><blockquote>old</blockquote>'))
        self.assertEqual(text, 'Hi\nthere')
        nested = self.ts.html_text('<p>Hi</p><div class="gmail_quote"><div>old<br>older</div><img src=x>oldest</div><p>after</p>')
        self.assertEqual(nested.split(), ['Hi', 'after'])
        self.assertEqual(self.ts.body_text(mail(body='Plain\n> quoted', html='<p>Html</p>')), 'Plain')

    def test_note_text(self):
        from datetime import datetime
        self.assertEqual(self.ts.note_text('Shop <shop@example.test>', datetime(2026, 11, 27, 15, 57), 'Thanks'), 'From Shop <shop@example.test>, 2026-11-27 15:57\n\nThanks')
        self.assertIsNone(self.ts.note_text('Shop', datetime(2026, 11, 27), ''))

    def test_eml_bytes_starts_with_from(self):
        raw = b'Subject: x\r\nFrom: a@example.test\r\n\r\nbody\r\n'
        out = self.ts.eml_bytes(raw)
        self.assertTrue(out.startswith(b'From: a@example.test'))
        self.assertEqual(out, self.ts.eml_bytes(raw))

    def test_is_reply(self):
        self.assertFalse(self.ts.is_reply(mail()))
        self.assertTrue(self.ts.is_reply(mail(headers={'In-Reply-To': '<a@example.test>'})))
        self.assertTrue(self.ts.is_reply(mail(headers={'References': '<a@example.test>'})))

    def test_classify_non_reply_in_known_thread(self):
        c = self.ts.classify
        self.assertEqual(c({'paperless-receipt'}, True, False), 'new')
        self.assertEqual(c(set(), True, True), 'followup')
        self.assertEqual(c({'paperless-receipt-body'}, True, True), 'followup')
        self.assertIsNone(c(set(), True, False))
        self.assertIsNone(c(set(), False, True))
        self.assertEqual(c({'paperless-receipt-body'}, False, True), 'new')

    def plan(self, kind, parts, note='note', present=(), queued=(), failed=(), has_root=False):
        eml = self.ts.Part('Faktura 12.eml', b'eml')
        present = {self.ts.checksum(p.data) for p in present}
        names = {p.name: p for p in [eml, *parts]}
        key = lambda name: self.ts.key(names[name])
        return self.ts.plan_message(kind, parts, eml, note, present, {key(n) for n in queued}, {key(n) for n in failed}, has_root)

    def test_plan_message_idempotent(self):
        a, b = self.ts.Part('a.pdf', b'a'), self.ts.Part('b.pdf', b'b')
        self.assertEqual(self.plan('new', [a, b], present=[a, b], has_root=True), self.ts.Plan(None, [], 'note', True))
        self.assertEqual(self.plan('new', [a, b]), self.ts.Plan(a, [], None, False))
        self.assertEqual(self.plan('new', [a, b], present=[a], has_root=True), self.ts.Plan(None, [b], 'note', False))
        self.assertEqual(self.plan('followup', [a], has_root=True), self.ts.Plan(None, [a], 'note', False))
        self.assertEqual(self.plan('followup', [], has_root=True), self.ts.Plan(None, [], 'note', True))
        self.assertEqual(self.plan('new', [a], present=[a]), self.ts.Plan(None, [], None, True))

    def test_plan_message_waits_on_failed(self):
        a, b = self.ts.Part('a.pdf', b'a'), self.ts.Part('b.pdf', b'b')
        self.assertEqual(self.plan('new', [a, b], queued=['a.pdf']), self.ts.Plan(None, [], None, False))
        self.assertEqual(self.plan('new', [a, b], failed=['a.pdf']), self.ts.Plan(None, [], None, False, True))
        self.assertEqual(self.plan('followup', [a, b], failed=['b.pdf'], has_root=True), self.ts.Plan(None, [], None, False, True))
        self.assertEqual(self.plan('followup', [a], queued=['a.pdf'], has_root=True), self.ts.Plan(None, [], 'note', False))
        self.assertEqual(self.plan('followup', [a], present=[a], failed=['a.pdf'], has_root=True), self.ts.Plan(None, [], 'note', True))

    def test_plan_files_first_then_email_page(self):
        eml = self.ts.Part('Faktura 12.eml', b'eml')
        a = self.ts.Part('a.pdf', b'a')
        self.assertEqual(self.plan('new', [a]), self.ts.Plan(a, [], None, False))
        self.assertEqual(self.plan('new', []), self.ts.Plan(eml, [], None, False))
        self.assertEqual(self.plan('new', [], present=[eml], has_root=True), self.ts.Plan(None, [], None, True))
        self.assertEqual(self.plan('followup', [], has_root=True).new, None)

    def test_needs_join(self):
        n = self.ts.needs_join
        self.assertIsNone(n([(1, True)], None))
        self.assertEqual(n([(1, True), (2, True)], 'b.pdf'), [1, 2])
        self.assertIsNone(n([(1, True), (2, False)], 'b.pdf'))
        self.assertIsNone(n([(1, True), (2, True)], 'All files: 1, 2'))
        self.assertEqual(n([(1, True), (2, True), (3, True)], 'All files: 1, 2'), [1, 2, 3])
        many = list(range(26800, 26820))
        label = self.ts.joined_label(many)
        self.assertLessEqual(len(label), 64)
        self.assertTrue(label.startswith('All files: 20 files #'))
        self.assertNotEqual(label, self.ts.joined_label(many[:-1] + [1]))
        self.assertIsNone(n([(i, True) for i in many], label))

    def test_parse_gm_fetch(self):
        lines = [
            b'1 (X-GM-THRID 1855 X-GM-MSGID 1856 X-GM-LABELS ("\\\\Important" paperless-receipt "Old label") UID 94095)',
            (b'2 (UID 94114 X-GM-THRID 1855 X-GM-MSGID 1857 X-GM-LABELS () BODY[] {4}', b'body'),
            b')',
        ]
        self.assertEqual(self.ts.parse_gm_fetch(lines), {
            '94095': ('1856', '1855', {'\\Important', 'paperless-receipt', 'Old label'}),
            '94114': ('1857', '1855', set()),
        })


class FakeBox:
    def __init__(self):
        self.messages = {}

    def add(self, uid, thrid, labels, msg):
        self.messages[uid] = [f'm{uid}', thrid, set(labels), msg.as_bytes()]

    def search(self, queries):
        return [uid for uid, (_, _, labels, _) in self.messages.items() if not labels & {'paperless-done', 'paperless-failed'}]

    def metadata(self, uids):
        return {uid: tuple(self.messages[uid][:3]) for uid in uids}

    def message(self, uid):
        if self.messages[uid][3] is None:
            raise OSError('broken')
        return self.messages[uid][3]

    def mark(self, uid, label):
        self.messages[uid][2].add(label)


class FakeStore:
    def __init__(self, ts):
        self.ts = ts
        self.docs, self.threads, self.labels = {}, {}, {}
        self.queued, self.notes, self.joins = [], [], []
        self.waiting = (set(), set(), set())

    def consume(self, data, root=None, thrid=None, label=None):
        doc_id = len(self.docs) + 1
        self.docs[self.ts.checksum(data)] = (doc_id, root or doc_id)
        if root is None:
            self.threads.setdefault(thrid, []).append(doc_id)
        self.labels[root or doc_id] = label
        return doc_id

    def tasks(self, now):
        return tuple(set(items) for items in self.waiting)

    def document_for_thread(self, thrid):
        return max(self.threads.get(thrid, []), default=None)

    def present(self, sums):
        return {s for s in sums if s in self.docs}

    def root_with_thread(self, present, thrid):
        roots = {doc_id for s in present for doc_id, root in [self.docs[s]] if doc_id == root}
        return next((r for r in self.threads.get(thrid, []) if r in roots), None)

    def queue(self, part, root_id, thrid, title):
        self.queued.append((part.name, root_id, thrid, title))

    def add_note(self, doc_id, text):
        if (doc_id, text) in self.notes:
            return False
        self.notes.append((doc_id, text))
        return True

    def local(self, date):
        return date

    def join_candidates(self, now):
        return sorted({root for doc_id, root in self.docs.values() if doc_id != root})

    def originals(self, root):
        return [(doc_id, True) for doc_id, r in sorted(self.docs.values()) if r == root]

    def newest_label(self, root):
        return self.labels.get(root)

    def queue_join(self, root, ids):
        self.joins.append((root, ids))


class ThreadSyncRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = load('thread_sync')

    def test_sync_thread_end_to_end(self):
        from datetime import datetime, timezone
        box, store, now = FakeBox(), FakeStore(self.ts), datetime(2026, 11, 28, tzinfo=timezone.utc)
        invoice = mail(body='Here it is', parts=[('application', 'pdf', b'%PDF-invoice', 'invoice.pdf', 'attachment')])
        box.add('10', 't1', {'paperless-receipt'}, invoice)
        box.add('11', 't9', set(), mail(body='Newsletter'))

        counts = self.ts.sync(box, store, now)
        self.assertEqual(store.queued, [('invoice.pdf', None, 't1', 'Faktura 12')])
        self.assertNotIn('paperless-done', box.messages['10'][2])
        self.assertEqual(counts['created'], 1)

        store.waiting = ({self.ts.key(self.ts.Part('invoice.pdf', b'%PDF-invoice'))}, {'t1'}, set())
        store.queued.clear()
        self.ts.sync(box, store, now)
        self.assertEqual(store.queued, [])

        root = store.consume(b'%PDF-invoice', thrid='t1')
        store.waiting = (set(), set(), set())
        reply = mail(body='Corrected copy\n\nOn Fri, 27 Nov 2026 Shop <shop@example.test> wrote:\n> Here it is', parts=[('application', 'pdf', b'%PDF-corrected', 'corrected.pdf', 'attachment')], headers={'In-Reply-To': '<a@example.test>'})
        box.add('12', 't1', set(), reply)
        box.add('13', 't1', set(), mail(body='Monthly bill, not a reply'))
        self.ts.sync(box, store, now)
        self.assertIn('paperless-done', box.messages['10'][2])
        self.assertEqual(store.queued, [('corrected.pdf', root, 't1', 'Faktura 12')])
        self.assertEqual([text.splitlines()[-1] for _, text in store.notes], ['Here it is', 'Corrected copy'])
        self.assertNotIn('paperless-done', box.messages['12'][2])
        self.assertNotIn('paperless-done', box.messages['13'][2])

        store.consume(b'%PDF-corrected', root=root, label='corrected.pdf')
        store.queued.clear()
        counts = self.ts.sync(box, store, now)
        self.assertIn('paperless-done', box.messages['12'][2])
        self.assertEqual(store.queued, [])
        self.assertEqual(len(store.notes), 2)
        self.assertEqual(store.joins, [(root, [1, 2])])
        self.assertEqual(counts['joined'], 1)

        store.labels[root] = 'All files: 1, 2'
        store.joins.clear()
        self.ts.sync(box, store, now)
        self.assertEqual(store.joins, [])

        store.labels[root] = 'corrected.pdf'
        store.waiting = ({f'join{root}'}, set(), set())
        self.ts.sync(box, store, now)
        self.assertEqual(store.joins, [])

    def test_sync_body_reply_becomes_note(self):
        from datetime import datetime, timezone
        box, store, now = FakeBox(), FakeStore(self.ts), datetime(2026, 11, 28, tzinfo=timezone.utc)
        root = store.consume(b'%PDF-invoice', thrid='t1')
        box.add('20', 't1', {'paperless-receipt-body'}, mail(body='Scroll down in the attachment', headers={'References': '<a@example.test>'}))
        self.ts.sync(box, store, now)
        self.assertEqual(store.queued, [])
        self.assertEqual(store.notes[0][0], root)
        self.assertTrue(store.notes[0][1].startswith('From Shop <shop@example.test>, 2026-11-27 14:57'))
        self.assertIn('paperless-done', box.messages['20'][2])

    def test_sync_waits_for_queued_new_document_of_thread(self):
        from datetime import datetime, timezone
        box, store, now = FakeBox(), FakeStore(self.ts), datetime(2026, 11, 28, tzinfo=timezone.utc)
        store.consume(b'%PDF-october', thrid='t1')
        box.add('50', 't1', set(), mail(body='About November', headers={'In-Reply-To': '<nov@example.test>'}))
        store.waiting = ({'x'}, {'t1'}, set())
        counts = self.ts.sync(box, store, now)
        self.assertEqual((store.queued, store.notes, counts['waiting']), ([], [], 1))

    def test_sync_shared_secondary_file_does_not_pick_old_document(self):
        from datetime import datetime, timezone
        box, store, now = FakeBox(), FakeStore(self.ts), datetime(2026, 11, 28, tzinfo=timezone.utc)
        october = store.consume(b'%PDF-october', thrid='t1')
        store.consume(b'%PDF-terms', root=october, label='terms.pdf')
        box.add('60', 't1', {'paperless-receipt'}, mail(parts=[('application', 'pdf', b'%PDF-november', 'november.pdf', 'attachment'), ('application', 'pdf', b'%PDF-terms', 'terms.pdf', 'attachment')]))
        self.ts.sync(box, store, now)
        self.assertEqual(store.queued, [('november.pdf', None, 't1', 'Faktura 12')])

    def test_sync_failure_is_per_message(self):
        from datetime import datetime, timezone
        box, store, now = FakeBox(), FakeStore(self.ts), datetime(2026, 11, 28, tzinfo=timezone.utc)
        box.add('30', 't3', {'paperless-receipt-body'}, mail())
        box.messages['30'][3] = None
        box.add('31', 't4', {'paperless-receipt-body'}, mail(body='Receipt'))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            counts = self.ts.sync(box, store, now)
        self.assertEqual(counts['failed'], 1)
        self.assertEqual(output.getvalue(), 'thread-sync: message m30 failed: OSError\n')
        self.assertEqual([q[0] for q in store.queued], ['Faktura 12.eml'])

    def test_sync_failed_import_labels_message(self):
        from datetime import datetime, timezone
        box, store, now = FakeBox(), FakeStore(self.ts), datetime(2026, 11, 28, tzinfo=timezone.utc)
        box.add('40', 't5', {'paperless-receipt-body'}, mail(body='Receipt'))
        eml = self.ts.Part('Faktura 12.eml', self.ts.eml_bytes(box.messages['40'][3]))
        store.waiting = (set(), set(), {self.ts.key(eml)})
        counts = self.ts.sync(box, store, now)
        self.assertEqual(store.queued, [])
        self.assertEqual(box.messages['40'][2], {'paperless-receipt-body', 'paperless-failed'})
        self.assertEqual(counts['import_failed'], 1)
        self.ts.sync(box, store, now)
        self.assertEqual(counts['import_failed'], 1)


class ThreadMigrateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ts = load('thread_sync')
        cls.tm = load('thread_migrate')

    def test_plan_threads(self):
        Msg = self.tm.Msg
        messages = [
            Msg('5', 't1', 5, True, 'eml', [15], 'n5'),
            Msg('1', 't1', 1, False, 'att', [10, 11], 'n1'),
            Msg('2', 't1', 2, True, 'eml', [12], 'n2'),
            Msg('3', 't1', 3, True, 'att', [13], 'n3'),
            Msg('4', 't1', 4, False, 'eml', [14], 'n4'),
            Msg('6', 't2', 1, True, 'eml', [], 'n6'),
            Msg('7', 't2', 2, True, 'eml', [17], None),
            Msg('8', 't2', 3, True, 'att', [18], None),
        ]
        self.assertEqual(self.tm.plan_threads(messages), [
            ('field', 10, 't1'), ('version', 10, 11), ('note', 10, 'n1'),
            ('note', 10, 'n2'), ('delete', 10, 12),
            ('version', 10, 13), ('note', 10, 'n3'),
            ('field', 14, 't1'),
            ('note', 14, 'n5'), ('delete', 14, 15),
            ('field', 17, 't2'),
            ('version', 17, 18),
        ])

    def test_plan_threads_never_trashes_root(self):
        Msg = self.tm.Msg
        self.assertEqual(self.tm.plan_threads([Msg('1', 't1', 1, False, 'att', [10], None), Msg('2', 't1', 2, True, 'eml', [10], 'n2')]), [('field', 10, 't1'), ('note', 10, 'n2')])

    def test_plan_threads_is_idempotent_for_merged_documents(self):
        Msg = self.tm.Msg
        self.assertEqual(self.tm.plan_threads([Msg('1', 't1', 1, False, 'att', [10], None), Msg('2', 't1', 2, True, 'att', [10], None)]), [('field', 10, 't1')])


if __name__ == '__main__':
    unittest.main()
