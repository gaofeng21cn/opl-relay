from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlparse


class DraftError(RuntimeError):
    pass


class DraftNotFound(DraftError):
    pass


class ApprovalMismatch(DraftError):
    pass


class DraftAlreadyClaimed(DraftError):
    pass


class SendStatusUnknown(DraftError):
    pass


@dataclass(frozen=True)
class Recipient:
    address: str
    name: str = ""


@dataclass(frozen=True)
class Attachment:
    name: str
    mime_type: str
    size: int
    content_sha256: str = ""
    provider_id: str = ""


@dataclass(frozen=True)
class DraftSnapshot:
    provider_account: str
    provider_uuid: str
    provider_message_id: str
    sender: str
    to: list[Recipient]
    cc: list[Recipient]
    bcc: list[Recipient]
    subject: str
    body_text: str
    attachments: list[Attachment] = field(default_factory=list)
    provider_guard: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class SendStart:
    provider_message_id: str
    provider_uuid: str


class DraftProvider(Protocol):
    def create(
        self,
        *,
        sender: str,
        to: list[Recipient],
        cc: list[Recipient],
        bcc: list[Recipient],
        subject: str,
        body_text: str,
        attachments: list[Path],
        visible: bool,
    ) -> DraftSnapshot: ...

    def inspect(self, *, provider_account: str, provider_uuid: str) -> DraftSnapshot: ...

    def open(self, *, provider_account: str, provider_uuid: str) -> DraftSnapshot: ...

    def send(self, snapshot: DraftSnapshot) -> SendStart: ...

    def find_sent(
        self,
        *,
        provider_account: str,
        provider_uuid: str,
        provider_message_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any] | None: ...

    def discard(self, *, provider_account: str, provider_uuid: str) -> bool: ...


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def normalize_body(value: str) -> str:
    normalized = normalize_text(
        value.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\u2028", "\n")
        .replace("\u2029", "\n")
    )
    normalized = "\n".join(
        line.rstrip(" \t") for line in normalized.split("\n")
    )
    return re.sub(r"\n{3,}", "\n\n", normalized)


def prepare_body(value: str) -> str:
    body = normalize_body(value).lstrip("\n")
    if not body.strip():
        raise DraftError("正文不能为空")
    return body


def _recipient_payload(recipient: Recipient) -> dict[str, str]:
    return {
        "address": normalize_text(recipient.address).strip().casefold(),
        "name": normalize_text(recipient.name).strip(),
    }


def approval_payload(account_id: str, snapshot: DraftSnapshot) -> dict[str, Any]:
    attachments = [
        {
            "name": normalize_text(item.name),
            "mime_type": item.mime_type.casefold(),
            "size": int(item.size),
            "content_sha256": item.content_sha256.casefold(),
        }
        for item in snapshot.attachments
    ]
    attachments.sort(
        key=lambda item: (
            item["name"],
            item["mime_type"],
            item["size"],
            item["content_sha256"],
        )
    )
    return {
        "account_id": account_id,
        "provider_account": normalize_text(snapshot.provider_account),
        "sender": snapshot.sender.strip().casefold(),
        "to": [_recipient_payload(item) for item in snapshot.to],
        "cc": [_recipient_payload(item) for item in snapshot.cc],
        "bcc": [_recipient_payload(item) for item in snapshot.bcc],
        "subject": normalize_text(snapshot.subject),
        "body_text": normalize_body(snapshot.body_text),
        "attachments": attachments,
    }


def approval_fingerprint(account_id: str, snapshot: DraftSnapshot) -> str:
    encoded = json.dumps(
        approval_payload(account_id, snapshot),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def make_draft_ref(account_id: str, provider_uuid: str) -> str:
    return (
        "mail-draft://apple-mail/"
        + quote(account_id, safe="")
        + "/"
        + quote(provider_uuid, safe="")
    )


def parse_draft_ref(draft_ref: str) -> tuple[str, str]:
    parsed = urlparse(draft_ref)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if parsed.scheme != "mail-draft" or parsed.netloc != "apple-mail" or len(parts) != 2:
        raise DraftError(f"无效 draft_ref: {draft_ref}")
    return parts[0], parts[1]


class DraftLedger:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mail_drafts (
              draft_ref TEXT PRIMARY KEY,
              account_id TEXT NOT NULL,
              provider_account TEXT NOT NULL,
              provider_uuid TEXT NOT NULL,
              provider_message_id TEXT NOT NULL DEFAULT '',
              state TEXT NOT NULL CHECK(state IN ('draft', 'sending', 'sent', 'unknown')),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              last_fingerprint TEXT NOT NULL DEFAULT '',
              approval_fingerprint TEXT NOT NULL DEFAULT '',
              send_started_at TEXT NOT NULL DEFAULT '',
              sent_at TEXT NOT NULL DEFAULT '',
              sent_receipt_json TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mail_draft_events (
              event_id INTEGER PRIMARY KEY AUTOINCREMENT,
              draft_ref TEXT NOT NULL,
              event_type TEXT NOT NULL,
              event_at TEXT NOT NULL,
              fingerprint TEXT NOT NULL DEFAULT '',
              detail_json TEXT NOT NULL DEFAULT ''
            )
            """
        )
        return conn

    def _event(
        self,
        conn: sqlite3.Connection,
        draft_ref: str,
        event_type: str,
        *,
        fingerprint: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO mail_draft_events
              (draft_ref, event_type, event_at, fingerprint, detail_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                draft_ref,
                event_type,
                utc_now(),
                fingerprint,
                json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
            ),
        )

    def register(
        self,
        *,
        account_id: str,
        snapshot: DraftSnapshot,
        event_type: str,
    ) -> dict[str, Any]:
        draft_ref = make_draft_ref(account_id, snapshot.provider_uuid)
        now = utc_now()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT state FROM mail_drafts WHERE draft_ref=?",
                (draft_ref,),
            ).fetchone()
            if existing and existing["state"] != "draft":
                raise DraftAlreadyClaimed(
                    f"草稿已进入不可重置状态: {existing['state']}"
                )
            conn.execute(
                """
                INSERT INTO mail_drafts (
                  draft_ref, account_id, provider_account, provider_uuid,
                  provider_message_id, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)
                ON CONFLICT(draft_ref) DO UPDATE SET
                  provider_account=excluded.provider_account,
                  provider_message_id=excluded.provider_message_id,
                  updated_at=excluded.updated_at
                """,
                (
                    draft_ref,
                    account_id,
                    snapshot.provider_account,
                    snapshot.provider_uuid,
                    snapshot.provider_message_id,
                    now,
                    now,
                ),
            )
            self._event(conn, draft_ref, event_type)
        return self.get(draft_ref)

    def register_sent_identity(
        self,
        *,
        account_id: str,
        provider_account: str,
        provider_uuid: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        draft_ref = make_draft_ref(account_id, provider_uuid)
        now = utc_now()
        message_id = str(receipt.get("message_id") or "")
        receipt_json = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mail_drafts (
                  draft_ref, account_id, provider_account, provider_uuid,
                  provider_message_id, state, created_at, updated_at,
                  sent_at, sent_receipt_json
                ) VALUES (?, ?, ?, ?, ?, 'sent', ?, ?, ?, ?)
                ON CONFLICT(draft_ref) DO UPDATE SET
                  provider_account=excluded.provider_account,
                  provider_message_id=excluded.provider_message_id,
                  state='sent',
                  updated_at=excluded.updated_at,
                  sent_at=excluded.sent_at,
                  sent_receipt_json=excluded.sent_receipt_json
                """,
                (
                    draft_ref,
                    account_id,
                    provider_account,
                    provider_uuid,
                    message_id,
                    now,
                    now,
                    now,
                    receipt_json,
                ),
            )
            self._event(
                conn,
                draft_ref,
                "sent_reconciled",
                detail=receipt,
            )
        return self.get(draft_ref)

    def get(self, draft_ref: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM mail_drafts WHERE draft_ref=?",
                (draft_ref,),
            ).fetchone()
        if row is None:
            raise DraftNotFound(f"找不到草稿记录: {draft_ref}")
        payload = dict(row)
        raw_receipt = payload.pop("sent_receipt_json", "")
        payload["sent_receipt"] = json.loads(raw_receipt) if raw_receipt else None
        return payload

    def note_inspection(
        self,
        draft_ref: str,
        *,
        snapshot: DraftSnapshot,
        fingerprint: str,
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE mail_drafts
                SET provider_message_id=?, last_fingerprint=?, updated_at=?
                WHERE draft_ref=?
                """,
                (snapshot.provider_message_id, fingerprint, now, draft_ref),
            )
            self._event(
                conn,
                draft_ref,
                "inspected",
                fingerprint=fingerprint,
            )

    def claim_send(self, draft_ref: str, *, fingerprint: str) -> None:
        now = utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                """
                UPDATE mail_drafts
                SET state='sending', approval_fingerprint=?,
                    send_started_at=?, updated_at=?
                WHERE draft_ref=? AND state='draft'
                """,
                (fingerprint, now, now, draft_ref),
            ).rowcount
            if changed != 1:
                current = conn.execute(
                    "SELECT state FROM mail_drafts WHERE draft_ref=?",
                    (draft_ref,),
                ).fetchone()
                state = current["state"] if current else "missing"
                raise DraftAlreadyClaimed(
                    f"草稿不可再次发送，当前状态: {state}"
                )
            self._event(
                conn,
                draft_ref,
                "send_claimed",
                fingerprint=fingerprint,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_unknown(
        self,
        draft_ref: str,
        *,
        provider_message_id: str = "",
        detail: str = "",
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE mail_drafts
                SET state='unknown',
                    provider_message_id=CASE WHEN ?='' THEN provider_message_id ELSE ? END,
                    updated_at=?
                WHERE draft_ref=? AND state='sending'
                """,
                (provider_message_id, provider_message_id, now, draft_ref),
            )
            self._event(
                conn,
                draft_ref,
                "send_unknown",
                detail={"error": detail},
            )

    def mark_sent(
        self,
        draft_ref: str,
        *,
        provider_message_id: str,
        receipt: dict[str, Any],
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE mail_drafts
                SET state='sent', provider_message_id=?, sent_at=?,
                    sent_receipt_json=?, updated_at=?
                WHERE draft_ref=? AND state IN ('draft', 'sending', 'unknown')
                """,
                (
                    provider_message_id,
                    now,
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                    now,
                    draft_ref,
                ),
            )
            self._event(
                conn,
                draft_ref,
                "sent_verified",
                detail=receipt,
            )


def snapshot_payload(
    account_id: str,
    draft_ref: str,
    snapshot: DraftSnapshot,
    *,
    state: str,
) -> dict[str, Any]:
    return {
        "draft_ref": draft_ref,
        "account_id": account_id,
        "provider": "apple-mail",
        "provider_account": snapshot.provider_account,
        "provider_uuid": snapshot.provider_uuid,
        "provider_message_id": snapshot.provider_message_id,
        "state": state,
        "sender": snapshot.sender,
        "to": [asdict(item) for item in snapshot.to],
        "cc": [asdict(item) for item in snapshot.cc],
        "bcc": [asdict(item) for item in snapshot.bcc],
        "subject": snapshot.subject,
        "body_text": snapshot.body_text,
        "attachments": [
            {
                "name": item.name,
                "mime_type": item.mime_type,
                "size": item.size,
                "content_sha256": item.content_sha256,
            }
            for item in snapshot.attachments
        ],
        "approval_fingerprint": approval_fingerprint(account_id, snapshot),
    }


class DraftService:
    def __init__(self, ledger: DraftLedger, provider: DraftProvider):
        self.ledger = ledger
        self.provider = provider

    def create(
        self,
        *,
        account_id: str,
        sender: str,
        to: list[Recipient],
        cc: list[Recipient],
        bcc: list[Recipient],
        subject: str,
        body_text: str,
        attachments: list[Path],
        visible: bool,
    ) -> dict[str, Any]:
        prepared = prepare_body(body_text)
        snapshot = self.provider.create(
            sender=sender,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body_text=prepared,
            attachments=attachments,
            visible=visible,
        )
        if normalize_body(snapshot.body_text).rstrip("\n") != prepared.rstrip("\n"):
            cleaned = False
            try:
                cleaned = self.provider.discard(
                    provider_account=snapshot.provider_account,
                    provider_uuid=snapshot.provider_uuid,
                )
            except Exception:
                pass
            raise DraftError(
                "Apple Mail 保存后的正文与输入不一致；"
                + ("异常草稿已移入已删除邮件" if cleaned else "异常草稿仍保留在 Mail 中")
            )
        record = self.ledger.register(
            account_id=account_id,
            snapshot=snapshot,
            event_type="created",
        )
        return snapshot_payload(
            account_id,
            record["draft_ref"],
            snapshot,
            state=record["state"],
        )

    def adopt(
        self,
        *,
        account_id: str,
        provider_account: str,
        provider_uuid: str,
    ) -> dict[str, Any]:
        prior_receipt = self.provider.find_sent(
            provider_account=provider_account,
            provider_uuid=provider_uuid,
            provider_message_id="",
            timeout_seconds=0,
        )
        if prior_receipt:
            record = self.ledger.register_sent_identity(
                account_id=account_id,
                provider_account=provider_account,
                provider_uuid=provider_uuid,
                receipt=prior_receipt,
            )
            return {
                "draft_ref": record["draft_ref"],
                "account_id": account_id,
                "provider": "apple-mail",
                "state": "sent",
                "sent_at": record["sent_at"],
                "sent_receipt": record["sent_receipt"],
            }
        snapshot = self.provider.inspect(
            provider_account=provider_account,
            provider_uuid=provider_uuid,
        )
        record = self.ledger.register(
            account_id=account_id,
            snapshot=snapshot,
            event_type="adopted",
        )
        receipt = self.provider.find_sent(
            provider_account=snapshot.provider_account,
            provider_uuid=snapshot.provider_uuid,
            provider_message_id=snapshot.provider_message_id,
            timeout_seconds=0,
        )
        if receipt:
            self.ledger.mark_sent(
                record["draft_ref"],
                provider_message_id=str(receipt.get("message_id") or ""),
                receipt=receipt,
            )
            return self.inspect(record["draft_ref"])
        return snapshot_payload(
            account_id,
            record["draft_ref"],
            snapshot,
            state=record["state"],
        )

    def inspect(self, draft_ref: str) -> dict[str, Any]:
        record = self.ledger.get(draft_ref)
        if record["state"] == "sent":
            return {
                "draft_ref": draft_ref,
                "account_id": record["account_id"],
                "provider": "apple-mail",
                "state": "sent",
                "sent_at": record["sent_at"],
                "sent_receipt": record["sent_receipt"],
            }

        receipt = self.provider.find_sent(
            provider_account=record["provider_account"],
            provider_uuid=record["provider_uuid"],
            provider_message_id=record["provider_message_id"],
            timeout_seconds=0,
        )
        if receipt:
            self.ledger.mark_sent(
                draft_ref,
                provider_message_id=str(receipt.get("message_id") or ""),
                receipt=receipt,
            )
            return self.inspect(draft_ref)

        snapshot = self.provider.inspect(
            provider_account=record["provider_account"],
            provider_uuid=record["provider_uuid"],
        )
        fingerprint = approval_fingerprint(record["account_id"], snapshot)
        self.ledger.note_inspection(
            draft_ref,
            snapshot=snapshot,
            fingerprint=fingerprint,
        )
        return snapshot_payload(
            record["account_id"],
            draft_ref,
            snapshot,
            state=record["state"],
        )

    def open(self, draft_ref: str) -> dict[str, Any]:
        record = self.ledger.get(draft_ref)
        if record["state"] != "draft":
            raise DraftAlreadyClaimed(
                f"只有 draft 状态可以打开编辑，当前状态: {record['state']}"
            )
        snapshot = self.provider.open(
            provider_account=record["provider_account"],
            provider_uuid=record["provider_uuid"],
        )
        return {
            "draft_ref": draft_ref,
            "state": record["state"],
            "opened": True,
            "subject": snapshot.subject,
        }

    def send(self, draft_ref: str, *, approval: str) -> dict[str, Any]:
        record = self.ledger.get(draft_ref)
        if record["state"] != "draft":
            raise DraftAlreadyClaimed(
                f"草稿不可再次发送，当前状态: {record['state']}"
            )
        prior_receipt = self.provider.find_sent(
            provider_account=record["provider_account"],
            provider_uuid=record["provider_uuid"],
            provider_message_id=record["provider_message_id"],
            timeout_seconds=0,
        )
        if prior_receipt:
            self.ledger.mark_sent(
                draft_ref,
                provider_message_id=str(prior_receipt.get("message_id") or ""),
                receipt=prior_receipt,
            )
            raise DraftAlreadyClaimed(
                "Apple Mail Sent 邮箱已存在同一草稿身份，已锁定为 sent"
            )
        snapshot = self.provider.inspect(
            provider_account=record["provider_account"],
            provider_uuid=record["provider_uuid"],
        )
        fingerprint = approval_fingerprint(record["account_id"], snapshot)
        if approval != fingerprint:
            raise ApprovalMismatch(
                "审批指纹与 Apple Mail 当前草稿不一致；请重新 inspect 和审核"
            )

        self.ledger.note_inspection(
            draft_ref,
            snapshot=snapshot,
            fingerprint=fingerprint,
        )
        self.ledger.claim_send(draft_ref, fingerprint=fingerprint)
        start: SendStart | None = None
        try:
            start = self.provider.send(snapshot)
            receipt = self.provider.find_sent(
                provider_account=snapshot.provider_account,
                provider_uuid=start.provider_uuid,
                provider_message_id=start.provider_message_id,
                timeout_seconds=30,
            )
            if not receipt:
                raise SendStatusUnknown(
                    "Apple Mail 已接受发送操作，但 Sent 邮箱尚未回读到凭证"
                )
        except Exception as exc:
            self.ledger.mark_unknown(
                draft_ref,
                provider_message_id=(
                    start.provider_message_id if start is not None else ""
                ),
                detail=str(exc),
            )
            if isinstance(exc, SendStatusUnknown):
                raise
            raise SendStatusUnknown(
                "发送结果未知，已禁止自动重试；请使用 draft inspect 做只读核对"
            ) from exc

        self.ledger.mark_sent(
            draft_ref,
            provider_message_id=start.provider_message_id,
            receipt=receipt,
        )
        return {
            "draft_ref": draft_ref,
            "state": "sent",
            "approval_fingerprint": fingerprint,
            "sent_receipt": receipt,
        }
