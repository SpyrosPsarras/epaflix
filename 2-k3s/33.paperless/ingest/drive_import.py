import argparse
import asyncio
from collections import Counter, deque
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from typing import NamedTuple
from urllib.parse import urlsplit
from uuid import UUID, uuid4


class DriveFile(NamedTuple):
    id: str
    name: str
    mime: str
    size: int | None


GOOGLE_EXPORT_LIMIT = 10 * 1024 * 1024
GOOGLE_TYPES = {
    'application/vnd.google-apps.document',
    'application/vnd.google-apps.spreadsheet',
    'application/vnd.google-apps.presentation',
}
SUPPORTED_TYPES = {
    'application/pdf',
    'image/jpeg',
    'image/png',
    'image/tiff',
    'image/gif',
    'image/webp',
    'text/plain',
    'text/csv',
    'application/csv',
    'message/rfc822',
    'application/msword',
    'application/vnd.ms-excel',
    'application/vnd.ms-powerpoint',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'application/vnd.oasis.opendocument.text',
    'application/vnd.oasis.opendocument.spreadsheet',
    'application/vnd.oasis.opendocument.presentation',
    'text/rtf',
} | GOOGLE_TYPES


def parse_page(text: str) -> tuple[list[DriveFile], str | None]:
    files = []
    token = None
    pattern = re.compile(r'^- Name: "(.*)" \(ID: ([^,]+), Type: ([^,]+)(?:, Size: ([^,]+))?, Created: .*\) Link: \S+$')
    for line in text.splitlines():
        if line.startswith('- Name:'):
            match = pattern.fullmatch(line)
            if not match:
                raise ValueError('Unrecognized Drive file listing')
            name, file_id, mime, size = match.groups()
            files.append(DriveFile(file_id, name, mime, int(size) if size is not None and size.isdecimal() else None))
        elif line.startswith('nextPageToken:'):
            token = line.partition(':')[2].strip() or None
    return files, token


def parse_download_url(text: str) -> str:
    match = re.search(r'^\s*(?:📎\s*)?Download URL: (https?://\S+)\s*$', text, re.MULTILINE)
    if not match:
        raise ValueError('Drive tool did not return a download URL')
    return match[1]


def export_format(mime: str) -> str | None:
    return 'pdf' if mime in GOOGLE_TYPES else None


def supported(mime: str) -> bool:
    return mime in SUPPORTED_TYPES


class Budget:
    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self.events = deque()

    def wait_seconds(self, now: float, size: int) -> float:
        if size < 0 or size > self.limit:
            raise ValueError('File exceeds the rolling download budget')
        while self.events and self.events[0][0] + self.window <= now:
            self.events.popleft()
        used = sum(event_size for _, event_size in self.events)
        if used + size <= self.limit:
            return 0
        for timestamp, event_size in self.events:
            used -= event_size
            if used + size <= self.limit:
                return max(0, timestamp + self.window - now)
        return 0

    def record(self, now: float, size: int):
        self.events.append((now, size))


def done_ids(manifest_path) -> set[str]:
    path = Path(manifest_path)
    if not path.exists():
        return set()
    return {row['id'] for line in path.read_text().splitlines() if (row := json.loads(line))['status'] == 'uploaded'}


