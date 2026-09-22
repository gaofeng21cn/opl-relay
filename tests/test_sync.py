import json

import pytest

from codex_mail_workbench import sync
from codex_mail_workbench.config import MailAccount, MailEndpoint
from codex_mail_workbench.store import (
    connect_email_store, fetch_raw_email_by_storage_ref, list_messages,
    folder_status, get_message_by_storage_ref,
)


class FakeImap:
    def __init__(self):
        self.messages = {i: f"Subject: message {i}\r\nMessage-ID: <{i}@test>\r\n\r\nbody {i}".encode() for i in range(1, 4)}
        self.epoch = 10
        self.fail = set()
        self.bad_search = False
        self.requested = []

    def login(self, *args):
        pass

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

    def select(self, folder, readonly=True):
        assert readonly is True
        return "OK", [str(len(self.messages)).encode()]

    def response(self, name):
        assert name == "UIDVALIDITY"
        return name, [str(self.epoch).encode()]

    def uid(self, command, *args):
        if command == "search":
            return "OK", [b"" if self.bad_search else b" ".join(str(u).encode() for u in self.messages)]
        assert command == "fetch"
        uids = list(map(int, args[0].split(",")))
        if args[1] == "(UID RFC822.SIZE)":
            return "OK", [f"1 (UID {u} RFC822.SIZE {len(self.messages[u])})".encode() for u in uids]
        assert args[1] == "(UID BODY.PEEK[])"
        self.requested.extend(uids)
        return "OK", [(f"1 (UID {u} BODY[]".encode(), self.messages[u]) for u in uids if u not in self.fail]

    def logout(self):
        pass


@pytest.fixture
def environment(monkeypatch, tmp_path):
    account = MailAccount(account_id="work", email="work@example.test",
        imap=MailEndpoint(host="imap.example.test", port=993, security="ssl",
                          username="work@example.test", credential_ref="work-imap"),
        include_folders=["INBOX"], exclude_folders=[])
    client = FakeImap()
    monkeypatch.setattr(sync, "load_account", lambda *a: account)
    monkeypatch.setattr(sync, "keychain_get_secret", lambda *a: "fake")
    monkeypatch.setattr(sync, "connect_imap", lambda *a: client)
    kwargs = dict(config_path=tmp_path / "accounts.toml", db_path=tmp_path / "mail.sqlite",
                  state_dir=tmp_path / "sync-state", account_id="work")
    return client, kwargs


def test_folder_names():
    assert sync.sanitize_folder_name("Sent Items") == "Sent_Items"
    assert sync.sanitize_folder_name("父/子") != sync.sanitize_folder_name("父/女")
    assert not sync.should_sync_folder("Archive", ["*"], ["Archive"])


def test_history_sync_includes_configured_archive_and_bill_children(environment, monkeypatch):
    client, kwargs = environment
    account = sync.load_account()
    account.include_folders[:] = ['*']
    folders = ['INBOX', 'Archive/Academic-History', 'Bill/Travel', 'Bill Travel', 'Trash']
    monkeypatch.setattr(client, 'list', lambda: ('OK', [
        f'(\\HasNoChildren) "/" "{name}"'.encode() for name in folders]))
    active = sync.sync_account(**kwargs, scope='active')
    assert [f['folder'] for f in active['folders']] == ['INBOX']
    history = sync.sync_account(**kwargs, scope='history')
    assert [f['folder'] for f in history['folders']] == folders[:-1]


def test_missing_uid_retried_even_after_a_later_success(environment):
    client, kwargs = environment
    client.fail = {2}
    first = sync.sync_account(**kwargs)
    assert first["ok"] is False
    assert first["folders"][0]["remaining"] == 1
    state = json.loads((kwargs["state_dir"] / "work.json").read_text())
    assert state["folders"]["INBOX"]["last_uid_synced"] == 1
    client.fail.clear()
    client.requested.clear()
    assert sync.sync_account(**kwargs)["ok"] is True
    assert client.requested == [2]


