"""IMAP-only review drafts. This module has no send or existing-draft edit path."""
from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import re
import sqlite3
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid
from html.parser import HTMLParser
from pathlib import Path

from .config import keychain_get_secret, load_accounts_config
from .drafts import normalize_body
from .mailbox import _list_mailboxes, _quoted_mailbox
from .store import connect_email_store, fetch_raw_email_by_storage_ref, get_message_by_storage_ref
from .sync import connect_imap, parse_fetch_uid_rfc822_map


class QuoteText(HTMLParser):
    """Retain readable quoted content, never execute or copy active HTML."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.suppressed = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.suppressed += 1
        elif not self.suppressed and tag in {"br", "p", "div", "li", "tr", "blockquote"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.suppressed = max(0, self.suppressed - 1)
        elif not self.suppressed and tag in {"p", "div", "li", "tr", "blockquote"}:
            self.parts.append("\n")
        elif not self.suppressed and tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)


def addresses(message, field):
    result = []
    for header in message.get_all(field, []):
        if header.defects:
            raise ValueError(f"Invalid {field} address header; clarify recipients")
        for addr in header.addresses:
            if not addr.username or not addr.domain or not addr.addr_spec.isascii():
                raise ValueError(f"Unsupported {field} address; clarify recipients")
            result.append(addr)
    return result


def reply_route(source, self_addresses):
    own = {a.casefold() for a in self_addresses}
    senders = addresses(source, "From")
    if len(senders) != 1:
        raise ValueError("Reply source must have exactly one sender")
    sent_by_self = senders[0].addr_spec.casefold() in own
    if source.get("Bcc"):
        raise ValueError("Source contains Bcc; clarify private recipient routing")
    seen = set(own)

    def unique(items):
        kept = []
        for addr in items:
            key = addr.addr_spec.casefold()
            if key not in seen:
                seen.add(key)
                kept.append(addr)
        return kept

    primary = [] if sent_by_self else (addresses(source, "Reply-To") or senders)
    to = unique(primary + addresses(source, "To"))
    cc = unique(addresses(source, "Cc"))
    if not to and cc:
        to, cc = cc[:1], cc[1:]
    if not to:
        raise ValueError("Reply All has no external recipients after excluding own addresses")
    return to, cc


def quoted_text(source):
    body = source.get_body(preferencelist=("plain", "html"))
    if body is None:
        raise ValueError("Source has no readable body; cannot preserve reply context")
    text = body.get_content()
    if body.get_content_type() == "text/html":
        parser = QuoteText()
        parser.feed(text)
        text = "".join(parser.parts)
    text = normalize_body(text).strip()
    if not text:
        raise ValueError("Source body is empty; inspect before replying")
    headers = [f"{key}: {source[key]}" for key in ("From", "Date", "To", "Cc", "Subject") if source[key]]
    return "\n".join(headers) + "\n\n" + text


def _paragraphs(text):
    return "".join('<p style="margin:0 0 1em">' + html.escape(p).replace("\n", "<br>\n") + "</p>"
                   for p in text.split("\n\n"))


def build_message(*, sender, self_addresses, body, signature, source_raw=None,
                  to=(), cc=(), subject="", attachments=()):
    body, signature = normalize_body(body).strip(), normalize_body(signature).strip()
    if not body or not signature:
        raise ValueError("Both reply text and the approved signature are required")
    if signature in body:
        raise ValueError("Body already contains the signature; supply new text only")
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = sender
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="draft.opl.local")
    quote = ""
    if source_raw is not None:
        source = BytesParser(policy=policy.default).parsebytes(source_raw)
        to, cc = reply_route(source, self_addresses)
        ids = source.get_all("Message-ID", [])
        if len(ids) != 1 or not re.fullmatch(r"<[^<>\s]+@[^<>\s]+>", str(ids[0]).strip()):
            raise ValueError("Source needs one valid Message-ID for a threaded reply")
        parent_id = str(ids[0]).strip()
        refs = re.findall(r"<[^<>\s]+@[^<>\s]+>", str(source.get("References", source.get("In-Reply-To", ""))))
        msg["In-Reply-To"] = parent_id
        msg["References"] = " ".join(dict.fromkeys([*refs, parent_id]))
        subject = str(source.get("Subject", ""))
        if not re.match(r"(?i)^re\s*:", subject):
            subject = "Re: " + subject
        quote = quoted_text(source)
    else:
        probe = EmailMessage(policy=policy.default)
        probe["To"] = ", ".join(to)
        probe["Cc"] = ", ".join(cc)
        seen = set()
        groups = []
        for field in ("To", "Cc"):
            group = []
            for addr in addresses(probe, field):
                if addr.addr_spec.casefold() not in seen:
                    seen.add(addr.addr_spec.casefold())
                    group.append(addr)
            groups.append(group)
        to, cc = groups
    if not to or not subject.strip():
        raise ValueError("Recipients and subject are required")
    msg["To"] = tuple(to)
    if cc:
        msg["Cc"] = tuple(cc)
    msg["Subject"] = subject
    new_text = body + "\n\n" + signature
    plain = new_text
    rich = '<html><body><div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.5">' + _paragraphs(new_text)
    if quote:
        plain += "\n\n" + "\n".join("> " + line if line else ">" for line in quote.splitlines())
        rich += '<blockquote style="margin:1em 0 0;padding-left:1em;border-left:2px solid #ccc">' + _paragraphs(quote) + '</blockquote>'
    rich += "</div></body></html>"
    msg.set_content(plain, charset="utf-8")
    msg.add_alternative(rich, subtype="html", charset="utf-8")
    for path in attachments:
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        major, minor = kind.split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=major, subtype=minor, filename=path.name)
    return msg.as_bytes()


def content_snapshot(raw):
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    parts = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_maintype() == "text":
            content = part.get_content().replace("\r\n", "\n").encode("utf-8")
        else:
            content = part.get_payload(decode=True) or b""
        parts.append({"type": part.get_content_type(), "name": part.get_filename(),
                      "disposition": part.get_content_disposition(),
                      "content_id": str(part.get("Content-ID", "")),
                      "sha256": hashlib.sha256(content).hexdigest()})
    return {
        "headers": {k: str(msg.get(k, "")) for k in
                    ("From", "To", "Cc", "Bcc", "Subject", "Message-ID", "In-Reply-To", "References")},
        "parts": parts,
    }


def resolve_drafts(client):
    boxes = _list_mailboxes(client)
    matches = [b.name for b in boxes if "\\drafts" in b.flags and "\\noselect" not in b.flags]
    if not matches:
        matches = [b.name for b in boxes if b.name.casefold() in {"drafts", "draft", "[gmail]/drafts"} and "\\noselect" not in b.flags]
    if len(matches) != 1:
        raise ValueError("Server Drafts folder is missing or ambiguous; no folder created")
    return matches[0]


def inspect_message(client, folder, expected):
    typ, _ = client.select(_quoted_mailbox(folder), readonly=True)
    if typ != "OK":
        raise ValueError("Cannot inspect server Drafts")
    message_id = str(BytesParser(policy=policy.default).parsebytes(expected)["Message-ID"])
    typ, data = client.uid("search", None, "HEADER", "Message-ID", '"' + message_id + '"')
    if typ != "OK":
        raise ValueError("Cannot search server draft identity")
    matches = []
    for uid in b" ".join(x for x in data if isinstance(x, bytes)).split():
        typ, fetched = client.uid("fetch", uid.decode(), "(UID FLAGS BODY.PEEK[])")
        if typ != "OK":
            raise ValueError("Cannot reread server draft")
        raw = parse_fetch_uid_rfc822_map(fetched).get(int(uid))
        if not raw:
            raise ValueError("Draft fetch returned no message")
        actual = BytesParser(policy=policy.default).parsebytes(raw)
        if str(actual.get("Message-ID", "")) != message_id:
            continue
        flags = b" ".join(x[0] if isinstance(x, tuple) else x for x in fetched if isinstance(x, (tuple, bytes)))
        match = re.search(rb"FLAGS\s+\(([^)]*)\)", flags, re.I)
        tokens = set(match[1].lower().split()) if match else set()
        if b"\\draft" not in tokens or b"\\deleted" in tokens:
            raise ValueError("Message is not a live server draft")
        if content_snapshot(raw) != content_snapshot(expected):
            raise ValueError("Server draft changed; preserve mobile edits and do not overwrite")
        matches.append({"uid": int(uid), "folder": folder, "message_id": message_id,
                        "to": str(actual["To"]), "cc": str(actual.get("Cc", "")),
                        "subject": str(actual["Subject"]), "server_verified": True,
                        "state": "draft", "send_allowed": False})
    if len(matches) > 1:
        raise ValueError("Multiple drafts match this request; reconcile manually")
    return matches[0] if matches else None


def _ledger(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS server_draft_requests (
        request_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, input_hash TEXT NOT NULL,
        raw BLOB NOT NULL, state TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '')""")
    conn.commit()
    return conn


