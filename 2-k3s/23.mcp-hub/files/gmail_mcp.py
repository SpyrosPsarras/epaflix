#!/usr/bin/env python3
"""Gmail tools for the MCP hub, over the Gmail REST API (scope gmail.modify).

Env: GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN, produced by
../tools/bootstrap-gmail.py and mounted from Secret mcp-hub/mcp-hub-gmail.
Access tokens are refreshed in memory; nothing is written to disk. Only the
standard library is used for HTTP, so the pod installs just the mcp SDK.

Self-test: --selftest exercises the pure helpers (MIME build, body extraction,
label resolution) without touching the network or the env.
"""

import base64
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html.parser import HTMLParser

API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
HEADERS = ("From", "To", "Cc", "Bcc", "Subject", "Date", "Message-ID", "In-Reply-To", "References", "Reply-To")
_WANTED = {h.lower() for h in HEADERS}
# Moving mail to Trash/Spam goes through gmail_trash (approval-gated in clients), not gmail_modify.
MODIFY_FORBIDDEN = {"TRASH", "SPAM"}
MAX_ATTACHMENT = 5 * 1024 * 1024
LABEL_TTL = 300


class Auth:
    """Refresh-token grant. One access token shared by all sessions in the pod."""

    def __init__(self, client_id, client_secret, refresh_token):
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self._token, self._expires, self._lock = None, 0.0, threading.Lock()

    def token(self):
        with self._lock:
            if self._token and time.time() < self._expires - 60:
                return self._token
            data = urllib.parse.urlencode({
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            }).encode()
            try:
                with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data), timeout=30) as resp:
                    body = json.load(resp)
            except urllib.error.HTTPError as e:
                raise RuntimeError(f"Gmail token refresh failed ({e.code}): {e.read().decode(errors='replace')[:300]}. "
                                   "Re-run tools/bootstrap-gmail.py if the refresh token was revoked.") from None
            self._token = body["access_token"]
            self._expires = time.time() + body.get("expires_in", 3600)
            return self._token


AUTH = None
_labels_cache = {"at": 0.0, "by_name": {}, "by_id": {}}
_labels_lock = threading.Lock()


def _api(method, path, params=None, body=None):
    url = API + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {AUTH.token()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"Gmail API {method} {path} -> HTTP {e.code}: {detail}") from None


# ---- pure helpers -----------------------------------------------------------

