import itertools
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate
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
        email="owner@example.test", imap=SimpleNamespace(username="owner", credential_ref="secret-ref",
                                                            fallback_keychain=None))})
    monkeypatch.setattr(sd, "connect_imap", lambda *a, **k: client)
    monkeypatch.setattr(sd, "keychain_get_secret", lambda ref, **kwargs: "secret")
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


_SENT_SEQUENCE = itertools.count(1)


def sent_message(*, to="Coordinator <coord@example.test>, Person <person@example.test>",
                 cc="Member <member@example.test>",
                 subject="Re: 会議の旅程 / Conference",
                 parent="<latest@example.test>", date=None):
    msg = EmailMessage()
    msg["From"] = "owner@example.test"
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Message-ID"] = f"<sent-{next(_SENT_SEQUENCE)}@mail.example.test>"
    msg["Date"] = date or formatdate(localtime=True)
    if parent:
        msg["In-Reply-To"] = parent
    msg.set_content("Approved content\n\n" + SIGNATURE)
    return msg.as_bytes()


class ReconcilerImap:
    """Drafts plus a Sent folder, with UIDPLUS deletion for the leftover drafts."""

    def __init__(self, drafts=(), sent=(), uidplus=True):
        self.drafts = list(drafts)
        self.sent = list(sent)
        self.mutations = []
        self.uidplus = uidplus
        self.selected = None

    def login(self, *args):
        return "OK", []

    def logout(self):
        pass

    def capability(self):
        return "OK", [b"IMAP4rev1 UIDPLUS" if self.uidplus else b"IMAP4rev1"]

    def list(self):
        return "OK", [b'(\\Drafts) "/" "Drafts"', b'(\\Sent) "/" "Sent Items"']

    def select(self, folder, readonly):
        self.selected = folder
        return "OK", []

    def uid(self, command, *args):
        if command == "search":
            folder = self.drafts if self.selected == '"Drafts"' else self.sent
            if args[0] == "HEADER":
                wanted = args[2].strip('"')
                hits = [str(i) for i, item in enumerate(folder, 1)
                        if BytesParser(policy=policy.default).parsebytes(item)["Message-ID"] == wanted]
            else:
                hits = [str(i) for i in range(1, len(folder) + 1)]
            return "OK", [" ".join(hits).encode()]
        if command == "fetch":
            folder = self.drafts if self.selected == '"Drafts"' else self.sent
            index = int(args[0]) - 1
            item = folder[index]
            flags = b"\\Draft" if self.selected == '"Drafts"' else b"\\Seen"
            return "OK", [(b"1 (UID " + str(index + 1).encode() + b" FLAGS (" + flags + b") BODY[])", item)]
        if command == "store":
            self.mutations.append(("store", args[0]))
            return "OK", []
        if command == "expunge":
            assert self.selected == '"Drafts"', "expunge must be scoped to Drafts"
            self.mutations.append(("expunge", args[0]))
            del self.drafts[int(args[0]) - 1]
            return "OK", []
        raise AssertionError("Unexpected command: " + command)


@pytest.fixture
def reconciler(tmp_path, monkeypatch):
    def install(client):
        monkeypatch.setattr(sd, "load_accounts_config", lambda p: {"work": SimpleNamespace(
            email="owner@example.test",
            imap=SimpleNamespace(username="owner", credential_ref="secret-ref", fallback_keychain=None))})
        monkeypatch.setattr(sd, "connect_imap", lambda *a, **k: client)
        monkeypatch.setattr(sd, "keychain_get_secret", lambda ref, **kwargs: "secret")
        kwargs = dict(config_path=tmp_path / "accounts.toml",
                      ledger_path=tmp_path / "drafts.sqlite", account_id="work")
        return kwargs
    return install


def seed_verified_draft(tmp_path, *, reply=True, request_id="one"):
    """Record one ledger-owned draft, built by the production message builder."""
    raw = (build(  # noqa: E501 - a threaded reply to the shared fixture source
        sender="owner@example.test", self_addresses=["owner@example.test", "alias@example.test"]
    ) if reply else sd.build_message(
        sender="owner@example.test", self_addresses=["owner@example.test"],
        body="Approved content", signature=SIGNATURE,
        to=["person@example.test"], subject="Direct note"))
    conn = sd._ledger(tmp_path / "drafts.sqlite")
    conn.execute("INSERT INTO server_draft_requests VALUES (?,?,?,?,?,?)",
                 (request_id, "work", "digest-" + request_id, raw, "verified", "Drafts"))
    conn.commit()
    conn.close()
    return raw


