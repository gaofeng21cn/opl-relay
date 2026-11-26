from __future__ import annotations

import json
import time
from email import policy
from email.parser import BytesParser
from pathlib import Path

from .apple_mail import AppleMailProvider
from .config import load_account
from .drafts import (
    ApprovalMismatch, DraftAlreadyClaimed, DraftLedger, DraftService,
    approval_fingerprint,
)
from .native import Himalaya, NativeError
from .schema import ensure_drafts_schema
from .server_drafts import (
    _ledger, _search_message, addresses, content_snapshot, inspect_message,
    reconcile_server_drafts, resolve_sent, server_draft, server_fingerprint,
)


def _schema(conn):
    ensure_drafts_schema(conn)


def _imap_sent_receipt(client, folder, raw):
    expected = content_snapshot(raw)
    message_id = expected["headers"]["Message-ID"]
    sent = _search_message(client, folder, message_id)
    if sent is None:
        return None
    if sent["deleted"] or sent["draft"] or content_snapshot(sent["raw"]) != expected:
        raise ValueError("Sent copy does not match the approved draft")
    return {"provider": "himalaya", "mailbox": folder, "uid": sent["uid"],
            "uidvalidity": sent["uidvalidity"], "message_id": message_id,
            "subject": expected["headers"]["Subject"]}


def _imap_delivery_result(conn, request_id, raw, delivery, *, client=None, error=None):
    receipt = json.loads(delivery["sent_receipt"]) if delivery["sent_receipt"] else None
    if not receipt and client is not None:
        try:
            receipt = _imap_sent_receipt(client, delivery["sent_folder"], raw)
        except (ValueError, RuntimeError, OSError) as exc:
            error = getattr(exc, "code", "sent_readback_failed")
    if receipt:
        conn.execute("UPDATE server_draft_smtp_deliveries SET sent_receipt=? WHERE request_id=?",
                     (json.dumps(receipt), request_id))
        conn.execute("UPDATE server_draft_requests SET state='sent' WHERE request_id=? AND state='sending'",
                     (request_id,))
        conn.commit()
        return {"request_id": request_id, "state": "sent", "server_verified": True,
                "send_allowed": False, "next_action": "complete", "sent_receipt": receipt}
    return {"request_id": request_id, "state": "unknown", "server_verified": False,
            "send_allowed": False, "next_action": "server-inspect",
            "message_id": content_snapshot(raw)["headers"]["Message-ID"],
            "smtp_accepted": bool(delivery["smtp_accepted"]),
            "reason": error or ("sent_copy_unverified" if delivery["smtp_accepted"] else "submission_unverified")}


def inspect_delivery(*, conn, ledger_path, account, request_id, raw):
    _schema(conn)
    delivery = conn.execute("SELECT * FROM server_draft_smtp_deliveries WHERE request_id=?",
                            (request_id,)).fetchone()
    if delivery:
        if delivery["sent_receipt"]:
            return _imap_delivery_result(conn, request_id, raw, delivery)
        try:
            client = Himalaya(account)
            return _imap_delivery_result(conn, request_id, raw, delivery, client=client)
        except (ValueError, RuntimeError, OSError) as exc:
            return _imap_delivery_result(conn, request_id, raw, delivery,
                                         error=getattr(exc, "code", "sent_readback_failed"))
    binding = conn.execute("SELECT draft_ref FROM server_draft_handoffs WHERE request_id=?",
                           (request_id,)).fetchone()
    if not binding:
        return None
    service = DraftService(DraftLedger(Path(ledger_path)), AppleMailProvider())
    record = service.ledger.get(binding["draft_ref"])
    if record["account_id"] != account.account_id:
        raise ValueError("Draft handoff belongs to another account")
    if record["state"] == "draft":
        return None
    if not account.graph:
        # Old Apple Mail handoffs remain single-use. Reconcile them against
        # the original Message-ID even if their old compose buffer vanished.
        legacy = {"smtp_accepted": False, "sent_receipt": None}
        try:
            client = Himalaya(account)
            receipt = _imap_sent_receipt(client, resolve_sent(client), raw)
        except (ValueError, RuntimeError, OSError):
            receipt = None
        if receipt:
            return {"request_id": request_id, "state": "sent", "server_verified": True,
                    "send_allowed": False, "next_action": "complete", "sent_receipt": receipt}
        return _imap_delivery_result(conn, request_id, raw, legacy, error="legacy_handoff_unverified")
    result = service.inspect(binding["draft_ref"])
    return {**result, "request_id": request_id, "send_allowed": False,
            "next_action": "complete" if result["state"] == "sent" else "server-inspect"}


