import hashlib
import json

import pytest

from codex_mail_workbench import graph
from codex_mail_workbench.config import GraphEndpoint, MailAccount, add_account, load_accounts_config
from codex_mail_workbench.store import connect_email_store, get_message_by_storage_ref, list_messages


ACCOUNT = MailAccount("test_ms", "pi@example.test", None, ["*"], [],
                      "microsoft_graph", GraphEndpoint("app-test", "keychain.test.oauth"))
RAW = b"From: A <a@example.test>\r\nTo: pi@example.test\r\nMessage-ID: <one@example.test>\r\nSubject: Test correspondence\r\nDate: Mon, 12 Oct 2026 10:00:00 +0800\r\n\r\nEvidence body.\r\n"
INBOX = {"id": "inbox-id", "displayName": "Localized inbox", "path": "INBOX", "totalItemCount": 1}
ARCHIVE = {"id": "archive-id", "displayName": "Archive", "path": "Archive", "totalItemCount": 0}


class FakeGraph:
    responses = []
    calls = []
    location = "inbox-id"
    fail_move = False
    scopes = {"mail.readwrite"}

    def __init__(self, account, **kwargs):
        self.account = account
        self.deadline = kwargs.get("deadline", 10**20)

    def identity(self):
        return {"verified": True}

    def _access_token(self):
        return "test-token"

    def folders(self):
        return [INBOX.copy(), ARCHIVE.copy()]

    def metadata(self, remote_id):
        return {"id": remote_id, "parentFolderId": self.location,
                "changeKey": "ck1", "internetMessageId": "<one@example.test>"}

    def mime(self, remote_id):
        self.calls.append(("mime", remote_id))
        return RAW

    def request(self, path, **kwargs):
        self.calls.append((path, kwargs))
        if path.endswith("/move"):
            if self.fail_move:
                raise graph.GraphError(0, "network_unavailable")
            type(self).location = kwargs["data"]["destinationId"]
            return {"id": "immutable-A"}
        return self.responses.pop(0)

    def pages(self, path):
        response = self.request(path)
        yield from response.get("value", [])


@pytest.fixture
def fake(monkeypatch):
    FakeGraph.responses, FakeGraph.calls = [], []
    FakeGraph.location, FakeGraph.fail_move = "inbox-id", False
    FakeGraph.scopes = {"mail.readwrite"}
    monkeypatch.setattr(graph, "GraphClient", FakeGraph)
    return FakeGraph


def sync(tmp_path, **kwargs):
    return graph.sync_graph_account(account=ACCOUNT, db_path=tmp_path / "mail.db",
                                    state_dir=tmp_path / "sync", scope="active", **kwargs)


def baseline(fake, tmp_path):
    fake.responses = [{"value": [{"id": "immutable-A", "changeKey": "ck1"}],
                   "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta/inbox"}]
    assert sync(tmp_path)["ok"]
    conn = connect_email_store(tmp_path / "mail.db")
    ref = list_messages(conn, account_ids=["test_ms"])[0]["storage_ref"]
    conn.close()
    return ref


def test_config_graph_add_preserves_existing_bytes(tmp_path):
    path = tmp_path / "accounts.toml"
    add_account(path, account_id="imap", email="a@example.test", host="imap.example.test",
                port=993, security="ssl", username="a", credential_ref="ref",
                include_folders=["INBOX"], exclude_folders=[])
    original = path.read_text()
    add_account(path, account_id=ACCOUNT.account_id, email=ACCOUNT.email, host="", port=993,
                security="ssl", username="", credential_ref=ACCOUNT.graph.credential_ref,
                include_folders=["*"], exclude_folders=[], provider="microsoft_graph", client_id="app-test")
    assert path.read_text().startswith(original.rstrip())
    loaded = load_accounts_config(path)
    assert loaded["imap"].imap.host == "imap.example.test"
    assert loaded["test_ms"].imap is None
    assert loaded["test_ms"].graph.client_id == "app-test"
    assert "access_token" not in path.read_text()


def test_sync_delta_is_used_and_tombstone_preserves_raw(fake, tmp_path):
    ref = baseline(fake, tmp_path)
    fake.responses = [{"value": [{"id": "immutable-A", "@removed": {"reason": "deleted"}}],
                   "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta/next"}]
    assert sync(tmp_path)["ok"]
    assert any(c[0] == "https://graph.microsoft.com/v1.0/delta/inbox" for c in fake.calls)
    conn = connect_email_store(tmp_path / "mail.db")
    assert not get_message_by_storage_ref(conn, ref)["present"]
    assert conn.execute("SELECT count(*) FROM email_blobs").fetchone()[0] == 1
    conn.close()