def test_limit_resumes_and_recovers_old_cursor_holes(environment):
    client, kwargs = environment
    kwargs["state_dir"].mkdir()
    (kwargs["state_dir"] / "work.json").write_text('{"folders":{"INBOX":{"last_uid_synced":999}}}')
    first = sync.sync_account(**kwargs, limit_per_folder=1)
    assert first["ok"] is False
    assert client.requested == [3]
    client.requested.clear()
    assert sync.sync_account(**kwargs)["ok"] is True
    assert client.requested == [2, 1]


def test_external_move_and_genuine_empty_preserve_references(environment):
    client, kwargs = environment
    sync.sync_account(**kwargs)
    conn = connect_email_store(kwargs["db_path"])
    ref = list_messages(conn)[0]["storage_ref"]
    original = fetch_raw_email_by_storage_ref(conn, ref)
    client.messages.clear()
    assert sync.sync_account(**kwargs)["ok"] is True
    assert list_messages(conn) == []
    assert fetch_raw_email_by_storage_ref(conn, ref) == original
    assert get_message_by_storage_ref(conn, ref)["present"] is False
    assert folder_status(conn)[0]["remote_count"] == 0
    conn.close()


def test_large_message_uses_verified_partial_fetches():
    import re
    raw = b"Subject: large\r\n\r\n" + b"a" * (5 * 1024 * 1024)
    class Chunked:
        def uid(self, command, uid, fields):
            if fields == "(UID RFC822.SIZE)":
                return "OK", [f"1 (UID 1 RFC822.SIZE {len(raw)})".encode()]
            match = re.search(r"<(\d+)\.(\d+)>", fields)
            assert match, "large bodies must not be requested as one literal"
            offset, length = map(int, match.groups())
            return "OK", [(b"1 (UID 1 BODY[]", raw[offset:offset+length])]
    assert list(sync.fetch_message_batch(Chunked(), [1])) == [(1, raw)]


def test_declared_size_disagreement_requires_identical_reread():
    raw = b"Subject: odd server\r\nMessage-ID: <odd@test>\r\n\r\nbody"

    class Server:
        def __init__(self, second_read: bytes) -> None:
            self.second_read = second_read
            self.reads = 0

        def uid(self, command, uid, fields):
            if fields == "(UID RFC822.SIZE)":
                return "OK", [f"1 (UID 1 RFC822.SIZE {len(raw) + 2})".encode()]
            self.reads += 1
            return "OK", [(b"1 (UID 1 BODY[]", raw if self.reads == 1 else self.second_read)]

    agreed = Server(raw)
    assert list(sync.fetch_message_batch(agreed, [1])) == [(1, raw)]
    assert agreed.reads == 2

    truncated = Server(raw[:4])
    with pytest.raises(ValueError, match="byte count mismatch"):
        list(sync.fetch_message_batch(truncated, [1]))


def test_invalid_empty_snapshot_does_not_invalidate_mail(environment):
    client, kwargs = environment
    sync.sync_account(**kwargs)
    client.bad_search = True
    assert sync.sync_account(**kwargs)["ok"] is False
    conn = connect_email_store(kwargs["db_path"])
    assert len(list_messages(conn)) == 3
    assert folder_status(conn)[0]["complete"] == 0
    conn.close()


def test_uidvalidity_reset_retains_old_bytes_and_fetches_reused_uid(environment):
    client, kwargs = environment
    sync.sync_account(**kwargs)
    conn = connect_email_store(kwargs["db_path"])
    refs = {m["storage_ref"]: fetch_raw_email_by_storage_ref(conn, m["storage_ref"]) for m in list_messages(conn)}
    client.epoch = 11
    client.messages = {1: b"Subject: new epoch\r\n\r\nnew content"}
    assert sync.sync_account(**kwargs)["ok"] is True
    assert len(list_messages(conn)) == 1
    assert list_messages(conn)[0]["subject"] == "new epoch"
    for ref, raw in refs.items():
        assert fetch_raw_email_by_storage_ref(conn, ref) == raw
    conn.close()