def _delivery_receipt(*, config_path, ledger_path, account_id, request_id, message_id):
    try:
        result = reconcile_server_drafts(config_path=config_path, ledger_path=ledger_path,
                                         account_id=account_id, request_id=request_id,
                                         sent_message_id=message_id, apply=True)
        if not result["drafts"]:
            return {"state": "sent"}
        entry = result["drafts"][0]
        return {"state": "sent", "draft_cleanup": entry["action"]}
    except Exception:
        return {"state": "sent", "draft_cleanup": "pending"}


def _send_imap_draft(conn, *, config_path, ledger_path, account, request_id, approval, check):
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT * FROM server_draft_requests WHERE request_id=? AND account_id=?",
                       (request_id, account.account_id)).fetchone()
    if not row or row["state"] != "verified":
        raise DraftAlreadyClaimed("Only a verified pending server draft can be sent; use server-inspect")
    if conn.execute("SELECT 1 FROM server_draft_handoffs WHERE request_id=?", (request_id,)).fetchone():
        raise DraftAlreadyClaimed("This request already has an Apple Mail handoff; use server-inspect")
    raw = bytes(row["raw"])
    if not approval or approval != server_fingerprint(raw):
        raise ApprovalMismatch("Approval does not match this server draft; inspect and review its content")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    senders = [addr.addr_spec for addr in addresses(message, "From")]
    recipients = list(dict.fromkeys(addr.addr_spec for field in ("To", "Cc")
                                    for addr in addresses(message, field)))
    if len(senders) != 1 or senders[0].casefold() != account.email.casefold() or not recipients or message.get("Bcc"):
        raise ValueError("Approved draft must use the selected sender, visible recipients and no Bcc")

    client = Himalaya(account)
    sent_folder = resolve_sent(client)
    if _imap_sent_receipt(client, sent_folder, raw):
        raise DraftAlreadyClaimed("Server Sent already has the approved Message-ID; use server-inspect")
    client.check_smtp()
    current = inspect_message(client, row["folder"], raw)
    if current is None:
        raise ValueError("Server draft is absent; use server-inspect")
    if check:
        return {"request_id": request_id, "state": "draft", "delivery_ready": True,
                "send_allowed": False, "approval_fingerprint": approval,
                "provider": "himalaya-smtp", "sent_mailbox": sent_folder,
                "subject": current["subject"], "sent_messages": 0}

    conn.execute("INSERT INTO server_draft_smtp_deliveries(request_id,sent_folder) VALUES (?,?)",
                 (request_id, sent_folder))
    conn.execute("UPDATE server_draft_requests SET state='sending' WHERE request_id=?", (request_id,))
    conn.commit()
    error = None
    accepted = False
    try:
        client.submit_draft(raw, sender=senders[0], recipients=recipients)
        accepted = True
    except (OSError, RuntimeError, NativeError) as exc:
        error = getattr(exc, "code", "submission_unverified")

    if accepted:
        conn.execute("UPDATE server_draft_smtp_deliveries SET smtp_accepted=1 WHERE request_id=?", (request_id,))
        conn.commit()
        try:
            if not _imap_sent_receipt(client, sent_folder, raw):
                client.append_sent(raw, folder=sent_folder)
        except (ValueError, RuntimeError, OSError) as exc:
            error = getattr(exc, "code", "sent_copy_unverified")

    delivery = conn.execute("SELECT * FROM server_draft_smtp_deliveries WHERE request_id=?",
                            (request_id,)).fetchone()
    for attempt in range(3 if accepted else 1):
        if attempt:
            time.sleep(1)
        result = _imap_delivery_result(conn, request_id, raw, delivery, client=client, error=error)
        if result["state"] == "sent":
            cleanup = _delivery_receipt(config_path=config_path, ledger_path=ledger_path,
                                        account_id=account.account_id, request_id=request_id,
                                        message_id=result["sent_receipt"]["message_id"])
            return {**result, "smtp_accepted": accepted, "approval_fingerprint": approval,
                    "draft_cleanup": cleanup.get("draft_cleanup", "pending")}
    return result


