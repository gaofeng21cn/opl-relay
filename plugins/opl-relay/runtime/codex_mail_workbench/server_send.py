from __future__ import annotations

from email import policy
from email.parser import BytesParser
from pathlib import Path

from .apple_mail import AppleMailProvider
from .config import load_account
from .drafts import (
    ApprovalMismatch, DraftAlreadyClaimed, DraftLedger, DraftService,
    approval_fingerprint,
)
from .server_drafts import (
    _ledger, reconcile_server_drafts, server_draft, server_fingerprint,
)


def _schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS server_draft_handoffs (
        request_id TEXT PRIMARY KEY, draft_ref TEXT NOT NULL)""")


def inspect_delivery(*, conn, ledger_path, account, request_id, raw):
    _schema(conn)
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


def send_server_draft(*, config_path, ledger_path, account_id, request_id, approval, check=False):
    account = load_account(config_path, account_id)
    conn = _ledger(ledger_path)
    try:
        _schema(conn)
        conn.commit()
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
