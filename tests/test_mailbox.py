import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_mail_workbench import mailbox as mailbox_module
from codex_mail_workbench.config import MailAccount, MailEndpoint
from codex_mail_workbench.store import (
    connect_email_store,
    get_message_by_storage_ref,
    upsert_email_message,
)


RAW_MESSAGE = (
    b"Subject: Move me\r\n"
    b"From: editor@example.test\r\n"
    b"To: work@example.test\r\n"
    b"Message-ID: <move@example.test>\r\n"
    b"\r\n"
    b"body"
)


def seed_message(db_path: Path, *, raw: bytes = RAW_MESSAGE) -> str:
    raw_hash = hashlib.sha256(raw).hexdigest()
    conn = connect_email_store(db_path)
    try:
        return upsert_email_message(
            conn,
            account_id="work",
            folder="INBOX",
            folder_slug="INBOX",
            uid=7,
            uidvalidity=1,
            message_id="<move@example.test>",
            subject="Move me",
            sender="editor@example.test",
            recipient="work@example.test",
            date_iso="2026-08-16T10:00:00+08:00",
            raw_sha256=raw_hash,
            raw_eml=raw,
            attachments=[],
            ingest_ts="2026-08-16T10:01:00+08:00",
        )
    finally:
        conn.close()


def account() -> MailAccount:
    return MailAccount(
        account_id="work",
        email="work@example.test",
        imap=MailEndpoint(
            host="imap.example.test",
            port=993,
            security="ssl",
            username="work@example.test",
            credential_ref="work-imap",
        ),
        include_folders=["*"],
        exclude_folders=[],
    )


class FakeImap:
    def __init__(self, *, capabilities=b"IMAP4rev1 UIDPLUS MOVE"):
        self.capability_tokens, self.move_called = capabilities, False
        self.mailboxes = {"INBOX": {7: RAW_MESSAGE}, "Trash": {}}

    def folders(self):
        return [{"name": name, "flags": frozenset({"\\trash"}) if name == "Trash" else frozenset()}
                for name in self.mailboxes]

    def capabilities(self):
        return set(self.capability_tokens.decode().upper().split())

    def status(self, folder):
        return {"uidvalidity": 1, "count": len(self.mailboxes[folder])}

    def mime(self, uid, *, folder):
        return self.mailboxes[folder][int(uid)]

    def metadata(self, folder, uids):
        return {uid: {} for uid in uids if uid in self.mailboxes[folder]}

    def find_message(self, folder, message_id):
        return [uid for uid, raw in self.mailboxes[folder].items()
                if f"Message-ID: {message_id}".encode() in raw]

    def move(self, uid, *, source, destination):
        self.move_called = True
        self.mailboxes[destination][101] = self.mailboxes[source].pop(int(uid))

    def copy(self, uid, *, source, destination):
        self.move_called = True
        self.mailboxes[destination][101] = self.mailboxes[source][int(uid)]

    def remove_exact(self, folder, uid):
        assert "UIDPLUS" in self.capabilities()
        del self.mailboxes[folder][int(uid)]


class FakeGmailImap(FakeImap):
    def __init__(self):
        super().__init__(capabilities=b"IMAP4rev1 UIDPLUS MOVE X-GM-EXT-1")
        self.mailboxes = {"INBOX": {7: RAW_MESSAGE}, "[Gmail]/All Mail": {101: RAW_MESSAGE}}

    def folders(self):
        return [{"name": "INBOX", "flags": frozenset()},
                {"name": "Archive", "flags": frozenset({"\\noselect"})},
                {"name": "[Gmail]/All Mail", "flags": frozenset({"\\all"})}]


class FakeFolderImap(FakeImap):
    def __init__(self):
        super().__init__()
        self.mailboxes["Bill Finance"] = {}


def configure_mailbox(monkeypatch, fake: FakeImap) -> None:
    monkeypatch.setattr(mailbox_module, "load_account", lambda path, account_id: account())
    monkeypatch.setattr(mailbox_module, "Himalaya", lambda loaded: fake)


