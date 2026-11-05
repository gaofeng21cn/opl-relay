from types import SimpleNamespace
from email import policy
from email.parser import BytesParser

import pytest

from codex_mail_workbench import server_drafts as drafts, server_send as delivery
from codex_mail_workbench.drafts import (
    ApprovalMismatch, Attachment, DraftAlreadyClaimed, DraftSnapshot,
    Recipient, SendStatusUnknown,
)
from codex_mail_workbench.drafts import SendStart


class Mailbox:
    def __init__(self):
        self.draft = None
        self.sent = None
        self.sends = 0
        self.unknown = False

    def folders(self):
        return [{"name": "Drafts", "flags": frozenset({"\\drafts"})},
                {"name": "Sent Items", "flags": frozenset({"\\sent"})}]

    def status(self, folder):
        return {"uidvalidity": 9, "count": int(bool(self.draft if folder == "Drafts" else self.sent))}

    def find_message(self, folder, message_id):
        raw = self.draft if folder == "Drafts" else self.sent
        return [42] if raw and drafts.content_snapshot(raw)["headers"]["Message-ID"] == message_id else []

    def metadata(self, folder, uids):
        return {uid: {"flags": ["\\Draft"] if folder == "Drafts" else []} for uid in uids}

    def mime(self, uid, *, folder):
        return self.draft if folder == "Drafts" else self.sent

    def create_draft(self, raw, *, folder):
        self.draft = raw

    def send_message(self, raw, *, sent_folder):
        self.sends += 1
        self.sent = raw
        if self.unknown:
            raise TimeoutError("connection lost after submission")

    def remove_exact(self, folder, uid):
        self.draft = None


class Provider:
    def __init__(self, client):
        self.client = client

    def inspect_server_draft(self, *, sender, message_id, expected_raw):
        if self.client.draft != expected_raw:
            raise ValueError("Server draft changed")
        return self.inspect()

    def inspect(self, **kwargs):
        message = BytesParser(policy=policy.default).parsebytes(self.client.draft)
        body = message.get_body(preferencelist=("plain", "html"))
        return DraftSnapshot(
            provider_account="Work", provider_uuid="controlled-draft-uuid",
            provider_message_id=str(message["Message-ID"]), sender=str(message["From"]),
            to=[Recipient(address="recipient@example.test")], cc=[], bcc=[],
            subject=str(message["Subject"]), body_text=body.get_content(), attachments=[],
            provider_threading={"in_reply_to_value": str(message.get("In-Reply-To", "")),
                                "references_value": str(message.get("References", ""))},
        )

    def find_sent(self, **kwargs):
        if not self.client.sent:
            return None
        return {"message_id": drafts.content_snapshot(self.client.sent)["headers"]["Message-ID"],
                "mailbox": "Sent Items", "subject": "Controlled self check"}

    def send(self, snapshot):
        self.client.send_message(self.client.draft, sent_folder="Sent Items")
        return SendStart(snapshot.provider_message_id, snapshot.provider_uuid)


@pytest.fixture
def service(tmp_path, monkeypatch):
    client = Mailbox()
    account = SimpleNamespace(account_id="work", email="owner@example.test", graph=None, smtp=True)
    monkeypatch.setattr(drafts, "load_accounts_config", lambda path: {"work": account})
    monkeypatch.setattr(delivery, "load_account", lambda *args: account)
    monkeypatch.setattr(drafts, "Himalaya", lambda *args: client)
    monkeypatch.setattr(delivery, "AppleMailProvider", lambda: Provider(client))
    inputs = dict(config_path=tmp_path / "accounts.toml", ledger_path=tmp_path / "drafts.db",
                  db_path=tmp_path / "mail.db", account_id="work", request_id="one",
                  to=["recipient@example.test"], subject="Controlled self check",
                  body="Reviewed prose", signature="Best regards,\nOwner")
    prepared = drafts.server_draft(**inputs, apply=True)
    send = {key: inputs[key] for key in ("config_path", "ledger_path", "account_id", "request_id")}
    return client, inputs, send | {"approval": prepared["approval_fingerprint"]}