def send_server_draft(*, config_path, ledger_path, account_id, request_id, approval, check=False):
    account = load_account(config_path, account_id)
    conn = _ledger(ledger_path)
    try:
        _schema(conn)
        conn.commit()
        if not account.graph:
            return _send_imap_draft(conn, config_path=config_path, ledger_path=ledger_path,
                                    account=account, request_id=request_id, approval=approval, check=check)
        row = conn.execute("SELECT * FROM server_draft_requests WHERE request_id=? AND account_id=?",
                           (request_id, account_id)).fetchone()
        if not row or row["state"] != "verified":
            raise DraftAlreadyClaimed("Only a verified pending server draft can be sent; use server-inspect")
        raw = bytes(row["raw"])
        if not approval or approval != server_fingerprint(raw):
            raise ApprovalMismatch("Approval does not match this server draft; inspect and review its content")
        provider = AppleMailProvider()
        service = DraftService(DraftLedger(ledger_path), provider)
        binding = conn.execute("SELECT draft_ref FROM server_draft_handoffs WHERE request_id=?",
                               (request_id,)).fetchone()
        message_id = str(BytesParser(policy=policy.default).parsebytes(raw)["Message-ID"])
        if binding:
            record = service.ledger.get(binding["draft_ref"])
            if record["state"] != "draft":
                raise DraftAlreadyClaimed("This request already claimed sending; use server-inspect, never resend")
        current = server_draft(config_path=config_path, db_path=None, ledger_path=ledger_path,
                               account_id=account_id, request_id=request_id, inspect=True)
        if current["state"] != "draft":
            raise DraftAlreadyClaimed("Server draft is no longer pending")
        snapshot = provider.inspect_server_draft(sender=account.email, message_id=message_id, expected_raw=raw)
        if check:
            return {"request_id": request_id, "state": "draft", "delivery_ready": True,
                    "send_allowed": False, "approval_fingerprint": approval,
                    "provider": "apple-mail", "provider_uuid": snapshot.provider_uuid,
                    "subject": snapshot.subject, "sent_messages": 0}
        record = service.ledger.register(account_id=account_id, snapshot=snapshot,
                                         event_type="server_draft_adopted",
                                         event_detail={"request_id": request_id})
        if binding and binding["draft_ref"] != record["draft_ref"]:
            raise ValueError("Apple Mail draft identity changed; do not send")
        conn.execute("INSERT OR IGNORE INTO server_draft_handoffs VALUES (?,?)",
                     (request_id, record["draft_ref"]))
        conn.commit()
        result = service.send(record["draft_ref"], approval=approval_fingerprint(account_id, snapshot))
        receipt = _delivery_receipt(config_path=config_path, ledger_path=ledger_path,
                                    account_id=account_id, request_id=request_id,
                                    message_id=result["sent_receipt"]["message_id"])
        return {**result, **receipt, "request_id": request_id, "send_allowed": False,
                "draft_cleanup": receipt.get("draft_cleanup", "pending")}
    finally:
        conn.close()
