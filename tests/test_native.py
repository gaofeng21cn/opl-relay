import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_mail_workbench import graph, native, remote_search
from codex_mail_workbench.cli import build_parser
from codex_mail_workbench.config import GraphEndpoint, MailAccount, MailEndpoint
from codex_mail_workbench.store import connect_email_store


ACCOUNT = MailAccount("native_test", "owner@example.test", None, ["INBOX"], [],
                      "microsoft_graph", GraphEndpoint("app", "keychain-ref"), "himalaya")
RAW = (b"From: other@example.test\r\nTo: owner@example.test\r\nSubject: itinerary\r\n"
       b"Date: Mon, 12 Oct 2026 01:00:00 +0000\r\nMessage-ID: <remote@test>\r\n\r\nDDW details")


@pytest.fixture
def native_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(native, "default_state_dir", lambda: tmp_path)
    monkeypatch.setattr(native, "load_toml", lambda path: {"himalaya": {"executable": "/usr/bin/true"}})
    return native.Himalaya(ACCOUNT, token="private-test-token")


def test_native_credentials_never_enter_config_argv_or_errors(native_tool, monkeypatch, tmp_path):
    config_paths = []
    def run(argv, **kw):
        config = Path(argv[argv.index("--config") + 1])
        config_paths.append(config)
        assert "private-test-token" not in config.read_text()
        assert "private-test-token" not in repr(argv)
        assert kw["env"]["OPL_MAIL_BACKEND_SECRET"] == "private-test-token"
        return SimpleNamespace(returncode=1, stdout=b"private-test-token", stderr=b"private-test-token")
    monkeypatch.setattr(native.subprocess, "run", run)
    with pytest.raises(native.NativeError) as error:
        native_tool.mime("immutable-id")
    assert "private-test-token" not in str(error.value)
    assert not any(p.exists() for p in config_paths)


def test_native_create_receipt_and_unknown_result_never_retry(native_tool, monkeypatch):
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        assert kw["input"] == RAW and "--send" not in argv
        return SimpleNamespace(returncode=0,
            stdout=b'{"message":"Microsoft Graph draft `mutable-id` successfully created"}', stderr=b"")
    monkeypatch.setattr(native.subprocess, "run", run)
    assert native_tool.create_draft(RAW) == {"id": "mutable-id"}
    assert len(calls) == 1
    def timeout(argv, **kw):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 1)
    monkeypatch.setattr(native.subprocess, "run", timeout)
    with pytest.raises(native.NativeError, match="outcome_unknown"):
        native_tool.create_draft(RAW)
    assert len(calls) == 2


def test_native_check_requires_actual_backend_success(native_tool, monkeypatch):
    monkeypatch.setattr(native_tool, "_run", lambda argv: {"backends": [{"ok": False}]})
    with pytest.raises(native.NativeError, match="connection_check_failed"):
        native_tool.check()


def test_native_read_can_use_whole_budget_but_writes_remain_bounded(native_tool, monkeypatch):
    monkeypatch.setattr(native.time, "monotonic", lambda: 10)
    native_tool.deadline = 210
    timeouts = []
    def run(argv, **kw):
        timeouts.append(kw["timeout"])
        return SimpleNamespace(returncode=0, stdout=b'{}', stderr=b'')
    monkeypatch.setattr(native.subprocess, "run", run)
    native_tool._run(["msgraph", "messages", "get", "id", "--raw"], raw=True)
    native_tool._run(["msgraph", "messages", "create"], body=RAW)
    assert timeouts == [200, 60]


def test_native_legacy_imap_names_are_normalized_and_batch_octets_are_preserved(native_tool, monkeypatch):
    import base64
    native_tool.account = replace(ACCOUNT, graph=None, provider="imap",
        imap=MailEndpoint("imap.example.test", 993, "ssl", "owner@example.test", "test"))
    monkeypatch.setattr(native, "keychain_get_secret", lambda *a, **kw: "test-secret")
    raw = b"Subject: bytes\r\n\r\n\xff\x00\xfe"
    def run(argv, **kw):
        assert argv[-3:] == ["--mailbox", "[Gmail]/已发邮件", "--body"]
        assert argv[argv.index("--mailbox") + 1] == "[Gmail]/已发邮件"
        return SimpleNamespace(returncode=0,
            stdout=('{"messages":[{"uid":9,"body":"' + base64.b64encode(raw).decode() + '"}]}').encode(), stderr=b'')
    monkeypatch.setattr(native.subprocess, "run", run)
    assert native_tool.fetch_bodies("[Gmail]/&XfJT0ZCuTvY-", [9]) == {9: raw}
    with pytest.raises(native.NativeError, match="unexpected_fetch_identity"):
        native_tool.fetch_bodies("[Gmail]/已发邮件", [10])


