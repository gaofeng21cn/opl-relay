from __future__ import annotations

import hashlib
import re
from email import policy
from email.parser import BytesParser

from .config import load_accounts_config
from .message import extract_text_body
from .server_drafts import _message_date, _subject_key, addresses, content_snapshot, reply_route
from .store import connect_email_store, fetch_raw_email_by_storage_ref, get_message_by_storage_ref, list_messages


def _thread_ids(message):
    return set(re.findall(r"<[^<>\s]+@[^<>\s]+>", " ".join(
        str(message.get(field, "")) for field in ("Message-ID", "In-Reply-To", "References"))))


def reply_context(*, config_path, db_path, account_id, storage_ref=None, query="", latest_sent=False):
    accounts = load_accounts_config(config_path)
    account = accounts[account_id]
    conn = connect_email_store(db_path)
    try:
        def read_verified(reference):
            meta = get_message_by_storage_ref(conn, reference)
            raw = fetch_raw_email_by_storage_ref(conn, reference)
            if not meta or meta["account_id"] != account_id or not raw:
                raise ValueError("Reply source does not belong to the selected account")
            if hashlib.sha256(raw).hexdigest() != meta["raw_sha256"]:
                raise ValueError("Reply source evidence hash mismatch")
            return meta, raw, BytesParser(policy=policy.default).parsebytes(raw)

        if not storage_ref:
            if not query.strip():
                raise ValueError("Provide --storage-ref or --query")
            rows = list_messages(conn, account_ids=[account_id], query=query.strip(),
                                 limit=500, scope="active")
            if len(rows) >= 500:
                raise ValueError("Reply search reached its result limit; narrow the query or provide --storage-ref")
            if not rows:
                raise ValueError("No matching active messages")
            sent_folders = {row[0] for row in conn.execute(
                "SELECT folder FROM mail_folders WHERE account_id=? AND role='sent'", (account_id,))}
            sent_folders.update({"Sent Items", "Sent", "Sent Mail", "Sent Messages", "[Gmail]/Sent Mail"})
            matched = []
            choices = []
            for item in rows:
                candidate_meta, candidate_raw, candidate = read_verified(item["storage_ref"])
                matched.append((item, candidate_meta, candidate_raw, candidate))
            for item, candidate_meta, candidate_raw, candidate in matched:
                if latest_sent:
                    senders = addresses(candidate, "From")
                    if (len(senders) == 1 and senders[0].addr_spec.casefold() == account.email.casefold()
                            and item["folder"] in sent_folders and _message_date(candidate)):
                        own_id = str(candidate.get("Message-ID", ""))
                        other_ids = {
                            str(other.get("Message-ID", ""))
                            for _, _, _, other in matched
                            if str(other.get("Message-ID", "")) != own_id
                        }
                        if (_thread_ids(candidate) - {own_id}).intersection(other_ids):
                            choices.append((candidate_meta, candidate_raw, candidate))
                else:
                    choices.append((candidate_meta, candidate_raw, candidate))
            if not choices:
                raise ValueError("No matching sent messages from the selected account")
            if not latest_sent and len(choices) != 1:
                raise ValueError("Query matches multiple messages; provide --storage-ref or --latest-sent")
            newest = max(choices, key=lambda item: (
                _message_date(item[2]).timestamp() if _message_date(item[2]) else float("-inf")))
            newest_date = _message_date(newest[2])
            if sum(1 for item in choices if _message_date(item[2]) == newest_date) > 1:
                raise ValueError("Query matches multiple equally recent messages; provide --storage-ref")
            storage_ref = newest[0]["storage_ref"]
        meta, raw, message = read_verified(storage_ref)
        candidates = []
        complete = True
        selected_latest = False
        if latest_sent:
            subject = _subject_key(str(message.get("Subject", "")))
            if not subject or not _thread_ids(message):
                raise ValueError("A valid source subject and thread identity are required")
            rows = list_messages(conn, account_ids=[account_id], query=subject, limit=500, scope="active")
            complete = len(rows) < 500
            if not complete:
                raise ValueError("Thread history reached its result limit; synchronize or narrow the search")
            original_ids = _thread_ids(message)
            sent_folders = {row[0] for row in conn.execute(
                "SELECT folder FROM mail_folders WHERE account_id=? AND role='sent'", (account_id,))}
            sent_folders.update({"Sent Items", "Sent", "Sent Mail", "Sent Messages", "[Gmail]/Sent Mail"})
            for item in rows:
                if item["folder"] not in sent_folders:
                    continue
                candidate_meta, candidate_raw, candidate = read_verified(item["storage_ref"])
                senders = addresses(candidate, "From")
                if (len(senders) != 1 or senders[0].addr_spec.casefold() != account.email.casefold()
                        or _subject_key(str(candidate.get("Subject", ""))) != subject
                        or not original_ids.intersection(_thread_ids(candidate))):
                    continue
                if not _message_date(candidate):
                    continue
                candidates.append((candidate_meta, candidate_raw, candidate))
            if candidates:
                newest = max(candidates, key=lambda item: _message_date(item[2]))
                newest_ids = {str(item[2].get("Message-ID", "")) for item in candidates
                              if _message_date(item[2]) == _message_date(newest[2])}
                if len(newest_ids) > 1:
                    raise ValueError("Latest sent reply is ambiguous; select an exact source")
                if _message_date(message) is None or _message_date(newest[2]) >= _message_date(message):
                    meta, raw, message = newest
                    selected_latest = True
        to, cc = reply_route(message, [item.email for item in accounts.values()])
        return {"account_id": account_id, "requested_storage_ref": storage_ref,
                "storage_ref": meta["storage_ref"], "source_selection": "latest_own_sent" if selected_latest else "exact",
                "coverage": {"scope": "active", "complete": complete, "freshness": "last synchronized cache"},
                "headers": content_snapshot(raw)["headers"], "body_text": extract_text_body(raw),
                "reply_to": [str(item) for item in to], "reply_cc": [str(item) for item in cc],
                "in_reply_to": str(message.get("Message-ID", "")),
                "candidates": [{"storage_ref": item[0]["storage_ref"], "date": item[0]["date"],
                                "message_id": str(item[2].get("Message-ID", ""))} for item in candidates]}
    finally:
        conn.close()
