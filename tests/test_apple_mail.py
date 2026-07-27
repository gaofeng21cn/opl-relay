from __future__ import annotations

import base64
from email.message import EmailMessage
from pathlib import Path

from codex_mail_workbench.apple_mail import AppleMailProvider, _snapshot
from codex_mail_workbench.drafts import Recipient


def raw_draft() -> dict[str, object]:
    message = EmailMessage()
    message["From"] = "work@example.test"
    message["To"] = "Reviewer <reviewer@example.test>"
    message["Subject"] = "Review request"
    message.set_content("First paragraph.\n\nSecond paragraph.")
    message.add_attachment(
        b"pdf-bytes",
        maintype="application",
        subtype="pdf",
        filename="paper.pdf",
    )
    source = message.as_bytes().decode("utf-8")
    return {
        "providerAccount": "Work",
        "uuid": "11111111-2222-3333-4444-555555555555",
        "messageId": "draft@example.test",
        "sender": "work@example.test",
        "to": [{"address": "reviewer@example.test", "name": "Reviewer"}],
        "cc": [],
        "bcc": [],
        "subject": "Review request",
        "content": "\nirst paragraph.\n\nSecond paragraph.",
        "attachments": [
            {
                "name": "paper.pdf",
                "mimeType": "application/pdf",
                "fileSize": len(b"pdf-bytes"),
                "id": "attachment-1",
            }
        ],
        "source": source,
        "guard": {"subject": "Review request"},
    }


def test_snapshot_prefers_mime_body_and_hashes_attachments() -> None:
    current = _snapshot(raw_draft())
    assert current.body_text == "First paragraph.\n\nSecond paragraph."
    assert current.attachments[0].content_sha256
    assert current.attachments[0].provider_id == "attachment-1"


def test_provider_passes_guard_to_atomic_send() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def runner(action: str, payload: dict[str, object]) -> object:
        calls.append((action, payload))
        if action == "send":
            return {
                "messageId": "sent@example.test",
                "uuid": "11111111-2222-3333-4444-555555555555",
            }
        return raw_draft()

    provider = AppleMailProvider(runner)
    current = provider.inspect(
        provider_account="Work",
        provider_uuid="11111111-2222-3333-4444-555555555555",
    )
    result = provider.send(current)

    assert result.provider_message_id == "sent@example.test"
    assert calls[-1][0] == "send"
    assert calls[-1][1]["expectedGuard"] == {"subject": "Review request"}


def test_provider_create_uses_absolute_attachment_paths(tmp_path: Path) -> None:
    attachment = tmp_path / "paper.pdf"
    attachment.write_bytes(base64.b64decode("cGRm"))
    captured: dict[str, object] = {}

    def runner(action: str, payload: dict[str, object]) -> object:
        captured.update(payload)
        return raw_draft()

    provider = AppleMailProvider(runner)
    provider.create(
        sender="work@example.test",
        to=[Recipient("reviewer@example.test")],
        cc=[],
        bcc=[],
        subject="Review request",
        body_text="First paragraph.\n\nSecond paragraph.",
        attachments=[attachment],
        visible=True,
    )

    assert captured["attachments"] == [str(attachment.resolve())]
    assert captured["visible"] is True
