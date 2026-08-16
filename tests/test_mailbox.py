import hashlib
from pathlib import Path
from types import SimpleNamespace

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
    def __init__(self, *, capabilities: bytes = b"IMAP4rev1 UIDPLUS MOVE") -> None:
        self.capability_tokens = capabilities
        self.selected = ""
        self.move_called = False
        self.mailboxes: dict[str, dict[int, bytes]] = {
            "INBOX": {7: RAW_MESSAGE},
            "Trash": {},
        }

    def login(self, username: str, secret: str) -> tuple[str, list[bytes]]:
        assert username == "work@example.test"
        assert secret == "secret"
        return "OK", [b"logged in"]

    def list(self):
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Trash) "/" "Trash"',
        ]

    def capability(self):
        return "OK", [self.capability_tokens]

    def select(self, folder: str, readonly: bool = True):
        assert folder.startswith('"')
        self.selected = folder[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        if self.selected not in self.mailboxes:
            return "NO", []
        return "OK", [str(len(self.mailboxes[self.selected])).encode()]

    def uid(self, command: str, *args: str):
        if command == "fetch":
            uid = int(args[0])
            raw = self.mailboxes[self.selected].get(uid)
            if raw is None:
                return "NO", []
            return "OK", [(f"{uid} (UID {uid} BODY[])".encode(), raw)]
        if command == "search" and args[0] == "UID":
            uid = int(args[1])
            if uid in self.mailboxes[self.selected]:
                return "OK", [str(uid).encode()]
            return "OK", [b""]
        if command == "search" and args[0] == "HEADER":
            message_id = args[2].strip('"')
            matches = [
                str(uid).encode()
                for uid, raw in self.mailboxes[self.selected].items()
                if f"Message-ID: {message_id}".encode() in raw
            ]
            return "OK", [b" ".join(matches)]
        if command == "move":
            self.move_called = True
            assert self.selected == "INBOX"
            uid = int(args[0])
            destination = args[1].strip('"')
            raw = self.mailboxes[self.selected].pop(uid)
            self.mailboxes[destination][101] = raw
            return "OK", [b"[COPYUID 1 7 101]"]
        raise AssertionError((command, args))

    def logout(self):
        return "BYE", [b"logged out"]


def configure_mailbox(monkeypatch, fake: FakeImap) -> None:
    monkeypatch.setattr(mailbox_module, "load_account", lambda path, account_id: account())
    monkeypatch.setattr(mailbox_module, "keychain_get_secret", lambda ref: "secret")
    monkeypatch.setattr(mailbox_module, "connect_imap", lambda loaded: fake)


def test_parse_imap_list_entry_resolves_special_use_folder() -> None:
    entry = mailbox_module.parse_imap_list_entry(
        b'(\\HasNoChildren \\Trash) "/" "Deleted Items"'
    )

    assert entry is not None
    assert entry.name == "Deleted Items"
    assert "\\trash" in entry.flags


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
    assert message_receipt["method"] == "uid_move"
    assert str(message_receipt["operation_ref"]).startswith("mailbox-operation://")
    assert fake.mailboxes["INBOX"] == {}
    assert fake.mailboxes["Trash"][101] == RAW_MESSAGE
    conn = connect_email_store(db_path)
    try:
        assert get_message_by_storage_ref(conn, storage_ref) is None
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
