import json

import pytest

from codex_mail_workbench import sync
from codex_mail_workbench.config import CredentialSecret, MailAccount, MailEndpoint
from codex_mail_workbench.store import (
    connect_email_store, fetch_raw_email_by_storage_ref, list_messages,
    folder_status, get_message_by_storage_ref,
)


class FakeImap:
    def __init__(self):
        self.messages = {i: f"Subject: message {i}\r\nMessage-ID: <{i}@test>\r\n\r\nbody {i}".encode() for i in range(1, 4)}
        self.epoch, self.bad_search = 10, False
        self.fail, self.requested = set(), []

    def folders(self):
        return [{"name": "INBOX", "flags": frozenset()}]

    def status(self, folder):
        return {"uidvalidity": self.epoch, "count": len(self.messages)}

    def ids(self, folder):
        return [] if self.bad_search else list(self.messages)

    def metadata(self, folder, uids):
        return {uid: {"size": len(self.messages[uid])} for uid in uids if uid not in self.fail}

    def mime(self, uid, *, folder):
        self.requested.append(uid)
        return self.messages[uid]

    def fetch_bodies(self, folder, uids):
        return {uid: self.mime(uid, folder=folder) for uid in uids}


@pytest.fixture
def environment(monkeypatch, tmp_path):
    account = MailAccount(account_id="work", email="work@example.test",
        imap=MailEndpoint(host="imap.example.test", port=993, security="ssl",
                          username="work@example.test", credential_ref="work-imap"),
        include_folders=["INBOX"], exclude_folders=[])
    client = FakeImap()
    monkeypatch.setattr(sync, "load_account", lambda *a: account)
    monkeypatch.setattr(sync, "Himalaya", lambda *a, **k: client)
    kwargs = dict(config_path=tmp_path / "accounts.toml", db_path=tmp_path / "mail.sqlite",
                  state_dir=tmp_path / "sync-state", account_id="work")
    return client, kwargs


def test_folder_names():
    assert sync.sanitize_folder_name("Sent Items").startswith("Sent_Items--")
    assert sync.sanitize_folder_name("父/子") != sync.sanitize_folder_name("父/女")
    assert not sync.should_sync_folder("Archive", ["*"], ["Archive"])


def test_history_sync_includes_configured_archive_and_bill_children(environment, monkeypatch):
    client, kwargs = environment
    account = sync.load_account()
    account.include_folders[:] = ['*']
    folders = ['INBOX', 'Archive/Academic-History', 'Bill/Travel', 'Bill Travel', 'Trash']
    monkeypatch.setattr(client, 'folders', lambda: [
        {"name": name, "flags": frozenset()} for name in folders])
    active = sync.sync_account(**kwargs, scope='active')
    assert [f['folder'] for f in active['folders']] == ['INBOX']
    history = sync.sync_account(**kwargs, scope='history')
    assert [f['folder'] for f in history['folders']] == folders[:-1]


def test_active_sync_uses_sent_role_with_legacy_include_and_preserves_folder_identities(environment, monkeypatch):
    client, kwargs = environment
    account = sync.load_account()
    account.include_folders[:] = ["INBOX", "[Gmail]/&XfJT0ZCuTvY-"]
    boxes = [{"name": "INBOX", "flags": frozenset()},
             {"name": "[Gmail]/已发邮件", "flags": frozenset({"\\sent"})},
             {"name": "[Gmail]/所有邮件", "flags": frozenset({"\\all"})}]
    monkeypatch.setattr(client, "folders", lambda: boxes)
    result = sync.sync_account(**kwargs, scope="active")
    assert result["ok"] and [f["folder"] for f in result["folders"]] == ["INBOX", "[Gmail]/已发邮件"]
    conn = connect_email_store(kwargs["db_path"])
    rows = list_messages(conn, scope="active")
    assert len(rows) == 6 and len({row["storage_ref"] for row in rows}) == 6
    conn.close()


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


def test_invalid_empty_snapshot_does_not_invalidate_mail(environment):
    client, kwargs = environment
    sync.sync_account(**kwargs)
    client.bad_search = True
    assert sync.sync_account(**kwargs)["ok"] is False
    conn = connect_email_store(kwargs["db_path"])
    assert len(list_messages(conn)) == 3
    assert folder_status(conn)[0]["complete"] == 0
    conn.close()


def test_sync_budget_returns_partial_scope_instead_of_waiting_forever(environment, monkeypatch):
    client, kwargs = environment
    account = sync.load_account()
    account.include_folders[:] = ["*"]
    folders = ["INBOX", "Archive", "Sent"]

    def slow_list():
        import time
        time.sleep(0.06)
        return [{"name": name, "flags": frozenset()} for name in folders]

    monkeypatch.setattr(client, "folders", slow_list)
    result = sync.sync_account(**kwargs, max_duration_sec=0.05, scope="all")

    assert result["ok"] is False
    assert result["timed_out"] is True
    assert result["next_folders"] == folders
    assert result["folders"][0]["folder"] == "INBOX"


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


def test_native_declared_size_disagreement_requires_identical_reread():
    raw = b"Subject: odd server\r\n\r\nbody"
    class Tool:
        def __init__(self, second):
            self.second, self.reads = second, 0
        def metadata(self, folder, uids):
            return {1: {"size": len(raw) + 2}}
        def mime(self, uid, *, folder):
            self.reads += 1
            return raw if self.reads == 1 else self.second
        def fetch_bodies(self, folder, uids):
            return {uid: self.mime(uid, folder=folder) for uid in uids}
    tool = Tool(raw)
    assert list(sync.fetch_message_batch(tool, [1], folder="INBOX")) == [(1, raw)]
    assert tool.reads == 2
    with pytest.raises(ValueError, match="byte count mismatch"):
        list(sync.fetch_message_batch(Tool(raw[:4]), [1], folder="INBOX"))