def test_imap_folder_command_emits_json_without_graph(monkeypatch, capsys):
    import json
    from codex_mail_workbench import cli
    account = replace(ACCOUNT, graph=None, provider="imap")
    monkeypatch.setattr(cli, "load_account", lambda *a: account)
    class Tool:
        def __init__(self, account): pass
        def folders(self):
            return [{"name": "[Gmail]/已发邮件", "flags": frozenset({"\\sent"})}]
    monkeypatch.setattr(native, "Himalaya", Tool)
    assert cli.cmd_account_folders(SimpleNamespace(config="unused", account="native_test", json=True)) == 0
    assert json.loads(capsys.readouterr().out)["folders"][0]["flags"] == ["\\sent"]


def test_native_graph_search_uses_provider_search_and_preserves_identity(native_tool, monkeypatch):
    def run(argv):
        assert argv == ["msgraph", "messages", "list", "--folder", "draft-folder",
                        "--search", '"OPL Relay native verification"', "--top", "6",
                        "--select", "id,internetMessageId"]
        return {"messages": [{"id": "mutable-id", "internetMessageId": "<draft@test>"}]}
    monkeypatch.setattr(native_tool, "_run", run)
    assert native_tool.search("OPL Relay native verification", folder="draft-folder", limit=6) == {
        "envelopes": [{"id": "mutable-id", "message-id": "<draft@test>"}]}


def test_native_graph_move_keeps_returned_identity_and_refuses_ambiguous_receipt(native_tool, monkeypatch):
    monkeypatch.setattr(native_tool, "_run", lambda argv:
                        {"message": "Microsoft Graph message moved to `new-mutable-id`"})
    assert native_tool.move("previous-id", destination="trash") == {"id": "new-mutable-id"}
    monkeypatch.setattr(native_tool, "_run", lambda argv: {"message": "Moved"})
    with pytest.raises(native.NativeError, match="move_outcome_unknown"):
        native_tool.move("previous-id", destination="trash")


def test_graph_native_move_resolves_returned_id_instead_of_missing_previous_id(monkeypatch):
    class Tool:
        def __init__(self, account, **kwargs): pass
        def move(self, remote_id, *, destination):
            assert remote_id == "previous-id" and destination == "trash"
            return {"id": "new-mutable-id"}
    monkeypatch.setattr(native, "Himalaya", Tool)
    client = graph.GraphClient(ACCOUNT)
    monkeypatch.setattr(client, "_access_token", lambda: "test-token")
    def http(url, **kwargs):
        assert kwargs["method"] == "GET" and "new-mutable-id" in url
        assert "previous-id" not in url
        return {"id": "new-immutable-id"}
    monkeypatch.setattr(graph, "_http", http)
    assert client.request("/me/messages/previous-id/move", method="POST", data={"destinationId": "trash"}) == {
        "id": "new-immutable-id"}


def test_graph_native_create_rebinds_immutable_identity(monkeypatch):
    import base64
    class Tool:
        def __init__(self, account, **kwargs):
            assert kwargs["token"] == "test-token"
        def create_draft(self, raw):
            assert raw == RAW
            return {"id": "mutable-id"}
    monkeypatch.setattr(native, "Himalaya", Tool)
    client = graph.GraphClient(ACCOUNT)
    monkeypatch.setattr(client, "_access_token", lambda: "test-token")
    def http(url, **kwargs):
        assert kwargs["method"] == "GET" and "mutable-id" in url
        assert 'ImmutableId' in kwargs["headers"]["Prefer"]
        return {"id": "immutable-id"}
    monkeypatch.setattr(graph, "_http", http)
    assert client.request("/me/messages", method="POST", data=base64.b64encode(RAW))["id"] == "immutable-id"


