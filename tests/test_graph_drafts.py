import base64

import pytest

from codex_mail_workbench import graph_drafts as gd, server_drafts as sd
from codex_mail_workbench.config import GraphEndpoint, MailAccount


ACCOUNT = MailAccount("test_ms", "owner@example.test", None, ["*"], [],
                      "microsoft_graph", GraphEndpoint("test-app", "test-ref"))


class FakeClient:
    raw = None
    creates = 0
    timeout_after_create = False
    scopes = {"mail.readwrite"}

    def __init__(self, account):
        pass

    def identity(self):
        pass

    def pages(self, path):
        return [{"id": "immutable-draft"}] if self.raw else []

    def request(self, path, method="GET", data=None):
        if path.startswith("/me/mailFolders/drafts"):
            return {"id": "drafts-id"}
        if method == "POST":
            type(self).raw = base64.b64decode(data)
            type(self).creates += 1
            if self.timeout_after_create:
                raise TimeoutError("unknown create")
            return {"id": "immutable-draft"}
        return {"id": "immutable-draft", "isDraft": True, "parentFolderId": "drafts-id"}

    def mime(self, remote_id):
        return self.raw


@pytest.fixture
def service(monkeypatch, tmp_path):
    FakeClient.raw, FakeClient.creates, FakeClient.timeout_after_create = None, 0, False
    monkeypatch.setattr(gd, "GraphClient", FakeClient)
    monkeypatch.setattr(sd, "load_accounts_config", lambda path: {"test_ms": ACCOUNT})
    return dict(config_path=tmp_path / "accounts.toml", db_path=tmp_path / "mail.db",
                ledger_path=tmp_path / "drafts.db", account_id="test_ms", request_id="test",
                body="Hello\n\nControlled draft.", signature="Best regards,\nOwner",
                to=["owner@example.test"], subject="Relay self-check")


def test_graph_draft_preview_apply_and_replay(service):
    assert sd.server_draft(**service)["state"] == "prepared"
    assert FakeClient.creates == 0
    result = sd.server_draft(**service, apply=True)
    assert result["server_verified"] and not result["send_allowed"]
    assert sd.server_draft(**service, apply=True)["remote_id"] == result["remote_id"]
    assert FakeClient.creates == 1


def test_graph_draft_unknown_create_is_reconciled_not_recreated(service):
    FakeClient.timeout_after_create = True
    with pytest.raises(TimeoutError):
        sd.server_draft(**service, apply=True)
    assert sd.server_draft(**service, inspect=True)["server_verified"]
    assert FakeClient.creates == 1


def test_graph_draft_mobile_edits_are_preserved(service):
    sd.server_draft(**service, apply=True)
    FakeClient.raw = FakeClient.raw.replace(b"Controlled draft.", b"My personal edits.")
    with pytest.raises(ValueError, match="differs"):
        sd.server_draft(**service, inspect=True)
    assert b"My personal edits." in FakeClient.raw and FakeClient.creates == 1


def test_graph_draft_no_send_or_unscoped_cleanup(service, tmp_path):
    result = sd.reconcile_server_drafts(config_path=tmp_path / "accounts.toml",
                                       ledger_path=tmp_path / "empty.db", account_id="test_ms", apply=True)
    assert result["checked"] == 0 and result["cleaned"] == 0


def test_graph_cleanup_verifies_returned_move_identity_and_exact_source_absence(service, monkeypatch):
    sd.server_draft(**service, apply=True)
    original = FakeClient.raw
    sent = original.replace(b"Controlled draft.", b"User-approved sent reply.")
    calls = []
    def request(self, path, method="GET", data=None):
        calls.append((path, method))
        if path.startswith("/me/mailFolders/drafts"):
            return {"id": "drafts-id"}
        if path.startswith("/me/mailFolders/deleteditems"):
            return {"id": "trash-id"}
        if method == "POST":
            assert path.endswith("/immutable-draft/move")
            return {"id": "new-target-id"}
        return {"isDraft": True, "parentFolderId": "drafts-id"}
    def pages(self, path):
        if path.startswith("/me/mailFolders/sentitems/messages"):
            return [{"id": "sent-id", "subject": "Relay self-check"}]
        assert "internetMessageId" in path and path.startswith("/me/mailFolders/drafts/messages")
        return []
    def metadata(self, remote_id):
        assert remote_id == "new-target-id"
        return {"parentFolderId": "trash-id"}
    monkeypatch.setattr(FakeClient, "request", request)
    monkeypatch.setattr(FakeClient, "pages", pages)
    monkeypatch.setattr(FakeClient, "metadata", metadata, raising=False)
    monkeypatch.setattr(FakeClient, "mime", lambda self, rid: sent if rid == "sent-id" else original)
    result = sd.reconcile_server_drafts(config_path=service["config_path"], ledger_path=service["ledger_path"],
                                       account_id="test_ms", apply=True)
    assert result["cleaned"] == 1
    assert len([c for c in calls if c[1] == "POST"]) == 1


def test_exchange_charset_head_is_accepted_but_mobile_edits_are_not(service):
    sd.server_draft(**service, apply=True)
    original = FakeClient.raw
    FakeClient.raw = original.replace(b"<html><body>",
        b'<html>\r\n<head>\r\n<meta http-equiv="Content-Type" content="text/html; charset=utf-8"></head>\r\n<body>')
    assert sd.server_draft(**service, inspect=True)["server_verified"]
    FakeClient.raw = FakeClient.raw.replace(b"Controlled draft.", b"Personal changed wording.")
    with pytest.raises(ValueError, match="differs"):
        sd.server_draft(**service, inspect=True)