def test_reconcile_removes_draft_already_carried_by_a_sent_reply(tmp_path, monkeypatch, reconciler):
    draft = seed_verified_draft(tmp_path)
    client = ReconcilerImap(drafts=[draft], sent=[sent_message()])
    result = sd.reconcile_server_drafts(**reconciler(client))
    assert result["candidates"] == 1 and result["cleaned"] == 0
    assert result["drafts"][0]["action"] == "candidate"
    assert client.mutations == []  # preview never mutates

    result = sd.reconcile_server_drafts(**reconciler(client), apply=True)
    assert result["cleaned"] == 1 and result["drafts"][0]["action"] == "cleaned"
    assert client.drafts == [] and ("expunge", "1") in client.mutations


def test_reconcile_keeps_a_pending_draft(tmp_path, monkeypatch, reconciler):
    draft = seed_verified_draft(tmp_path)
    other = sent_message(to="Someone <other@example.test>")
    client = ReconcilerImap(drafts=[draft], sent=[other])
    result = sd.reconcile_server_drafts(**reconciler(client), apply=True)
    assert result["candidates"] == 0 and client.drafts == [draft]
    assert result["drafts"][0]["reason"] == "no matching sent message"


def test_reconcile_requires_same_reply_target_not_only_subject(tmp_path, monkeypatch, reconciler):
    draft = seed_verified_draft(tmp_path)
    same_subject = sent_message(parent="<a-different-parent@example.test>")
    client = ReconcilerImap(drafts=[draft], sent=[same_subject])
    result = sd.reconcile_server_drafts(**reconciler(client), apply=True)
    assert result["candidates"] == 0 and client.drafts == [draft]


def test_reconcile_requires_same_subject_even_with_same_reply_target(tmp_path, monkeypatch, reconciler):
    draft = seed_verified_draft(tmp_path)
    different_subject = sent_message(subject="Re: An unrelated question")
    client = ReconcilerImap(drafts=[draft], sent=[different_subject])
    result = sd.reconcile_server_drafts(**reconciler(client), apply=True)
    assert result["candidates"] == 0 and client.drafts == [draft]


def test_reconcile_preserves_a_draft_edited_on_another_client(tmp_path, monkeypatch, reconciler):
    draft = seed_verified_draft(tmp_path)
    edited = BytesParser(policy=policy.default).parsebytes(draft)
    edited.get_body(preferencelist=("plain",)).set_content(
        "A different reply that the user is still editing.")
    client = ReconcilerImap(drafts=[edited.as_bytes()], sent=[sent_message()])
    result = sd.reconcile_server_drafts(**reconciler(client), apply=True)
    assert result["candidates"] == 0 and client.drafts == [edited.as_bytes()]
    assert result["drafts"][0]["user_edited"] is True


def test_reconcile_ignores_a_sent_copy_older_than_the_draft(tmp_path, monkeypatch, reconciler):
    draft = seed_verified_draft(tmp_path)
    older = sent_message(date="Mon, 01 Jan 2024 09:00:00 +0800")
    client = ReconcilerImap(drafts=[draft], sent=[older])
    assert sd.reconcile_server_drafts(**reconciler(client), apply=True)["candidates"] == 0


def test_reconcile_reports_absent_draft_without_mutating(tmp_path, monkeypatch, reconciler):
    seed_verified_draft(tmp_path)
    client = ReconcilerImap(drafts=[], sent=[sent_message()])
    result = sd.reconcile_server_drafts(**reconciler(client), apply=True)
    assert result["drafts"][0]["action"] == "absent" and client.mutations == []


def test_reconcile_refuses_unscoped_expunge_without_uidplus(tmp_path, monkeypatch, reconciler):
    draft = seed_verified_draft(tmp_path)
    client = ReconcilerImap(drafts=[draft], sent=[sent_message()], uidplus=False)
    with pytest.raises(ValueError, match="UIDPLUS"):
        sd.reconcile_server_drafts(**reconciler(client), apply=True)
    assert client.drafts == [draft] and client.mutations == []


def test_reconcile_with_no_verified_drafts_does_not_connect(tmp_path, monkeypatch, reconciler):
    client = ReconcilerImap(drafts=[], sent=[])
    monkeypatch.setattr(sd, "connect_imap", lambda *a, **k: pytest.fail("unexpected connection"))
    monkeypatch.setattr(sd, "load_accounts_config", lambda p: {"work": SimpleNamespace(
        email="owner@example.test",
        imap=SimpleNamespace(username="owner", credential_ref="secret-ref", fallback_keychain=None))})
    result = sd.reconcile_server_drafts(config_path=tmp_path / "accounts.toml",
                                        ledger_path=tmp_path / "drafts.sqlite", account_id="work", apply=True)
    assert result["checked"] == 0
