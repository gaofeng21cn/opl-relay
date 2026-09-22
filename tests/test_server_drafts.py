from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from types import SimpleNamespace

import pytest

from codex_mail_workbench import server_drafts as sd


SIGNATURE = "Best regards,\nFeng GAO, Ph.D.\nhttps://example.test/"


def source(*, html_only=False):
    msg = EmailMessage()
    msg["From"] = 'Coordinator <coord@example.test>'
    msg["To"] = 'Owner <owner@example.test>, Person <person@example.test>'
    msg["Cc"] = 'Alias <alias@example.test>, Member <member@example.test>, person@example.test'
    msg["Subject"] = "会議の旅程 / Conference"
    msg["Message-ID"] = "<latest@example.test>"
    msg["References"] = "<first@example.test>"
    msg.set_content("<style>HIDDEN</style><p>最新の質問</p><blockquote>Previous conversation</blockquote>"
                    if html_only else "最新の質問\n\nPrevious conversation\n> Older question",
                    subtype="html" if html_only else "plain")
    return msg


def build(**kwargs):
    defaults = dict(sender="owner@example.test", self_addresses=["owner@example.test", "alias@example.test"],
                    body="小林様\n\nご提案の内容で問題ございません。\nよろしくお願いいたします。",
                    signature=SIGNATURE, source_raw=source().as_bytes())
    return sd.build_message(**(defaults | kwargs))


def test_reply_all_preserves_route_thread_context_and_unicode_without_duplicate_signature(tmp_path):
    attachment = tmp_path / "行程单.pdf"
    attachment.write_bytes(b"test attachment")
    raw = build(attachments=[attachment])
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    assert [a.addr_spec for a in msg["To"].addresses] == ["coord@example.test", "person@example.test"]
    assert [a.addr_spec for a in msg["Cc"].addresses] == ["member@example.test"]
    assert msg["In-Reply-To"] == "<latest@example.test>"
    assert msg["References"] == "<first@example.test> <latest@example.test>"
    assert msg["Subject"] == "Re: 会議の旅程 / Conference"
    plain = msg.get_body(preferencelist=("plain",)).get_content().replace("\r\n", "\n")
    assert plain.startswith("小林様\n\nご提案")
    assert plain.count("Best regards,") == 1
    assert "> Previous conversation" in plain and "> > Older question" in plain
    assert "<blockquote" in msg.get_body(preferencelist=("html",)).get_content()
    assert next(msg.iter_attachments()).get_filename() == "行程单.pdf"
    assert next(msg.iter_attachments()).get_payload(decode=True) == b"test attachment"


def test_reply_to_sent_mail_uses_original_recipients_and_reply_to_is_honored():
    msg = source()
    msg["Reply-To"] = "assistant@example.test"
    out = BytesParser(policy=policy.default).parsebytes(build(source_raw=msg.as_bytes()))
    assert out["To"].addresses[0].addr_spec == "assistant@example.test"
    msg.replace_header("From", "owner@example.test")
    out = BytesParser(policy=policy.default).parsebytes(build(source_raw=msg.as_bytes()))
    assert [a.addr_spec for a in out["To"].addresses] == ["person@example.test"]


def test_html_only_quote_is_readable_without_active_content():
    raw = build(source_raw=source(html_only=True).as_bytes(), body="Hello <script> & friends")
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    plain = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert "最新の質問" in plain and "Previous conversation" in plain
    assert "HIDDEN" not in plain
    assert "&lt;script&gt; &amp;" in html
    assert "<script>" not in html


@pytest.mark.parametrize("bad", ["no-id", "bcc", "bad-address", "duplicate-id"])
def test_ambiguous_source_fails_before_writing(bad):
    msg = source()
    if bad == "no-id":
        del msg["Message-ID"]
    elif bad == "bcc":
        msg["Bcc"] = "hidden@example.test"
    elif bad == "bad-address":
        msg.replace_header("From", "not-an-email")
    else:
        raw = b"Message-ID: <duplicate@example.test>\n" + msg.as_bytes()
        with pytest.raises(ValueError):
            build(source_raw=raw)
        return
    with pytest.raises(ValueError):
        build(source_raw=msg.as_bytes())


def test_duplicate_signature_and_header_injection_rejected():
    with pytest.raises(ValueError):
        build(body="Hello\n\n" + SIGNATURE)
    with pytest.raises(ValueError):
        build(source_raw=None, to=["a@example.test\nBcc: b@example.test"], subject="Hi")


class FakeImap:
    def __init__(self):
        self.raw = None
        self.appends = 0
        self.timeout_after_append = False
        self.flags = b"\\Draft"

    def login(self, *args):
        return "OK", []

    def list(self):
        return "OK", [b'(\\Drafts) "/" "Drafts"']

    def select(self, folder, readonly):
        assert folder == '"Drafts"' and readonly is True
        return "OK", []

    def uid(self, command, *args):
        if command == "search":
            return "OK", [b"3" if self.raw else b""]
        if command == "fetch":
            return "OK", [(b"1 (UID 3 FLAGS (" + self.flags + b") BODY[])", self.raw)]
        raise AssertionError("Unexpected mutation: " + command)

    def append(self, folder, flags, date, raw):
        assert folder == '"Drafts"' and flags == "(\\Draft)"
        self.raw = raw
        self.appends += 1
        if self.timeout_after_append:
            raise TimeoutError("connection lost")
        return "OK", []

    def logout(self):
        pass


@pytest.fixture
def service(tmp_path, monkeypatch):
    client = FakeImap()
    monkeypatch.setattr(sd, "load_accounts_config", lambda p: {"work": SimpleNamespace(
        email="owner@example.test", imap=SimpleNamespace(username="owner", credential_ref="secret-ref"))})
    monkeypatch.setattr(sd, "connect_imap", lambda *a, **k: client)
    monkeypatch.setattr(sd, "keychain_get_secret", lambda ref: "secret")
    kwargs = dict(config_path=tmp_path / "accounts.toml", db_path=tmp_path / "mail.sqlite",
                  ledger_path=tmp_path / "drafts.sqlite", account_id="work", request_id="one",
                  to=["member@example.test"], subject="Test", body="Hello\n\nApproved content", signature=SIGNATURE)
    return client, kwargs


def test_preview_does_not_connect_and_apply_is_idempotent(service, monkeypatch):
    client, kwargs = service
    assert sd.server_draft(**kwargs)["state"] == "prepared"
    assert client.appends == 0
    result = sd.server_draft(**kwargs, apply=True)
    assert result["server_verified"] is True and result["send_allowed"] is False
    assert sd.server_draft(**kwargs, apply=True)["uid"] == result["uid"]
    assert client.appends == 1
    with pytest.raises(ValueError, match="different content"):
        sd.server_draft(**(kwargs | {"body": "Changed"}), apply=True)
    assert client.appends == 1


def test_timeout_reconciles_without_duplicate_append(service):
    client, kwargs = service
    client.timeout_after_append = True
    with pytest.raises(TimeoutError):
        sd.server_draft(**kwargs, apply=True)
    assert sd.server_draft(**kwargs, inspect=True)["server_verified"] is True
    assert sd.server_draft(**kwargs, apply=True)["state"] == "draft"
    assert client.appends == 1


def test_missing_or_mobile_edited_draft_is_not_recreated(service):
    client, kwargs = service
    sd.server_draft(**kwargs, apply=True)
    saved = client.raw
    msg = BytesParser(policy=policy.default).parsebytes(saved)
    msg.replace_header("Subject", "Mobile edit")
    client.raw = msg.as_bytes()
    with pytest.raises(ValueError, match="changed"):
        sd.server_draft(**kwargs, apply=True)
    client.raw = None
    with pytest.raises(ValueError, match="absent"):
        sd.server_draft(**kwargs, apply=True)
    assert client.appends == 1


def test_non_draft_or_deleted_message_not_reported_ready(service):
    client, kwargs = service
    sd.server_draft(**kwargs, apply=True)
    for flags in (b"\\Seen", b"\\Draft \\Deleted"):
        client.flags = flags
        with pytest.raises(ValueError, match="not a live server draft"):
            sd.server_draft(**kwargs, inspect=True)


def test_ambiguous_drafts_folder_not_guessed(service):
    client, kwargs = service
    client.list = lambda: ("OK", [b'(\\Drafts) "/" "Drafts"', b'(\\Drafts) "/" "Other"'])
    with pytest.raises(ValueError, match="ambiguous"):
        sd.server_draft(**kwargs, apply=True)
    assert client.appends == 0