def test_remote_search_only_caches_matches_and_does_not_claim_snapshot(monkeypatch, tmp_path):
    class Control:
        def __init__(self, account, **kwargs): pass
        def identity(self): pass
        def _access_token(self): return "test-token"
        def folders(self):
            return [{"id": "archive", "path": "Archive"}, {"id": "junk", "path": "Junk Email"}]
        def metadata(self, remote_id): return {"id": "immutable-id", "parentFolderId": "archive"}
    class Tool:
        def __init__(self, account, **kwargs): pass
        def search(self, query, **kwargs):
            assert kwargs["folder"] == "archive"
            return {"envelopes": [{"id": "mutable-id", "message-id": "remote@test"}]}
        def mime(self, remote_id, **kwargs):
            assert remote_id == "immutable-id"
            return RAW
    monkeypatch.setattr(graph, "GraphClient", Control)
    monkeypatch.setattr(remote_search, "Himalaya", Tool)
    result = remote_search.search_remote(account=ACCOUNT, db_path=tmp_path / "mail.db",
        query="DDW", scope="history", since="2026-10-12T08:00:00+08:00",
        until="2026-10-12T10:00:00+08:00")
    assert result["ok"] and len(result["messages"]) == 1
    assert result["folders_searched"] == ["Archive"]
    conn = connect_email_store(tmp_path / "mail.db")
    assert conn.execute("SELECT count(*) FROM folder_snapshots").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM mail_reviews").fetchone()[0] == 0
    conn.close()


def test_sync_default_never_requests_all_archive_folders():
    assert build_parser().parse_args(["sync", "--account", "work"] ).scope == "active"


def test_native_snapshot_uses_uid_objects_and_validates_epoch(native_tool, monkeypatch):
    def run(argv):
        if argv[1] == "search":
            return {"uid_mode": True, "ids": [{"id": 9}, {"id": 14}]}
        return {"uid_validity": 6, "messages": 2}
    monkeypatch.setattr(native_tool, "_run", run)
    assert native_tool.ids("INBOX") == [9, 14]
    assert native_tool.status("INBOX") == {"uidvalidity": 6, "count": 2}
    monkeypatch.setattr(native_tool, "_run", lambda argv: {"uid_validity": None, "messages": 2})
    with pytest.raises(native.NativeError, match="invalid_folder_identity"):
        native_tool.status("INBOX")


def test_native_identity_search_uses_exact_header_and_encoded_folder(native_tool, monkeypatch):
    def run(argv, **kwargs):
        assert argv[:3] == ["imap", "raw", "--"]
        assert argv[3] == 'R1 EXAMINE "[Gmail]/&g0l6Pw-"\r\nR2 UID SEARCH HEADER Message-ID "<reply@test>"'
        assert kwargs["mutation"] is False
        return b'* SEARCH 42\\r\\nR1 OK opened\\r\\nR2 OK found\\r\\n'
    monkeypatch.setattr(native_tool, "_run", run)
    assert native_tool.find_message("[Gmail]/草稿", "<reply@test>") == [42]
    monkeypatch.setattr(native_tool, "_run", lambda *a, **kw:
        b'* SEARCH 42\r\nR1 NO missing\r\nR2 OK found\r\n')
    with pytest.raises(native.NativeError, match="command_rejected"):
        native_tool.find_message("Drafts", "<reply@test>")


@pytest.mark.parametrize("bad", ["Drafts\nR3 EXPUNGE", r"Drafts\nR3 EXPUNGE", r"<id\r@test>"])
def test_native_identity_rejects_actual_and_cli_decoded_newlines(native_tool, monkeypatch, bad):
    monkeypatch.setattr(native_tool, "_run", lambda *a, **kw: pytest.fail("unexpected network request"))
    with pytest.raises(native.NativeError, match="invalid_imap_identity"):
        native_tool.remove_exact(bad, 17)


def test_native_cleanup_is_uid_scoped_and_refuses_non_uidplus(native_tool, monkeypatch):
    calls = []
    monkeypatch.setattr(native_tool, "capabilities", lambda: {"UIDPLUS"})
    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["mutation"] is True
        assert "UID EXPUNGE 17" in argv[-1]
        assert "R3 EXPUNGE" not in argv[-1]
        return b'R1 OK opened\r\nR2 OK stored\r\nR3 OK removed\r\n'
    monkeypatch.setattr(native_tool, "_run", run)
    native_tool.remove_exact("Drafts", 17)
    assert len(calls) == 1
    monkeypatch.setattr(native_tool, "capabilities", lambda: set())
    with pytest.raises(native.NativeError, match="uidplus_required"):
        native_tool.remove_exact("Drafts", 17)
    assert len(calls) == 1


def test_native_raw_cleanup_uses_encoded_mailbox_identity_and_checks_all_completions(native_tool, monkeypatch):
    monkeypatch.setattr(native_tool, "capabilities", lambda: {"UIDPLUS"})
    def run(argv, **kwargs):
        assert 'SELECT "[Gmail]/&g0l6Pw-"' in argv[-1]
        return b'R1 NO missing\r\nR2 OK stored\r\nR3 OK removed\r\n'
    monkeypatch.setattr(native_tool, "_run", run)
    with pytest.raises(native.NativeError, match="command_outcome_unknown"):
        native_tool.remove_exact("[Gmail]/草稿", 17)