def server_draft(*, config_path, db_path, ledger_path, account_id, request_id,
                 body="", signature="", source_ref=None, to=(), cc=(), subject="",
                 attachments=(), apply=False, inspect=False, self_aliases=()):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", request_id):
        raise ValueError("Use a stable, non-sensitive request ID (letters, digits, . _ -)")
    accounts = load_accounts_config(config_path)
    account = accounts[account_id]
    conn = _ledger(ledger_path)
    client = None
    try:
        conn.execute("BEGIN IMMEDIATE")  # single writer, including APPEND uncertainty
        row = conn.execute("SELECT * FROM server_draft_requests WHERE request_id=?", (request_id,)).fetchone()
        if row and row["account_id"] != account_id:
            raise ValueError("Request ID belongs to a different account")
        if inspect:
            if not row:
                raise ValueError("Unknown draft request")
            raw = bytes(row["raw"])
        else:
            source_raw = None
            if source_ref:
                store = connect_email_store(db_path)
                try:
                    meta = get_message_by_storage_ref(store, source_ref)
                    source_raw = fetch_raw_email_by_storage_ref(store, source_ref)
                finally:
                    store.close()
                if not meta or meta["account_id"] != account_id or not source_raw:
                    raise ValueError("Reply source does not belong to the selected account")
                if hashlib.sha256(source_raw).hexdigest() != meta["raw_sha256"]:
                    raise ValueError("Reply source evidence hash mismatch")
            attachment_paths = [Path(p).expanduser() for p in attachments]
            raw = build_message(sender=account.email,
                                self_addresses=[a.email for a in accounts.values()] + list(self_aliases),
                                body=body, signature=signature, source_raw=source_raw,
                                to=to, cc=cc, subject=subject, attachments=attachment_paths)
            snapshot = content_snapshot(raw)
            snapshot["headers"].pop("Message-ID")
            identity = {"content": snapshot, "source_ref": source_ref}
            digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            if row:
                if row["input_hash"] != digest:
                    raise ValueError("Request ID already binds different content; do not replace a mobile draft")
                raw = bytes(row["raw"])
            else:
                conn.execute("INSERT INTO server_draft_requests VALUES (?,?,?,?,?,?)",
                             (request_id, account_id, digest, raw, "prepared", ""))
                row = conn.execute("SELECT * FROM server_draft_requests WHERE request_id=?", (request_id,)).fetchone()
        if not apply and not inspect:
            conn.commit()
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            return {"state": "prepared", "request_id": request_id, "server_verified": False,
                    "send_allowed": False, "headers": content_snapshot(raw)["headers"],
                    "body_text": msg.get_body(preferencelist=("plain",)).get_content(),
                    "body_html": msg.get_body(preferencelist=("html",)).get_content()}
        client = connect_imap(account, timeout_sec=20)
        client.login(account.imap.username, keychain_get_secret(account.imap.credential_ref))
        folder = row["folder"] or resolve_drafts(client)
        found = inspect_message(client, folder, raw)
        if found:
            conn.execute("UPDATE server_draft_requests SET state='verified',folder=? WHERE request_id=?", (folder, request_id))
            conn.commit()
            return {**found, "request_id": request_id}
        if inspect or row["state"] != "prepared":
            raise ValueError("Draft absent or APPEND result unknown; do not recreate or resend")
        conn.execute("UPDATE server_draft_requests SET state='attempted',folder=? WHERE request_id=?", (folder, request_id))
        conn.commit()  # durable BEFORE network mutation; interruption can never trigger another APPEND
        conn.execute("BEGIN IMMEDIATE")
        # A concurrent caller can inspect while waiting; only this prepared owner appends.
        typ, _ = client.append(_quoted_mailbox(folder), "(\\Draft)", None, raw)
        if typ != "OK":
            raise ValueError("Server rejected APPEND; inspect this request before creating another")
        found = inspect_message(client, folder, raw)
        if not found:
            raise ValueError("APPEND returned OK but draft readback is absent; result unconfirmed")
        conn.execute("UPDATE server_draft_requests SET state='verified' WHERE request_id=?", (request_id,))
        conn.commit()
        return {**found, "request_id": request_id}
    finally:
        conn.close()
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass
