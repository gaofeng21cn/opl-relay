from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from codex_mail_workbench.drafts import (
    ApprovalMismatch,
    Attachment,
    DraftAlreadyClaimed,
    DraftError,
    DraftLedger,
    DraftService,
    DraftSnapshot,
    Recipient,
    SendStart,
    SendStatusUnknown,
    approval_fingerprint,
    make_draft_ref,
    parse_draft_ref,
    prepare_body,
)


def snapshot(**changes: object) -> DraftSnapshot:
    base = DraftSnapshot(
        provider_account="Work",
        provider_uuid="11111111-2222-3333-4444-555555555555",
        provider_message_id="draft@example.test",
        sender="work@example.test",
        to=[Recipient("reviewer@example.test", "Reviewer")],
        cc=[],
        bcc=[],
        subject="Review request",
        body_text="First paragraph.\n\nSecond paragraph.",
        attachments=[
            Attachment(
                name="paper.pdf",
                mime_type="application/pdf",
                size=123,
                content_sha256="a" * 64,
                provider_id="attachment-1",
            )
        ],
        provider_guard={"subject": "Review request"},
    )
    return replace(base, **changes)


class FakeProvider:
    def __init__(self, current: DraftSnapshot):
        self.current = current
        self.send_calls = 0
        self.discard_calls = 0
        self.assert_create_body = True
        self.send_error: Exception | None = None
        self.sent_receipt: dict[str, object] = {
            "provider": "apple-mail",
            "message_id": current.provider_message_id,
            "provider_uuid": current.provider_uuid,
            "mailbox": "Sent",
        }
        self.receipt: dict[str, object] | None = None
        self.reply_all_kwargs: dict[str, object] | None = None

    def create(self, **kwargs: object) -> DraftSnapshot:
        if self.assert_create_body:
            assert kwargs["body_text"] == self.current.body_text
        return self.current

    def reply_all(self, **kwargs: object) -> DraftSnapshot:
        self.reply_all_kwargs = kwargs
        return self.current

    def inspect(self, **kwargs: object) -> DraftSnapshot:
        return self.current

    def open(self, **kwargs: object) -> DraftSnapshot:
        return self.current

    def send(self, current: DraftSnapshot) -> SendStart:
        self.send_calls += 1
        if self.send_error:
            raise self.send_error
        self.receipt = self.sent_receipt
        return SendStart(current.provider_message_id, current.provider_uuid)

    def find_sent(self, **kwargs: object) -> dict[str, object] | None:
        return self.receipt

    def discard(self, **kwargs: object) -> bool:
        self.discard_calls += 1
        return True


def registered_service(tmp_path: Path) -> tuple[DraftService, FakeProvider, str]:
    current = snapshot()
    provider = FakeProvider(current)
    service = DraftService(DraftLedger(tmp_path / "drafts.sqlite"), provider)
    adopted = service.adopt(
        account_id="work",
        provider_account=current.provider_account,
        provider_uuid=current.provider_uuid,
    )
    return service, provider, str(adopted["draft_ref"])


def test_body_preparation_removes_only_leading_blank_lines() -> None:
    body = prepare_body(
        "\r\n\r\nFirst paragraph. \r\n\u2028\r\nSecond paragraph.\t"
    )
    assert body == "First paragraph.\n\nSecond paragraph."


def test_fingerprint_covers_every_approved_surface() -> None:
    base = snapshot()
    original = approval_fingerprint("work", base)
    variants = [
        ("other", base),
        ("work", replace(base, provider_account="Other")),
        ("work", replace(base, sender="other@example.test")),
        ("work", replace(base, to=[Recipient("other@example.test")])),
        ("work", replace(base, cc=[Recipient("cc@example.test")])),
        ("work", replace(base, bcc=[Recipient("bcc@example.test")])),
        ("work", replace(base, subject="Changed")),
        ("work", replace(base, body_text="Changed")),
        (
            "work",
            replace(
                base,
                attachments=[
                    replace(base.attachments[0], content_sha256="b" * 64)
                ],
            ),
        ),
    ]
    assert all(
        approval_fingerprint(account_id, candidate) != original
        for account_id, candidate in variants
    )


def test_draft_ref_round_trip() -> None:
    value = make_draft_ref("work account", "UUID/value")
    assert parse_draft_ref(value) == ("work account", "UUID/value")


def test_send_requires_current_fingerprint_and_is_at_most_once(tmp_path: Path) -> None:
    service, provider, draft_ref = registered_service(tmp_path)

    with pytest.raises(ApprovalMismatch):
        service.send(draft_ref, approval="sha256:stale")
    assert provider.send_calls == 0

    inspected = service.inspect(draft_ref)
    result = service.send(
        draft_ref,
        approval=str(inspected["approval_fingerprint"]),
    )
    assert result["state"] == "sent"
    assert provider.send_calls == 1

    with pytest.raises(DraftAlreadyClaimed):
        service.send(
            draft_ref,
            approval=str(inspected["approval_fingerprint"]),
        )
    assert provider.send_calls == 1


def test_edit_invalidates_previous_approval(tmp_path: Path) -> None:
    service, provider, draft_ref = registered_service(tmp_path)
    inspected = service.inspect(draft_ref)
    provider.current = replace(provider.current, body_text="Edited after review")

    with pytest.raises(ApprovalMismatch):
        service.send(
            draft_ref,
            approval=str(inspected["approval_fingerprint"]),
        )
    assert provider.send_calls == 0


def test_create_cleans_up_body_mismatch(tmp_path: Path) -> None:
    current = snapshot(body_text="Changed by provider")
    provider = FakeProvider(current)
    provider.assert_create_body = False
    service = DraftService(DraftLedger(tmp_path / "drafts.sqlite"), provider)

    with pytest.raises(DraftError, match="异常草稿已移入已删除邮件"):
        service.create(
            account_id="work",
            sender=current.sender,
            to=current.to,
            cc=[],
            bcc=[],
            subject=current.subject,
            body_text="Expected body",
            attachments=[],
            visible=False,
        )
    assert provider.discard_calls == 1


def test_reply_all_registers_exact_source_and_keeps_send_gate(tmp_path: Path) -> None:
    current = snapshot(
        to=[Recipient("chair@example.test", "Chair")],
        cc=[Recipient("secretary@example.test", "Secretary")],
        body_text="Approved reply.\n\nOn Aug 4, Chair wrote:\n> Source message",
    )
    provider = FakeProvider(current)
    ledger_path = tmp_path / "drafts.sqlite"
    service = DraftService(DraftLedger(ledger_path), provider)

    result = service.reply_all(
        account_id="work",
        sender="work@example.test",
        provider_account="Work",
        source_message_id=141819,
        mailbox_path="INBOX/Conference",
        body_text="Approved reply.",
        attachments=[],
        visible=False,
    )

    assert provider.reply_all_kwargs == {
        "sender": "work@example.test",
        "provider_account": "Work",
        "source_message_id": 141819,
        "mailbox_path": "INBOX/Conference",
        "body_text": "Approved reply.",
        "attachments": [],
        "visible": False,
    }
    assert result["reply"] == {
        "mode": "reply_all",
        "source": {
            "provider": "apple-mail",
            "account": "Work",
            "id": 141819,
            "mailboxPath": "INBOX/Conference",
        },
    }
    assert result["to"][0]["address"] == "chair@example.test"
    assert result["cc"][0]["address"] == "secretary@example.test"
    inspected = service.inspect(str(result["draft_ref"]))
    assert inspected["approval_fingerprint"] == result["approval_fingerprint"]

    with sqlite3.connect(ledger_path) as conn:
        event_type, detail_json = conn.execute(
            "SELECT event_type, detail_json FROM mail_draft_events ORDER BY event_id LIMIT 1"
        ).fetchone()
    assert event_type == "reply_all_created"
    assert json.loads(detail_json) == result["reply"]


def test_reply_all_cleans_up_when_reviewed_body_is_not_preserved(tmp_path: Path) -> None:
    current = snapshot(body_text="Provider replaced the reviewed body")
    provider = FakeProvider(current)
    service = DraftService(DraftLedger(tmp_path / "drafts.sqlite"), provider)

    with pytest.raises(DraftError, match="Reply All 保存后的审核正文"):
        service.reply_all(
            account_id="work",
            sender="work@example.test",
            provider_account="Work",
            source_message_id=141819,
            mailbox_path="INBOX",
            body_text="Approved reply.",
            attachments=[],
            visible=False,
        )
    assert provider.discard_calls == 1


def test_unknown_send_result_is_not_retryable(tmp_path: Path) -> None:
    service, provider, draft_ref = registered_service(tmp_path)
    inspected = service.inspect(draft_ref)
    provider.send_error = RuntimeError("automation connection lost")

    with pytest.raises(SendStatusUnknown):
        service.send(
            draft_ref,
            approval=str(inspected["approval_fingerprint"]),
        )
    assert service.ledger.get(draft_ref)["state"] == "unknown"

    with pytest.raises(DraftAlreadyClaimed):
        service.send(
            draft_ref,
            approval=str(inspected["approval_fingerprint"]),
        )
    assert provider.send_calls == 1


def test_existing_sent_receipt_blocks_duplicate_send(tmp_path: Path) -> None:
    service, provider, draft_ref = registered_service(tmp_path)
    inspected = service.inspect(draft_ref)
    provider.receipt = provider.sent_receipt

    with pytest.raises(DraftAlreadyClaimed, match="已锁定为 sent"):
        service.send(
            draft_ref,
            approval=str(inspected["approval_fingerprint"]),
        )
    assert provider.send_calls == 0
    assert service.ledger.get(draft_ref)["state"] == "sent"


def test_inspect_reconciles_existing_sent_receipt(tmp_path: Path) -> None:
    service, provider, draft_ref = registered_service(tmp_path)
    provider.receipt = provider.sent_receipt

    result = service.inspect(draft_ref)

    assert result["state"] == "sent"
    assert result["sent_receipt"]["message_id"] == provider.current.provider_message_id
    assert service.ledger.get(draft_ref)["state"] == "sent"


def test_adopt_recovers_sent_identity_without_live_draft(tmp_path: Path) -> None:
    current = snapshot()
    provider = FakeProvider(current)
    provider.receipt = provider.sent_receipt
    service = DraftService(DraftLedger(tmp_path / "drafts.sqlite"), provider)

    result = service.adopt(
        account_id="work",
        provider_account=current.provider_account,
        provider_uuid=current.provider_uuid,
    )

    assert result["state"] == "sent"
    assert result["sent_receipt"]["message_id"] == current.provider_message_id


def test_ledger_does_not_store_message_content(tmp_path: Path) -> None:
    service, _, draft_ref = registered_service(tmp_path)
    service.inspect(draft_ref)
    raw = (tmp_path / "drafts.sqlite").read_bytes()
    assert b"First paragraph" not in raw
    assert b"reviewer@example.test" not in raw
