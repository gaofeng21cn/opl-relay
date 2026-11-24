from types import SimpleNamespace
import pytest

from codex_mail_workbench import server_drafts as drafts, server_send as delivery
from codex_mail_workbench.drafts import (
    ApprovalMismatch, DraftAlreadyClaimed,
)


class Mailbox:
    def __init__(self):
        self.draft = None
        self.sent = None
        self.sends = 0
        self.unknown = False
        self.submitted = None
        self.append_count = 0

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

    def check_smtp(self):
        pass

    def submit_draft(self, raw, *, sender, recipients):
        assert sender == "owner@example.test"
        assert recipients == ["recipient@example.test", "copied@example.test"]
        self.sends += 1
        self.submitted = raw
        if self.unknown:
            raise TimeoutError("connection lost after submission")

    def append_sent(self, raw, *, folder):
        assert folder == "Sent Items"
        assert raw == self.submitted
        self.append_count += 1
        self.sent = raw

    def remove_exact(self, folder, uid):
        self.draft = None


@pytest.fixture
def service(tmp_path, monkeypatch):
    client = Mailbox()
    account = SimpleNamespace(account_id="work", email="owner@example.test", graph=None, smtp=True)
    monkeypatch.setattr(drafts, "load_accounts_config", lambda path: {"work": account})
    monkeypatch.setattr(delivery, "load_account", lambda *args: account)
    monkeypatch.setattr(drafts, "Himalaya", lambda *args: client)
    monkeypatch.setattr(delivery, "Himalaya", lambda *args: client)
    monkeypatch.setattr(delivery.time, "sleep", lambda _: None)
    inputs = dict(config_path=tmp_path / "accounts.toml", ledger_path=tmp_path / "drafts.db",
                  db_path=tmp_path / "mail.db", account_id="work", request_id="one",
                  to=["recipient@example.test"], cc=["copied@example.test"], subject="Re: 转发：Controlled self check",
                  in_reply_to="<original@example.test>", references=["<ancestor@example.test>"],
                  body="Reviewed prose", signature="Best regards,\nOwner")
    prepared = drafts.server_draft(**inputs, apply=True)
    send = {key: inputs[key] for key in ("config_path", "ledger_path", "account_id", "request_id")}
    return client, inputs, send | {"approval": prepared["approval_fingerprint"]}


def test_send_verifies_same_content_and_cleans_only_its_draft(service):
    client, inputs, send = service
    result = delivery.send_server_draft(**send)
    assert result["state"] == "sent" and result["draft_cleanup"] == "cleaned"
    assert result["server_verified"] and result["sent_receipt"]["mailbox"] == "Sent Items"
    headers = drafts.content_snapshot(client.sent)["headers"]
    assert headers["Message-ID"] == result["sent_receipt"]["message_id"]
    assert headers["Subject"] == "Re: Controlled self check"
    assert headers["In-Reply-To"] == "<original@example.test>"
    assert headers["References"] == "<ancestor@example.test> <original@example.test>"
    assert client.sends == client.append_count == 1 and client.draft is None
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
    result = delivery.send_server_draft(**send)
    assert result["state"] == "unknown" and result["smtp_accepted"] is False
    assert client.append_count == 0
    with pytest.raises(DraftAlreadyClaimed):
        delivery.send_server_draft(**send)
    client.sent = client.submitted
    result = drafts.server_draft(**inputs, inspect=True)
    assert result["state"] == "sent" and result["sent_receipt"]["message_id"]
    assert client.sends == 1


def test_unresolved_send_stays_unknown_even_if_draft_still_exists(service):
    client, inputs, send = service
    client.unknown = True
    assert delivery.send_server_draft(**send)["state"] == "unknown"
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


def test_imap_delivery_needs_no_apple_mail(service, monkeypatch):
    client, inputs, send = service
    def unavailable():
        raise AssertionError("Apple Mail must not be used for IMAP delivery")
    monkeypatch.setattr(delivery, "AppleMailProvider", unavailable)
    assert delivery.send_server_draft(**send)["state"] == "sent"


def test_smtp_preflight_failure_leaves_draft_sendable(service):
    client, inputs, send = service
    def bad_auth():
        raise ValueError("SMTP credentials unavailable")
    client.check_smtp = bad_auth
    with pytest.raises(ValueError, match="credentials"):
        delivery.send_server_draft(**send)
    assert client.sends == 0
    assert drafts.server_draft(**inputs, inspect=True)["state"] == "draft"


def test_smtp_acceptance_with_unconfirmed_sent_copy_never_resubmits(service):
    client, inputs, send = service
    def failed_append(raw, *, folder):
        client.append_count += 1
        raise TimeoutError("Sent APPEND result unknown")
    client.append_sent = failed_append
    result = delivery.send_server_draft(**send)
    assert result["state"] == "unknown" and result["smtp_accepted"] is True
    assert drafts.server_draft(**inputs, inspect=True)["state"] == "unknown"
    with pytest.raises(DraftAlreadyClaimed):
        delivery.send_server_draft(**send)
    assert client.sends == client.append_count == 1


def test_existing_apple_mail_handoff_cannot_switch_to_smtp(service):
    client, inputs, send = service
    with drafts._ledger(inputs["ledger_path"]) as conn:
        delivery._schema(conn)
        conn.execute("INSERT INTO server_draft_handoffs VALUES (?,?)", (inputs["request_id"], "old"))
    with pytest.raises(DraftAlreadyClaimed, match="Apple Mail handoff"):
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