def download_complete(received: int, content_length: int | None) -> bool:
    return content_length is None or received == content_length


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--paperless-url', default='http://127.0.0.1:18000')
    parser.add_argument('--hub-url', default='http://mcp-hub.mcp-hub-homelab.svc.cluster.local:8000/drive')
    parser.add_argument('--plan-only', action='store_true')
    args = parser.parse_args()

    import httpx2 as httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    os.umask(0o077)
    args.out.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.out.is_symlink() or args.out.stat().st_mode & 0o077:
        print('Output directory must be private', file=sys.stderr)
        return 1
    manifest_path = args.out / 'manifest.jsonl'
    report_path = args.out / 'report.json'
    for path in [manifest_path, report_path]:
        if path.is_symlink():
            print('Output files must not be symlinks', file=sys.stderr)
            return 1
        if path.exists():
            path.chmod(0o600)

    async def run():
        files = []
        latest = {}
        reservations = {}
        done = set()
        if manifest_path.exists():
            for index, line in enumerate(manifest_path.read_text().splitlines()):
                row = json.loads(line)
                latest[row['id']] = row
                if row['status'] == 'uploaded':
                    done.add(row['id'])
                if 'budget_time' in row:
                    reservations[row.get('attempt_id', f'legacy-{index}')] = row
        budget = Budget(1024**3, 3600)
        hub_headers = {'Authorization': 'Bearer ' + os.environ['MCP_HUB_TOKEN']}
        pl_headers = {} if args.plan_only else {'Authorization': 'Token ' + os.environ['VAULT_PL_TOKEN']}
        stopped = False
        stopped_error = None

        def record(file, status, **details):
            row = dict(zip(DriveFile._fields, file)) | {'status': status} | details
            with manifest_path.open('a') as manifest:
                manifest.write(json.dumps(row) + '\n')
                manifest.flush()
                os.fsync(manifest.fileno())
            latest[file.id] = row

        for row in sorted(reservations.values(), key=lambda row: row['budget_time']):
            timestamp = row['budget_time']
            if row['status'] == 'downloading':
                timestamp = time.time()
                record(
                    DriveFile(*(row[key] for key in DriveFile._fields)),
                    'failed', error='interrupted', attempt_id=row['attempt_id'],
                    budget_time=timestamp, budget_bytes=row['budget_bytes'],
                )
            budget.record(timestamp, row['budget_bytes'])
        budget.events = deque(sorted(budget.events))

        async def tool_text(session, name, arguments):
            result = await session.call_tool(name, arguments)
            if result.is_error:
                raise RuntimeError('Drive MCP call failed')
            if any(block.type != 'text' for block in result.content):
                raise ValueError('Drive MCP returned non-text content')
            return '\n'.join(block.text for block in result.content)

        try:
            async with httpx.AsyncClient(headers=hub_headers, timeout=300.0, follow_redirects=False) as hub:
                async with streamable_http_client(args.hub_url, http_client=hub) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        token = None
                        seen_tokens = set()
                        while True:
                            arguments = {
                                'query': "'me' in owners and mimeType != 'application/vnd.google-apps.folder'",
                                'corpora': 'user',
                                'page_size': 1000,
                                'detailed': True,
                            }
                            if token:
                                arguments['page_token'] = token
                            page, token = parse_page(await tool_text(session, 'search_drive_files', arguments))
                            files.extend(page)
                            if not token:
                                break
                            if token in seen_tokens:
                                raise ValueError('Drive listing repeated a page token')
                            seen_tokens.add(token)

                        async with httpx.AsyncClient(headers=pl_headers, timeout=300.0, follow_redirects=False) as paperless:
                            tag_id = None
                            if not args.plan_only:
                                response = await paperless.get(args.paperless_url.rstrip('/') + '/api/tags/?name__iexact=source:drive')
                                response.raise_for_status()
                                tags = response.json()['results']
                                if len(tags) != 1 or tags[0]['name'] != 'source:drive':
                                    raise ValueError('Expected exactly one source:drive tag')
                                tag_id = tags[0]['id']
                            for file in files:
                                if file.id in done:
                                    continue
                                if not supported(file.mime):
                                    record(file, 'skipped')
                                    continue
                                if args.plan_only:
                                    record(file, 'planned')
                                    continue
                                reserve = GOOGLE_EXPORT_LIMIT if export_format(file.mime) else budget.limit if file.size is None else file.size
                                if reserve > budget.limit:
                                    record(file, 'failed', error='budget_limit')
                                    continue
                                if shutil.disk_usage(args.out).free < 2 * reserve:
                                    record(file, 'failed', error='disk_space')
                                    stopped = True
                                    break
                                while delay := budget.wait_seconds(time.time(), reserve):
                                    await asyncio.sleep(delay)
                                local_path = None
                                response = None
                                received = 0
                                phase = 'download_link'
                                reservation = {'attempt_id': uuid4().hex, 'budget_time': time.time(), 'budget_bytes': reserve}
                                try:
                                    record(file, 'downloading', **reservation)
                                    arguments = {'file_id': file.id}
                                    if file_export := export_format(file.mime):
                                        arguments['export_format'] = file_export
                                    try:
                                        text = await tool_text(session, 'get_drive_file_download_url', arguments)
                                    finally:
                                        reservation['budget_time'] = time.time()
                                        budget.record(reservation['budget_time'], reserve)
                                    url = parse_download_url(text)
                                    target, origin = urlsplit(url), urlsplit(args.hub_url)
                                    if (target.scheme, target.netloc) != (origin.scheme, origin.netloc) or target.username or target.password or target.query or target.fragment or not re.fullmatch(r'/drive/attachments/[0-9a-fA-F-]{36}', target.path):
                                        raise ValueError('Unexpected download origin or path')
                                    UUID(target.path.rsplit('/', 1)[1])
                                    phase = 'download'
                                    with tempfile.NamedTemporaryFile(dir=args.out, prefix='download-', delete=False) as local:
                                        local_path = Path(local.name)
                                        async with hub.stream('GET', url, headers={'Accept-Encoding': 'identity'}) as response:
                                            response.raise_for_status()
                                            length = response.headers.get('content-length')
                                            length = int(length) if length is not None else None
                                            if length is not None and (length < 0 or length > reserve):
                                                raise ValueError('Download exceeds reserved size')
                                            async for chunk in response.aiter_raw():
                                                received += len(chunk)
                                                if received > reserve:
                                                    raise ValueError('Download exceeds reserved size')
                                                local.write(chunk)
                                            if not download_complete(received, length):
                                                record(file, 'failed', error='short_download', bytes_received=received, **reservation)
                                                continue
                                    upload_name = file.name + '.pdf' if export_format(file.mime) else file.name
                                    phase = 'upload'
                                    response = None
                                    with local_path.open('rb') as document:
                                        response = await paperless.post(
                                            args.paperless_url.rstrip('/') + '/api/documents/post_document/',
                                            data={'title': file.name, 'tags': str(tag_id)},
                                            files={'document': (upload_name, document, 'application/pdf' if export_format(file.mime) else file.mime)},
                                        )
                                    response.raise_for_status()
                                    task_uuid = str(UUID(response.json()))
                                    record(file, 'uploaded', task_uuid=task_uuid, downloaded_bytes=received, **reservation)
                                except Exception as error:
                                    record(file, 'failed', error=phase, http_status=response.status_code if response is not None else None, **reservation)
                                    if phase == 'download_link' and isinstance(error, ValueError):
                                        stopped = True
                                        break
                                finally:
                                    if local_path is not None:
                                        local_path.unlink(missing_ok=True)
        except Exception as error:
            stopped = True
            stopped_error = type(error).__name__
        finally:
            rows = [latest.get(file.id, {'status': 'pending'}) | {'mime': file.mime} for file in files]
            report = {
                'counts': dict(Counter(row['status'] for row in rows)),
                'mime_counts': dict(Counter(file.mime for file in files)),
                'total_bytes': sum(file.size or 0 for file in files),
                'unknown_size_files': sum(file.size is None for file in files),
                'downloaded_bytes': sum(row.get('downloaded_bytes', 0) for row in rows),
                'failed_ids': [file.id for file in files if latest.get(file.id, {}).get('status') == 'failed'],
                'stopped': stopped,
                'stopped_error': stopped_error,
            }
            report_path.write_text(json.dumps(report, indent=2) + '\n')
        return 1 if stopped or report['failed_ids'] else 0

    try:
        return asyncio.run(run())
    except Exception:
        print('Drive import failed; no response content was logged', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
