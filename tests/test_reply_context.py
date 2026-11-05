import hashlib
from email import policy
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from codex_mail_workbench import reply_context as replies
from codex_mail_workbench.store import connect_email_store, upsert_email_message


@pytest.fixture
def thread(tmp_path, monkeypatch):
    monkeypatch.setattr(replies, "load_accounts_config", lambda path: {
        "work": SimpleNamespace(email="owner@example.test"),
        "other": SimpleNamespace(email="other@example.test")})
    database = tmp_path / "mail.db"
    sequence = []
    def seed(message_id, *, folder="INBOX", sender="secretary@example.test", parent="", day=21):
        message = EmailMessage(policy=policy.SMTP)
        message["Message-ID"] = message_id
        message["Subject"] = "Re: Conference invitation"
        message["From"] = sender
        message["To"] = "secretary@example.test" if sender.startswith("owner") else "owner@example.test"
        message["Cc"] = "speaker@example.test"
        message["Date"] = f"{day} Oct 2026 10:00:00 +0800"
        if parent:
            message["In-Reply-To"] = parent
            message["References"] = parent
        message.set_content("Full original body including biography request.")
        raw = message.as_bytes()
        sequence.append(message_id)
        conn = connect_email_store(database)
        try:
            return upsert_email_message(conn, account_id="work", folder=folder, folder_slug=folder,
                uid=len(sequence), uidvalidity=1, message_id=message_id, subject=str(message["Subject"]),
                sender=sender, recipient=str(message["To"]), date_iso=f"2026-10-{day}T10:00:00+08:00",
                raw_sha256=hashlib.sha256(raw).hexdigest(), raw_eml=raw, attachments=[],
                ingest_ts="2026-10-28T10:00:00+08:00")
        finally:
            conn.close()
    return dict(config_path=tmp_path / "accounts.toml", db_path=database, account_id="work"), seed


def test_followup_selects_latest_own_sent_in_same_thread_with_complete_route(thread):
    inputs, seed = thread
    source = seed("<original@example.test>")
    last_sent = seed("<sent@example.test>", folder="Sent Items", sender="owner@example.test",
                     parent="<original@example.test>", day=22)
    seed("<unrelated@example.test>", folder="Sent Items", sender="owner@example.test", day=25)
    seed("<spoofed@example.test>", sender="owner@example.test", parent="<original@example.test>", day=26)
    result = replies.reply_context(**inputs, storage_ref=source, latest_sent=True)
    assert result["storage_ref"] == last_sent
    assert result["in_reply_to"] == "<sent@example.test>"
    assert result["reply_to"] == ["secretary@example.test"]
    assert result["reply_cc"] == ["speaker@example.test"]
    assert "biography" in result["body_text"]
    assert result["coverage"]["complete"]


def test_ordinary_reply_retains_exact_incoming_source(thread):
    inputs, seed = thread
    source = seed("<original@example.test>")
    seed("<sent@example.test>", folder="Sent Items", sender="owner@example.test",
         parent="<original@example.test>", day=22)
    assert replies.reply_context(**inputs, storage_ref=source)["storage_ref"] == source


def test_thread_lookup_never_chooses_unrelated_same_subject(thread):
    inputs, seed = thread
    source = seed("<original@example.test>")
    seed("<unrelated@example.test>", folder="Sent Items", sender="owner@example.test", day=25)
    assert replies.reply_context(**inputs, storage_ref=source, latest_sent=True)["storage_ref"] == source


def test_query_followup_requires_real_thread_relationship(thread):
    inputs, seed = thread
    source = seed("<original@example.test>")
    sent = seed("<sent@example.test>", folder="Sent Items", sender="owner@example.test",
                parent="<original@example.test>", day=22)
    seed("<same-subject-unrelated@example.test>", folder="Sent Items",
         sender="owner@example.test", day=30)
    result = replies.reply_context(**inputs, query="Conference invitation", latest_sent=True)
    assert result["storage_ref"] == sent


def test_query_followup_fails_when_all_same_subject_messages_are_unrelated(thread):
    inputs, seed = thread
    seed("<unrelated@example.test>", folder="Sent Items", sender="owner@example.test", day=30)
    with pytest.raises(ValueError, match="matching sent"):
        replies.reply_context(**inputs, query="Conference invitation", latest_sent=True)


def test_plain_query_fails_on_multiple_candidate_sources(thread):
    inputs, seed = thread
    seed("<first@example.test>")
    seed("<second@example.test>", day=22)
    with pytest.raises(ValueError, match="multiple messages"):
        replies.reply_context(**inputs, query="Conference invitation")


def test_cross_account_source_is_rejected(thread):
    inputs, seed = thread
    source = seed("<original@example.test>")
    with pytest.raises(ValueError, match="selected account"):
        replies.reply_context(**(inputs | {"account_id": "other"}), storage_ref=source)