def test_send_verifies_same_content_and_cleans_only_its_draft(service):
    client, inputs, send = service
    result = delivery.send_server_draft(**send)
    assert result["state"] == "sent" and result["draft_cleanup"] == "cleaned"
    assert result["sent_receipt"]["message_id"]
    assert client.sends == 1 and client.draft is None
    with pytest.raises(DraftAlreadyClaimed):
        delivery.send_server_draft(**send)
    assert drafts.server_draft(**inputs, inspect=True)["state"] == "sent"
    assert client.sends == 1


def test_stale_fingerprint_never_claims_or_sends(service):
    client, inputs, send = service
    with pytest.raises(ApprovalMismatch):
        delivery.send_server_draft(**(send | {"approval": "sha256:wrong"}))
    assert client.sends == 0
    assert delivery.send_server_draft(**send)["state"] == "sent"


def test_changed_live_draft_is_preserved_without_sending(service):
    client, inputs, send = service
    client.draft = client.draft.replace(b"Reviewed prose", b"Personal edits")
    with pytest.raises(ValueError, match="changed"):
        delivery.send_server_draft(**send)
    assert client.sends == 0 and b"Personal edits" in client.draft


def test_timeout_can_only_reconcile_never_retry_send(service):
    client, inputs, send = service
    client.unknown = True
    with pytest.raises(SendStatusUnknown):
        delivery.send_server_draft(**send)
    with pytest.raises(DraftAlreadyClaimed):
        delivery.send_server_draft(**send)
    result = drafts.server_draft(**inputs, inspect=True)
    assert result["state"] == "sent" and result["sent_receipt"]["message_id"]
    assert client.sends == 1


def test_unresolved_send_stays_unknown_even_if_draft_still_exists(service):
    client, inputs, send = service
    def unknown(raw, **kwargs):
        client.sends += 1
        raise TimeoutError("unknown delivery")
    client.send_message = unknown
    with pytest.raises(SendStatusUnknown):
        delivery.send_server_draft(**send)
    assert drafts.server_draft(**inputs, inspect=True)["state"] == "unknown"
    with pytest.raises(DraftAlreadyClaimed):
        delivery.send_server_draft(**send)
    assert client.sends == 1


def test_already_sent_exact_identity_is_not_sent_again(service):
    client, inputs, send = service
    client.sent = client.draft
    with pytest.raises(DraftAlreadyClaimed):
        delivery.send_server_draft(**send)
    assert client.sends == 0


def test_cleanup_failure_does_not_turn_verified_send_into_retryable_failure(service):
    client, inputs, send = service
    def unavailable(*args):
        raise TimeoutError("cleanup unavailable")
    client.remove_exact = unavailable
    result = delivery.send_server_draft(**send)
    assert result["state"] == "sent" and result["draft_cleanup"] == "pending"
    with pytest.raises(DraftAlreadyClaimed):
        delivery.send_server_draft(**send)
    assert client.sends == 1


def test_missing_synchronized_apple_mail_draft_does_not_claim_send(service, monkeypatch):
    client, inputs, send = service
    class Unavailable:
        def inspect_server_draft(self, **kwargs):
            raise ValueError("Apple Mail has not synchronized the draft")
    monkeypatch.setattr(delivery, "AppleMailProvider", Unavailable)
    with pytest.raises(ValueError, match="synchronized"):
        delivery.send_server_draft(**send)
    assert client.sends == 0


def test_prepared_preview_can_be_revised_without_creating_second_request(service):
    client, inputs, send = service
    revised = inputs | {"request_id": "preview", "body": "First wording"}
    drafts.server_draft(**revised)
    changed = drafts.server_draft(**(revised | {"body": "Revised wording"}))
    assert "Revised wording" in changed["body_text"]
    assert changed["state"] == "prepared"
    with pytest.raises(ValueError, match="different content"):
        drafts.server_draft(**(inputs | {"body": "Do not overwrite live draft"}), apply=True)


def test_delivery_check_never_claims_or_sends(service):
    client, inputs, send = service
    result = delivery.send_server_draft(**send, check=True)
    assert result["delivery_ready"] and result["sent_messages"] == 0
    assert client.sends == 0
    assert delivery.send_server_draft(**send)["state"] == "sent"