def _b64url_decode(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _headers(payload):
    # Header names are case-insensitive (Postfix writes Message-Id).
    return {h["name"].lower(): h["value"] for h in payload.get("headers", []) if h["name"].lower() in _WANTED}


class _HTMLText(HTMLParser):
    SKIP = {"script", "style", "head"}
    BREAK = {"p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}

    def __init__(self):
        super().__init__()
        self.out, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BREAK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BREAK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def _html_to_text(html):
    p = _HTMLText()
    p.feed(html)
    text = "".join(p.out)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _walk(payload):
    yield payload
    for part in payload.get("parts", []):
        yield from _walk(part)


def _body_text(payload):
    """text/plain body if present, otherwise text/html flattened to text."""
    plain, html = [], []
    for part in _walk(payload):
        data = part.get("body", {}).get("data")
        if not data or part.get("filename"):
            continue
        mime = part.get("mimeType", "")
        text = _b64url_decode(data).decode("utf-8", errors="replace")
        if mime == "text/plain":
            plain.append(text)
        elif mime == "text/html":
            html.append(text)
    if plain:
        return "\n".join(plain)
    return _html_to_text("\n".join(html))


def _attachments(payload):
    return [
        {"filename": p["filename"], "mime_type": p.get("mimeType"), "size": p.get("body", {}).get("size"),
         "attachment_id": p.get("body", {}).get("attachmentId")}
        for p in _walk(payload) if p.get("filename")
    ]


def _summary(msg):
    h = _headers(msg.get("payload", {}))
    labels = msg.get("labelIds", [])
    return {
        "id": msg["id"], "thread_id": msg.get("threadId"), "date": h.get("date"),
        "from": h.get("from"), "to": h.get("to"), "cc": h.get("cc"), "subject": h.get("subject"),
        "snippet": msg.get("snippet"), "labels": labels, "unread": "UNREAD" in labels,
    }


def _full(msg, max_body_chars):
    out = _summary(msg)
    payload = msg.get("payload", {})
    h = _headers(payload)
    out.update({"message_id_header": h.get("message-id"), "in_reply_to": h.get("in-reply-to"),
                "reply_to": h.get("reply-to")})
    body = _body_text(payload)
    out["body"] = body[:max_body_chars]
    out["body_truncated"] = len(body) > max_body_chars
    out["attachments"] = _attachments(payload)
    return out


def _build_mime(to, subject, body, cc="", bcc="", in_reply_to=None, references=None):
    if not to.strip():
        raise ValueError("'to' must not be empty")
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _resolve_labels(names, by_name, by_id):
    """Accept label names (case-insensitive) or IDs; return IDs."""
    ids = []
    for n in names or []:
        if n in by_id:
            ids.append(n)
        elif n.lower() in by_name:
            ids.append(by_name[n.lower()])
        else:
            raise ValueError(f"unknown label '{n}' (use gmail_labels to list them)")
    return ids


# ---- API-backed operations --------------------------------------------------

def _labels():
    with _labels_lock:
        now = time.time()
        if now - _labels_cache["at"] > LABEL_TTL:
            labels = _api("GET", "/labels").get("labels", [])
            _labels_cache.update(at=now, by_name={l["name"].lower(): l["id"] for l in labels},
                                 by_id={l["id"]: l["name"] for l in labels})
        return _labels_cache


def _get_metadata(message_id):
    return _api("GET", f"/messages/{message_id}",
                {"format": "metadata", "metadataHeaders": list(HEADERS)})


def _search(q, max_results, include_spam_trash):
    max_results = max(1, min(int(max_results), 50))
    res = _api("GET", "/messages", {"q": q, "maxResults": max_results,
                                    "includeSpamTrash": "true" if include_spam_trash else "false"})
    ids = [m["id"] for m in res.get("messages", [])]
    if not ids:
        return {"query": q, "count": 0, "messages": []}
    with ThreadPoolExecutor(max_workers=8) as pool:
        msgs = list(pool.map(_get_metadata, ids))
    return {"query": q, "count": len(msgs), "estimated_total": res.get("resultSizeEstimate"),
            "next_page": bool(res.get("nextPageToken")), "messages": [_summary(m) for m in msgs]}


def _get(message_id, max_body_chars):
    return _full(_api("GET", f"/messages/{message_id}", {"format": "full"}), max_body_chars)


def _thread(thread_id, max_body_chars):
    t = _api("GET", f"/threads/{thread_id}", {"format": "full"})
    return {"thread_id": t["id"], "count": len(t.get("messages", [])),
            "messages": [_full(m, max_body_chars) for m in t.get("messages", [])]}


def _modify(message_ids, add_labels, remove_labels):
    if not message_ids:
        raise ValueError("message_ids must not be empty")
    cache = _labels()
    added = _resolve_labels(add_labels, cache["by_name"], cache["by_id"])
    if MODIFY_FORBIDDEN & set(added):
        raise ValueError("use gmail_trash to move mail to Trash; gmail_modify cannot add TRASH or SPAM")
    body = {"ids": message_ids,
            "addLabelIds": added,
            "removeLabelIds": _resolve_labels(remove_labels, cache["by_name"], cache["by_id"])}
    _api("POST", "/messages/batchModify", body=body)
    return {"modified": len(message_ids), "added": body["addLabelIds"], "removed": body["removeLabelIds"]}


def _compose(to, subject, body, cc, bcc, reply_to_message_id):
    """Return the Gmail message resource for a new mail or a reply in an existing thread."""
    resource = {}
    in_reply_to = references = None
    if reply_to_message_id:
        orig = _get_metadata(reply_to_message_id)
        h = _headers(orig["payload"])
        in_reply_to, references = h.get("message-id"), h.get("references")
        resource["threadId"] = orig["threadId"]
        if not subject:
            subject = h.get("subject", "")
            if not subject.lower().startswith("re:"):
                subject = "Re: " + subject
    resource["raw"] = _build_mime(to, subject, body, cc, bcc, in_reply_to=in_reply_to, references=references)
    return resource


def _draft(to, subject, body, cc, bcc, reply_to_message_id):
    d = _api("POST", "/drafts", body={"message": _compose(to, subject, body, cc, bcc, reply_to_message_id)})
    return {"draft_id": d["id"], "message_id": d["message"]["id"], "thread_id": d["message"].get("threadId")}


def _send(to, subject, body, cc, bcc, reply_to_message_id):
    m = _api("POST", "/messages/send", body=_compose(to, subject, body, cc, bcc, reply_to_message_id))
    return {"sent": True, "message_id": m["id"], "thread_id": m.get("threadId")}


def _send_draft(draft_id):
    m = _api("POST", "/drafts/send", body={"id": draft_id})
    return {"sent": True, "message_id": m["id"], "thread_id": m.get("threadId")}


def _trash(message_id):
    m = _api("POST", f"/messages/{message_id}/trash")
    return {"trashed": m["id"], "labels": m.get("labelIds", [])}


def _attachment(message_id, attachment_id):
    a = _api("GET", f"/messages/{message_id}/attachments/{attachment_id}")
    if a.get("size", 0) > MAX_ATTACHMENT:
        raise ValueError(f"attachment is {a['size']} bytes; limit is {MAX_ATTACHMENT}")
    return {"message_id": message_id, "attachment_id": attachment_id, "size": a.get("size"),
            "content_b64": base64.b64encode(_b64url_decode(a["data"])).decode()}


# ---- MCP server -------------------------------------------------------------

def _run(fn, *args):
    """JSON-encode a result; turn expected failures into ToolError so the message reaches the model (the SDK hides any other exception text)."""
    from mcp.server.mcpserver.exceptions import ToolError

    try:
        return json.dumps(fn(*args))
    except (RuntimeError, ValueError, KeyError, OSError) as e:
        raise ToolError(str(e)) from None


def server():
    """Build the MCPServer. Requires the GMAIL_* env; raises if incomplete."""
    from mcp.server.mcpserver import MCPServer

    global AUTH
    cid, secret, refresh = (os.environ.get(k, "") for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"))
    if not (cid and secret and refresh):
        raise SystemExit("GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN must be set "
                         "(Secret mcp-hub-gmail; run 2-k3s/23.mcp-hub/tools/bootstrap-gmail.py)")
    AUTH = Auth(cid, secret, refresh)

    mcp = MCPServer("gmail", instructions=(
        "Read-write access to the owner's personal Gmail. Search with Gmail query syntax "
        "(e.g. 'from:x newer_than:7d is:unread'). Prefer gmail_draft over gmail_send unless the user "
        "asked to send: a sent mail cannot be recalled. Trash is recoverable for 30 days."))

    @mcp.tool()
    def gmail_search(q: str, max_results: int = 20, include_spam_trash: bool = False) -> str:
        """Search messages with Gmail query syntax (from:, to:, subject:, label:, is:unread, newer_than:7d, has:attachment ...). Returns up to max_results (<=50) summaries: id, thread_id, from, subject, date, snippet, labels."""
        return _run(_search, q, max_results, include_spam_trash)

    @mcp.tool()
    def gmail_get(message_id: str, max_body_chars: int = 20000) -> str:
        """Fetch one message by id: headers, plain-text body (HTML flattened), attachment list with attachment_id."""
        return _run(_get, message_id, max_body_chars)

    @mcp.tool()
    def gmail_thread(thread_id: str, max_body_chars: int = 4000) -> str:
        """Fetch every message in a thread, oldest first, each with a body truncated to max_body_chars."""
        return _run(_thread, thread_id, max_body_chars)

    @mcp.tool()
    def gmail_labels() -> str:
        """List labels as {id: name}. System labels: INBOX, UNREAD, STARRED, IMPORTANT, SPAM, TRASH, SENT, DRAFT."""
        return _run(lambda: _labels()["by_id"])

    @mcp.tool()
    def gmail_modify(message_ids: list[str], add_labels: list[str] | None = None,
                     remove_labels: list[str] | None = None) -> str:
        """Add/remove labels (names or ids) on messages. Archive = remove INBOX; mark read = remove UNREAD; star = add STARRED. Cannot add TRASH/SPAM (use gmail_trash)."""
        return _run(_modify, message_ids, add_labels or [], remove_labels or [])

    @mcp.tool()
    def gmail_draft(to: str, subject: str, body: str, cc: str = "", bcc: str = "",
                    reply_to_message_id: str | None = None) -> str:
        """Create a plain-text draft (not sent). With reply_to_message_id it is threaded as a reply and an empty subject becomes 'Re: <original>'. Returns draft_id for gmail_send_draft."""
        return _run(_draft, to, subject, body, cc, bcc, reply_to_message_id)

    @mcp.tool()
    def gmail_send(to: str, subject: str, body: str, cc: str = "", bcc: str = "",
                   reply_to_message_id: str | None = None) -> str:
        """Send a plain-text email immediately. Same threading rules as gmail_draft. Irreversible."""
        return _run(_send, to, subject, body, cc, bcc, reply_to_message_id)

    @mcp.tool()
    def gmail_send_draft(draft_id: str) -> str:
        """Send an existing draft by draft_id. Irreversible."""
        return _run(_send_draft, draft_id)

    @mcp.tool()
    def gmail_trash(message_id: str) -> str:
        """Move a message to Trash (Gmail purges it after 30 days)."""
        return _run(_trash, message_id)

    @mcp.tool()
    def gmail_attachment(message_id: str, attachment_id: str) -> str:
        """Download one attachment (<=5 MiB) as base64. Get attachment_id from gmail_get."""
        return _run(_attachment, message_id, attachment_id)

    return mcp


# ---- selftest ---------------------------------------------------------------

def _selftest():
    def enc(s):
        return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")

    payload = {
        "mimeType": "multipart/mixed",
        "headers": [{"name": "From", "value": "a@x"}, {"name": "Subject", "value": "hi"},
                    {"name": "Message-ID", "value": "<m1@x>"}, {"name": "X-Ignored", "value": "z"}],
        "parts": [
            {"mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/plain", "body": {"data": enc("plain body")}},
                {"mimeType": "text/html", "body": {"data": enc("<p>html <b>body</b></p>")}},
            ]},
            {"mimeType": "application/pdf", "filename": "a.pdf", "body": {"attachmentId": "att1", "size": 12}},
        ],
    }
    assert _body_text(payload) == "plain body", _body_text(payload)
    assert _headers({"headers": [{"name": "Message-Id", "value": "<p@x>"}, {"name": "CC", "value": "c@x"}]}) == \
        {"message-id": "<p@x>", "cc": "c@x"}
    assert _headers(payload) == {"from": "a@x", "subject": "hi", "message-id": "<m1@x>"}, _headers(payload)
    assert _attachments(payload) == [{"filename": "a.pdf", "mime_type": "application/pdf", "size": 12,
                                      "attachment_id": "att1"}], _attachments(payload)

    html_only = {"mimeType": "text/html", "body": {"data": enc(
        "<html><head><style>x{}</style></head><body><p>One</p><div>Two &amp; three</div><script>bad()</script></body></html>")}}
    assert _body_text(html_only) == "One\n\nTwo & three", repr(_body_text(html_only))

    msg = {"id": "m", "threadId": "t", "labelIds": ["INBOX", "UNREAD"], "snippet": "s", "payload": payload}
    full = _full(msg, max_body_chars=5)
    assert full["unread"] and full["body"] == "plain" and full["body_truncated"], full
    assert full["message_id_header"] == "<m1@x>", full

    raw = _build_mime("b@y", "Re: hi", "reply text\n", cc="c@y", in_reply_to="<m1@x>", references="<m0@x>")
    from email import message_from_bytes, policy
    parsed = message_from_bytes(_b64url_decode(raw), policy=policy.default)
    assert parsed["To"] == "b@y" and parsed["Cc"] == "c@y" and parsed["Subject"] == "Re: hi", parsed.items()
    assert parsed["In-Reply-To"] == "<m1@x>" and parsed["References"] == "<m0@x> <m1@x>", parsed.items()
    assert parsed.get_content().strip() == "reply text", parsed.get_content()
    assert "Bcc" not in parsed
    try:
        _build_mime("", "s", "b")
        raise AssertionError("empty 'to' must raise")
    except ValueError:
        pass

    by_name, by_id = {"inbox": "INBOX", "receipts": "Label_7"}, {"INBOX": "INBOX", "Label_7": "Receipts"}
    assert _resolve_labels(["Inbox", "Label_7", "receipts"], by_name, by_id) == ["INBOX", "Label_7", "Label_7"]
    try:
        _resolve_labels(["nope"], by_name, by_id)
        raise AssertionError("unknown label must raise")
    except ValueError:
        pass

    # Warm cache so _modify rejects before any API call.
    _labels_cache.update(at=time.time(), by_name={"trash": "TRASH", "spam": "SPAM"},
                         by_id={"TRASH": "TRASH", "SPAM": "SPAM"})
    for forbidden in ("TRASH", "spam"):
        try:
            _modify(["m1"], [forbidden], [])
            raise AssertionError(f"gmail_modify must refuse {forbidden}")
        except ValueError as e:
            assert "gmail_trash" in str(e), e
    _labels_cache["at"] = 0.0
    print("gmail selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        server().run(transport="stdio")