def test_limited_page_replay_retries_holes_without_duplicate_fetch(fake, tmp_path):
    page = {"value": [{"id": "immutable-A", "changeKey": "ck1"},
                       {"id": "immutable-B", "changeKey": "ck1"}],
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta/done"}
    fake.responses = [page]
    first = sync(tmp_path, limit_per_folder=1)
    assert not first["ok"] and first["new_messages"] == 1
    fake.responses = [page]
    assert sync(tmp_path, limit_per_folder=1)["ok"]
    assert [c for c in fake.calls if c[0] == "mime"] == [("mime", "immutable-A"), ("mime", "immutable-B")]


def test_dry_run_never_fetches_or_creates_store(fake, tmp_path):
    assert sync(tmp_path, dry_run=True)["ok"]
    assert not fake.calls and not (tmp_path / "mail.db").exists()


def test_delta_expiry_rebuild_removes_stale_members_only_when_complete(fake, tmp_path, monkeypatch):
    ref = baseline(fake, tmp_path)
    old = FakeGraph.request
    expired = [True]
    def request(self, path, **kwargs):
        if expired.pop() if expired else False:
            raise graph.GraphError(410, "SyncStateNotFound")
        return old(self, path, **kwargs)
    monkeypatch.setattr(FakeGraph, "request", request)
    fake.responses = [{"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta/rebuilt"}]
    assert sync(tmp_path)["ok"]
    conn = connect_email_store(tmp_path / "mail.db")
    assert not get_message_by_storage_ref(conn, ref)["present"]
    conn.close()


def test_graph_refuses_token_forwarding_and_mismatched_mailbox(monkeypatch):
    client = graph.GraphClient(ACCOUNT, token="fake")
    with pytest.raises(ValueError, match="off-origin"):
        client.request("https://evil.example.test/v1.0/stolen")
    monkeypatch.setattr(client, "request", lambda path: {"mail": "other@example.test"})
    with pytest.raises(ValueError, match="does not match"):
        client.identity()


def test_move_exact_identity_and_source_target_readback(fake, tmp_path):
    ref = baseline(fake, tmp_path)
    preview = graph.move_graph_messages(account=ACCOUNT, db_path=tmp_path / "mail.db",
                                       destination="archive", storage_refs=[ref], apply=False)
    assert preview["ready"] == 1 and preview["moved"] == 0
    fake.responses = [{"value": []}]  # Exact source-folder absence after move.
    result = graph.move_graph_messages(account=ACCOUNT, db_path=tmp_path / "mail.db",
                                      destination="archive", storage_refs=[ref], apply=True)
    assert result["moved"] == 1 and result["ok"]
    conn = connect_email_store(tmp_path / "mail.db")
    assert not get_message_by_storage_ref(conn, ref)["present"]
    target = get_message_by_storage_ref(conn, result["messages"][0]["destination_storage_ref"])
    assert target["present"] and target["folder"] == "Archive"
    assert target["raw_sha256"] == hashlib.sha256(RAW).hexdigest()
    conn.close()


def test_unknown_move_is_not_retried(fake, tmp_path):
    ref = baseline(fake, tmp_path)
    fake.fail_move = True
    result = graph.move_graph_messages(account=ACCOUNT, db_path=tmp_path / "mail.db",
                                      destination="archive", storage_refs=[ref], apply=True)
    assert not result["ok"] and result["messages"][0]["status"] == "unknown"
    assert len([c for c in fake.calls if c[0].endswith("/move")]) == 1


def test_move_accepts_new_provider_identity_but_keeps_old_reference_historical(fake, tmp_path, monkeypatch):
    ref = baseline(fake, tmp_path)
    original = FakeGraph.request
    def request(self, path, **kwargs):
        result = original(self, path, **kwargs)
        return {"id": "immutable-new"} if path.endswith("/move") else result
    monkeypatch.setattr(FakeGraph, "request", request)
    fake.responses = [{"value": []}]
    result = graph.move_graph_messages(account=ACCOUNT, db_path=tmp_path / "mail.db",
                                      destination="archive", storage_refs=[ref], apply=True)
    assert result["ok"] and result["moved"] == 1
    conn = connect_email_store(tmp_path / "mail.db")
    assert not get_message_by_storage_ref(conn, ref)["present"]
    target = get_message_by_storage_ref(conn, result["messages"][0]["destination_storage_ref"])
    assert target["present"] and target["folder"] == "Archive"
    assert conn.execute("SELECT remote_id FROM graph_message_ids WHERE uid=?", (target["uid"],)).fetchone()[0] == "immutable-new"
    conn.close()


def test_reconcile_missing_old_identity_matches_only_exact_target_and_never_moves(fake, tmp_path, monkeypatch):
    ref = baseline(fake, tmp_path)
    original = FakeGraph.metadata
    def metadata(self, remote_id):
        if remote_id == "immutable-A":
            raise graph.GraphError(404, "ErrorItemNotFound")
        return original(self, remote_id)
    monkeypatch.setattr(FakeGraph, "metadata", metadata)
    fake.location = "archive-id"
    fake.responses = [{"value": [{"id": "immutable-new", "parentFolderId": "archive-id"}]}, {"value": []}]
    result = graph.reconcile_graph_moves(account=ACCOUNT, db_path=tmp_path / "mail.db",
                                         destination="archive", storage_refs=[ref])
    assert result["ok"] and result["messages"][0]["status"] == "moved"
    assert result["remote_writes"] == 0 and not any(c[0].endswith("/move") for c in fake.calls)
    conn = connect_email_store(tmp_path / "mail.db")
    assert not get_message_by_storage_ref(conn, ref)["present"]
    target = get_message_by_storage_ref(conn, result["messages"][0]["destination_storage_ref"])
    assert target["present"] and target["folder"] == "Archive"
    conn.close()
    fake.responses = [{"value": [{"id": "one"}, {"id": "two"}]}]
    result = graph.reconcile_graph_moves(account=ACCOUNT, db_path=tmp_path / "mail.db",
                                         destination="archive", storage_refs=[ref])
    assert not result["ok"] and result["messages"][0]["status"] == "unknown"


def test_unknown_move_reconciliation_only_reads_and_records_proof(fake, tmp_path):
    ref = baseline(fake, tmp_path)
    fake.location = "archive-id"  # Provider already performed the earlier move.
    fake.responses = [{"value": []}]
    result = graph.reconcile_graph_moves(account=ACCOUNT, db_path=tmp_path / "mail.db",
                                         destination="archive", storage_refs=[ref])
    assert result["ok"] and result["remote_writes"] == 0
    assert result["messages"][0]["status"] == "moved"
    assert not any(c[0].endswith("/move") for c in fake.calls)
    fake.responses = [{"value": []}]
    again = graph.reconcile_graph_moves(account=ACCOUNT, db_path=tmp_path / "mail.db",
                                        destination="archive", storage_refs=[ref])
    assert again["messages"][0]["operation_ref"] == result["messages"][0]["operation_ref"]


def test_token_refresh_keeps_rotated_credentials_in_keychain_only(monkeypatch, tmp_path):
    from codex_mail_workbench import paths
    monkeypatch.setattr(paths, "default_state_dir", lambda: tmp_path)
    token = {"client_id": "app-test", "email": ACCOUNT.email, "expires_at": 0,
             "access_token": "old-access", "refresh_token": "old-refresh", "scope": "Mail.Read User.Read"}
    saved = []
    monkeypatch.setattr(graph, "keychain_get_secret", lambda ref: json.dumps(token))
    monkeypatch.setattr(graph, "_token_request", lambda account, parameters:
                        {"access_token": "fresh-access", "refresh_token": "rotated-refresh"})
    monkeypatch.setattr(graph, "_save_token", lambda account, value: saved.append(value))
    assert graph.GraphClient(ACCOUNT)._access_token() == "fresh-access"
    assert saved[0]["refresh_token"] == "rotated-refresh"
    assert not any("refresh" in p.read_text() for p in tmp_path.iterdir())


def test_refresh_lock_cannot_outlive_sync_budget(monkeypatch, tmp_path):
    import fcntl
    import time
    from codex_mail_workbench import paths
    monkeypatch.setattr(paths, "default_state_dir", lambda: tmp_path)
    with (tmp_path / (ACCOUNT.account_id + ".oauth.lock")).open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(graph.GraphError) as error:
            graph.GraphClient(ACCOUNT, deadline=time.monotonic() + 0.02)._access_token()
    assert error.value.code == "oauth_refresh_busy"