def test_bill_destination_requires_an_existing_exact_folder() -> None:
    mailboxes = [
        mailbox_module.ImapMailbox(name="Archive", flags=frozenset()),
        mailbox_module.ImapMailbox(name="Bill", flags=frozenset()),
        mailbox_module.ImapMailbox(name="Bill Travel", flags=frozenset()),
    ]
    assert mailbox_module._resolve_destination("bill", mailboxes) == "Bill"
    with pytest.raises(mailbox_module.MailboxMoveFailure) as missing:
        mailbox_module._resolve_destination("bill", mailboxes[:1])
    assert missing.value.code == "destination_missing"


def test_exact_destination_accepts_legacy_name_but_prefers_literal_live_match():
    boxes = [mailbox_module.ImapMailbox("[Gmail]/已发邮件", frozenset())]
    assert mailbox_module._resolve_exact_folder("[Gmail]/&XfJT0ZCuTvY-", boxes) == "[Gmail]/已发邮件"
    boxes = [mailbox_module.ImapMailbox(name, frozenset()) for name in ("Rock &- Roll", "Rock & Roll")]
    assert mailbox_module._resolve_exact_folder("Rock &- Roll", boxes) == "Rock &- Roll"


def test_move_dry_run_does_not_mutate_mailbox_or_local_store(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "mail.sqlite"
    storage_ref = seed_message(db_path)
    fake = FakeImap()
    configure_mailbox(monkeypatch, fake)

    result = mailbox_module.move_messages(
        config_path=tmp_path / "accounts.toml",
        db_path=db_path,
        account_id="work",
        destination="trash",
        storage_refs=[storage_ref],
        apply=False,
    )

    assert result["ok"] is True
    assert result["moved"] == 0
    assert fake.move_called is False
    conn = connect_email_store(db_path)
    try:
        assert get_message_by_storage_ref(conn, storage_ref) is not None
    finally:
        conn.close()


def test_move_apply_verifies_target_and_records_local_receipt(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "mail.sqlite"
    storage_ref = seed_message(db_path)
    fake = FakeImap()
    configure_mailbox(monkeypatch, fake)

    result = mailbox_module.move_messages(
        config_path=tmp_path / "accounts.toml",
        db_path=db_path,
        account_id="work",
        destination="trash",
        storage_refs=[storage_ref],
        apply=True,
    )

    assert result["ok"] is True
    assert result["moved"] == 1
    assert fake.move_called is True
    message_receipt = result["messages"][0]
    assert message_receipt["status"] == "moved"
    assert message_receipt["method"] == "himalaya_uid_move"
    assert str(message_receipt["operation_ref"]).startswith("mailbox-operation://")
    assert fake.mailboxes["INBOX"] == {}
    assert fake.mailboxes["Trash"][101] == RAW_MESSAGE
    conn = connect_email_store(db_path)
    try:
        assert get_message_by_storage_ref(conn, storage_ref)["present"] is False
        operation = conn.execute(
            "SELECT destination_folder FROM mailbox_operations WHERE storage_ref=?",
            (storage_ref,),
        ).fetchone()
        assert operation == ("Trash",)
    finally:
        conn.close()


def test_move_preflight_hash_mismatch_performs_no_remote_write(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "mail.sqlite"
    storage_ref = seed_message(db_path)
    fake = FakeImap()
    fake.mailboxes["INBOX"][7] = RAW_MESSAGE + b" changed"
    configure_mailbox(monkeypatch, fake)

    result = mailbox_module.move_messages(
        config_path=tmp_path / "accounts.toml",
        db_path=db_path,
        account_id="work",
        destination="trash",
        storage_refs=[storage_ref],
        apply=True,
    )

    assert result["ok"] is False
    assert result["moved"] == 0
    assert fake.move_called is False
    assert result["messages"][0]["error"]["code"] == "source_hash_mismatch"


def test_resolve_move_target_gmail_noselect_archive_uses_all_mail() -> None:
    mailboxes = [
        mailbox_module.ImapMailbox(name="INBOX", flags=frozenset()),
        mailbox_module.ImapMailbox(
            name="Archive", flags=frozenset({"\\haschildren", "\\noselect"})
        ),
        mailbox_module.ImapMailbox(name="[Gmail]/All Mail", flags=frozenset({"\\all"})),
    ]

    target, gmail_archive = mailbox_module._resolve_move_target(
        destination="archive",
        exact_folder="",
        mailboxes=mailboxes,
        capabilities={"IMAP4REV1", "X-GM-EXT-1"},
    )

    assert target == "[Gmail]/All Mail"
    assert gmail_archive is True


def test_gmail_archive_ignores_selectable_custom_archive() -> None:
    mailboxes = [
        mailbox_module.ImapMailbox(name="Archive", flags=frozenset()),
        mailbox_module.ImapMailbox(name="[Gmail]/All Mail", flags=frozenset({"\\all"})),
    ]

    target, gmail_archive = mailbox_module._resolve_move_target(
        destination="archive",
        exact_folder="",
        mailboxes=mailboxes,
        capabilities={"X-GM-EXT-1"},
    )

    assert target == "[Gmail]/All Mail"
    assert gmail_archive is True


def test_resolve_exact_folder_requires_existing_selectable_name() -> None:
    mailboxes = [
        mailbox_module.ImapMailbox(name="Bill Finance", flags=frozenset()),
        mailbox_module.ImapMailbox(name="Archive", flags=frozenset({"\\noselect"})),
    ]

    assert mailbox_module._resolve_exact_folder("Bill Finance", mailboxes) == "Bill Finance"
    with pytest.raises(mailbox_module.MailboxMoveFailure) as missing:
        mailbox_module._resolve_exact_folder("Bill Orders", mailboxes)
    assert missing.value.code == "destination_missing"
    with pytest.raises(mailbox_module.MailboxMoveFailure) as unselectable:
        mailbox_module._resolve_exact_folder("Archive", mailboxes)
    assert unselectable.value.code == "destination_not_selectable"


def test_gmail_archive_removes_inbox_label_and_records_receipt(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "mail.sqlite"
    storage_ref = seed_message(db_path)
    fake = FakeGmailImap()
    configure_mailbox(monkeypatch, fake)

    result = mailbox_module.move_messages(
        config_path=tmp_path / "accounts.toml",
        db_path=db_path,
        account_id="work",
        destination="archive",
        storage_refs=[storage_ref],
        apply=True,
    )

    assert result["ok"] is True
    assert result["moved"] == 1
    assert result["destination_folder"] == "[Gmail]/All Mail"
    message_receipt = result["messages"][0]
    assert message_receipt["status"] == "moved"
    assert message_receipt["method"] == "uid_move_to_all_mail"
    assert fake.mailboxes["INBOX"] == {}
    assert 101 in fake.mailboxes["[Gmail]/All Mail"]
    conn = connect_email_store(db_path)
    try:
        assert get_message_by_storage_ref(conn, storage_ref)["present"] is False
        operation = conn.execute(
            "SELECT destination_folder, method FROM mailbox_operations WHERE storage_ref=?",
            (storage_ref,),
        ).fetchone()
        assert operation == ("[Gmail]/All Mail", "uid_move_to_all_mail")
    finally:
        conn.close()


def test_gmail_archive_dry_run_never_touches_the_mailbox(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "mail.sqlite"
    storage_ref = seed_message(db_path)
    fake = FakeGmailImap()
    configure_mailbox(monkeypatch, fake)

    result = mailbox_module.move_messages(
        config_path=tmp_path / "accounts.toml",
        db_path=db_path,
        account_id="work",
        destination="archive",
        storage_refs=[storage_ref],
        apply=False,
    )

    assert result["ok"] is True
    assert result["ready"] == 1
    assert result["moved"] == 0
    assert 7 in fake.mailboxes["INBOX"]


def test_gmail_archive_requires_matching_all_mail_copy_before_write(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "mail.sqlite"
    storage_ref = seed_message(db_path)
    fake = FakeGmailImap()
    fake.mailboxes["[Gmail]/All Mail"].clear()
    configure_mailbox(monkeypatch, fake)

    result = mailbox_module.move_messages(
        config_path=tmp_path / "accounts.toml",
        db_path=db_path,
        account_id="work",
        destination="archive",
        storage_refs=[storage_ref],
        apply=True,
    )

    assert result["ok"] is False
    assert result["moved"] == 0
    assert result["messages"][0]["error"]["code"] == "all_mail_copy_missing"
    assert 7 in fake.mailboxes["INBOX"]


def test_move_to_exact_existing_folder(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "mail.sqlite"
    storage_ref = seed_message(db_path)
    fake = FakeFolderImap()
    configure_mailbox(monkeypatch, fake)

    result = mailbox_module.move_messages(
        config_path=tmp_path / "accounts.toml",
        db_path=db_path,
        account_id="work",
        destination="",
        exact_folder="Bill Finance",
        storage_refs=[storage_ref],
        apply=True,
    )

    assert result["ok"] is True
    assert result["moved"] == 1
    assert result["destination"] == "Bill Finance"
    assert result["destination_folder"] == "Bill Finance"
    assert fake.mailboxes["INBOX"] == {}
    assert fake.mailboxes["Bill Finance"][101] == RAW_MESSAGE


def test_move_requires_exactly_one_target(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "mail.sqlite"
    storage_ref = seed_message(db_path)
    fake = FakeImap()
    configure_mailbox(monkeypatch, fake)

    with pytest.raises(ValueError):
        mailbox_module.move_messages(
            config_path=tmp_path / "accounts.toml",
            db_path=db_path,
            account_id="work",
            destination="archive",
            exact_folder="Bill Finance",
            storage_refs=[storage_ref],
            apply=False,
        )

    with pytest.raises(ValueError):
        mailbox_module.move_messages(
            config_path=tmp_path / "accounts.toml", db_path=db_path,
            account_id="work", destination="", exact_folder="",
            storage_refs=[storage_ref], apply=False)


def test_uidplus_only_native_move_verifies_copy_before_exact_removal(monkeypatch, tmp_path):
    ref = seed_message(tmp_path / "mail.sqlite")
    fake = FakeImap(capabilities=b"IMAP4rev1 UIDPLUS")
    configure_mailbox(monkeypatch, fake)
    result = mailbox_module.move_messages(config_path=tmp_path / "accounts.toml",
        db_path=tmp_path / "mail.sqlite", account_id="work", destination="trash",
        storage_refs=[ref], apply=True)
    assert result["ok"] and result["moved"] == 1
    assert result["messages"][0]["method"] == "himalaya_uid_copy_uid_expunge"
    assert fake.mailboxes["Trash"][101] == RAW_MESSAGE and not fake.mailboxes["INBOX"]


def test_uidplus_copy_without_verified_target_preserves_source(monkeypatch, tmp_path):
    ref = seed_message(tmp_path / "mail.sqlite")
    fake = FakeImap(capabilities=b"IMAP4rev1 UIDPLUS")
    configure_mailbox(monkeypatch, fake)
    monkeypatch.setattr(fake, "copy", lambda *a, **kw: None)
    result = mailbox_module.move_messages(config_path=tmp_path / "accounts.toml",
        db_path=tmp_path / "mail.sqlite", account_id="work", destination="trash",
        storage_refs=[ref], apply=True)
    assert not result["ok"] and result["messages"][0]["remote_state"] == "unknown"
    assert fake.mailboxes["INBOX"][7] == RAW_MESSAGE


def test_folder_epoch_change_refuses_native_move(monkeypatch, tmp_path):
    ref = seed_message(tmp_path / "mail.sqlite")
    fake = FakeImap()
    configure_mailbox(monkeypatch, fake)
    monkeypatch.setattr(fake, "status", lambda folder: {"uidvalidity": 2, "count": 1})
    result = mailbox_module.move_messages(config_path=tmp_path / "accounts.toml",
        db_path=tmp_path / "mail.sqlite", account_id="work", destination="trash",
        storage_refs=[ref], apply=True)
    assert not result["ok"] and not fake.move_called
    assert result["messages"][0]["error"]["code"] == "source_epoch_changed"
