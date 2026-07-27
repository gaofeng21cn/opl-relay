from codex_mail_workbench.sync import sanitize_folder_name, should_sync_folder


def test_sanitize_folder_name_keeps_imap_folder_refs_stable() -> None:
    assert sanitize_folder_name("Sent Items") == "Sent_Items"
    assert sanitize_folder_name("Junk E-mail") == "Junk_E-mail"
    assert sanitize_folder_name("父/子").startswith("folder_")
    assert sanitize_folder_name("父/子") != sanitize_folder_name("父/女")


def test_should_sync_folder_honors_include_and_exclude() -> None:
    assert should_sync_folder("INBOX", ["*"], ["Archive"])
    assert not should_sync_folder("Archive", ["*"], ["Archive"])
    assert should_sync_folder("INBOX", ["INBOX"], [])
    assert not should_sync_folder("Sent Items", ["INBOX"], [])


def test_limited_incremental_sync_advances_only_to_fetched_uid(
    monkeypatch, tmp_path
) -> None:
    from codex_mail_workbench import sync as sync_module
    from codex_mail_workbench.config import MailAccount, MailEndpoint

    account = MailAccount(
        account_id="work",
        email="work@example.test",
        imap=MailEndpoint(
            host="imap.example.test",
            port=993,
            security="ssl",
            username="work@example.test",
            credential_ref="work-imap",
        ),
        include_folders=["INBOX"],
        exclude_folders=[],
    )

    class FakeConn:
        def close(self) -> None:
            pass

    class FakeClient:
        def login(self, username: str, auth_value: str) -> None:
            assert username == "work@example.test"
            assert auth_value == "dummy-auth-value"

        def list(self):
            return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

        def select(self, folder: str, readonly: bool = True):
            assert folder == '"INBOX"'
            assert readonly is True
            return "OK", []

        def uid(self, command: str, *args: str):
            if command == "search":
                return "OK", [b"1 2 3 4 5"]
            if command == "fetch":
                uid = args[0]
                raw = (
                    b"Subject: limited sync\r\n"
                    b"From: a@example.test\r\n"
                    b"To: b@example.test\r\n"
                    b"Message-ID: <limited@example.test>\r\n"
                    b"\r\n"
                    b"body"
                )
                return "OK", [(f"1 (UID {uid} BODY[]".encode(), raw)]
            raise AssertionError(command)

        def logout(self) -> None:
            pass

    monkeypatch.setattr(sync_module, "load_account", lambda path, account_id: account)
    monkeypatch.setattr(sync_module, "keychain_get_secret", lambda ref: "dummy-auth-value")
    monkeypatch.setattr(sync_module, "connect_imap", lambda loaded: FakeClient())
    monkeypatch.setattr(sync_module, "connect_email_store", lambda path: FakeConn())
    monkeypatch.setattr(
        sync_module,
        "upsert_email_message",
        lambda conn, **kwargs: "email-store://work/INBOX/5/hash",
    )

    state_dir = tmp_path / "sync-state"
    state_dir.mkdir()
    (state_dir / "work.json").write_text(
        '{"account_id":"work","folders":{"INBOX":{"last_uid_synced":2}}}',
        encoding="utf-8",
    )

    payload = sync_module.sync_account(
        config_path=tmp_path / "accounts.toml",
        db_path=tmp_path / "mail.sqlite",
        state_dir=state_dir,
        account_id="work",
        mode="incremental",
        limit_per_folder=1,
    )

    state = (state_dir / "work.json").read_text(encoding="utf-8")
    assert payload["new_messages"] == 1
    assert '"last_uid_synced": 3' in state


def test_limited_incremental_sync_does_not_advance_when_fetch_fails(
    monkeypatch, tmp_path
) -> None:
    from codex_mail_workbench import sync as sync_module
    from codex_mail_workbench.config import MailAccount, MailEndpoint

    account = MailAccount(
        account_id="work",
        email="work@example.test",
        imap=MailEndpoint(
            host="imap.example.test",
            port=993,
            security="ssl",
            username="work@example.test",
            credential_ref="work-imap",
        ),
        include_folders=["INBOX"],
        exclude_folders=[],
    )

    class FakeConn:
        def close(self) -> None:
            pass

    class FakeClient:
        def login(self, username: str, auth_value: str) -> None:
            pass

        def list(self):
            return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

        def select(self, folder: str, readonly: bool = True):
            return "OK", []

        def uid(self, command: str, *args: str):
            if command == "search":
                return "OK", [b"1 2 3"]
            if command == "fetch":
                return "NO", []
            raise AssertionError(command)

        def logout(self) -> None:
            pass

    monkeypatch.setattr(sync_module, "load_account", lambda path, account_id: account)
    monkeypatch.setattr(sync_module, "keychain_get_secret", lambda ref: "dummy-auth-value")
    monkeypatch.setattr(sync_module, "connect_imap", lambda loaded: FakeClient())
    monkeypatch.setattr(sync_module, "connect_email_store", lambda path: FakeConn())
    monkeypatch.setattr(
        sync_module,
        "upsert_email_message",
        lambda conn, **kwargs: "email-store://work/INBOX/3/hash",
    )

    state_dir = tmp_path / "sync-state"
    state_dir.mkdir()
    (state_dir / "work.json").write_text(
        '{"account_id":"work","folders":{"INBOX":{"last_uid_synced":2}}}',
        encoding="utf-8",
    )

    payload = sync_module.sync_account(
        config_path=tmp_path / "accounts.toml",
        db_path=tmp_path / "mail.sqlite",
        state_dir=state_dir,
        account_id="work",
        mode="incremental",
        limit_per_folder=1,
    )

    state = (state_dir / "work.json").read_text(encoding="utf-8")
    assert payload["new_messages"] == 0
    assert '"last_uid_synced": 2' in state
