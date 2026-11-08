import base64
import hashlib
from email.message import EmailMessage
from pathlib import Path

import pytest

from codex_mail_workbench import workbench as wb
from codex_mail_workbench.store import connect_email_store, upsert_email_message


@pytest.fixture
def mailbox(tmp_path):
    profile = tmp_path / "profile"
    state = profile / "data" / "relay"
    state.mkdir(parents=True)
    signature = profile / "templates" / "mail-signature.txt"
    signature.parent.mkdir()
    signature.write_text("Best regards,\nTest Owner\n", encoding="utf-8")
    config = state / "accounts.toml"
    config.write_text('''[[accounts]]
account_id = "work"
email = "owner@example.test"
[accounts.imap]
host = "imap.example.test"
port = 993
security = "ssl"
username = "owner@example.test"
credential_ref = "unused-test-reference"
[[accounts]]
account_id = "other"
email = "other@example.test"
[accounts.imap]
host = "imap.example.test"
port = 993
security = "ssl"
username = "other@example.test"
credential_ref = "unused-test-reference"
''')
    message = EmailMessage()
    message["From"] = "Secretary <secretary@example.test>"
    message["To"] = "Owner <owner@example.test>"
    message["Cc"] = "Professor <professor@example.test>, other@example.test"
    message["Subject"] = "Updated conference programme"
    message["Message-ID"] = "<conference-source@example.test>"
    message["Date"] = "Thu, 05 Nov 2026 08:00:00 +0800"
    message.set_content("Dear Professor,\n\nPlease find the updated programme.\n\nPrevious context.")
    message.add_alternative('<p>Updated programme</p><script>alert("untrusted")</script>', subtype="html")
    message.add_attachment(b"test PDF bytes", maintype="application", subtype="pdf", filename="programme.pdf")
    raw = message.as_bytes()
    db = state / "mail.sqlite"
    conn = connect_email_store(db)
    ref = upsert_email_message(conn, account_id="work", folder="INBOX", folder_slug="INBOX",
                              uid=1, uidvalidity=1, message_id=str(message["Message-ID"]),
                              subject=str(message["Subject"]), sender=str(message["From"]),
                              recipient=str(message["To"]), date_iso="2026-11-05T08:00:00+08:00",
                              raw_sha256=hashlib.sha256(raw).hexdigest(), raw_eml=raw,
                              attachments=[], ingest_ts="2026-11-05T08:01:00+08:00")
    conn.close()
    service = wb.Workbench(config_path=config, db_path=db,
                           ledger_path=state / "drafts.sqlite", profile_path=profile)
    return service, ref


def test_read_and_list_are_scoped_without_creating_a_draft_ledger(mailbox):
    service, ref = mailbox
    before = service.db_path.read_bytes()
    assert len(service.messages(account_id="work")["messages"]) == 1
    assert service.messages(account_id="other")["messages"] == []
    message = service.read(account_id="work", storage_ref=ref)["message"]
    assert message["headers"]["Cc"].startswith("Professor")
    assert message["attachments"][0]["name"] == "programme.pdf"
    assert "Previous context" in message["body_text"]
    assert service.drafts(account_id="work") == {"drafts": []}
    assert service.db_path.read_bytes() == before
    assert not service.ledger_path.exists()
    with pytest.raises(ValueError, match="不属于"):
        service.read(account_id="other", storage_ref=ref)


def test_reply_all_uses_source_headers_and_idempotent_request(mailbox):
    service, ref = mailbox
    first = service.reply(account_id="work", storage_ref=ref, body="Thank you.", request_id="test-reply")["draft"]
    second = service.reply(account_id="work", storage_ref=ref, body="Should not overwrite.", request_id="test-reply")["draft"]
    assert first == second
    assert first["to"] == ["Secretary <secretary@example.test>"]
    assert first["cc"] == ["Professor <professor@example.test>"]
    assert first["source_ref"] == ref
    assert first["signature"] == "Best regards,\nTest Owner"
    assert "Previous context" in first["quote"]


def test_save_revision_conflict_preserves_current_user_edits(mailbox):
    service, ref = mailbox
    service.reply(account_id="work", storage_ref=ref, request_id="reply")
    result = service.save(account_id="work", draft_id="reply", revision=1,
                          body="The user's current edit", attachment_ids=[])
    assert result["draft"]["revision"] == 2
    with pytest.raises(ValueError, match="版本|更新"):
        service.save(account_id="work", draft_id="reply", revision=1,
                     body="A stale AI edit", attachment_ids=[])
    assert service.draft(account_id="work", draft_id="reply")["draft"]["body"] == "The user's current edit"


def test_attachment_import_hash_path_and_tamper_checks(mailbox):
    service, ref = mailbox
    original = service.attachment(account_id="work", storage_ref=ref, index=0)["attachment"]
    assert base64.b64decode(original["data"]) == b"test PDF bytes"
    data = b"approved attachment"
    args = {"name": "ticket.pdf", "data": base64.b64encode(data).decode(),
            "sha256": hashlib.sha256(data).hexdigest()}
    attachment = service.upload(**args)["attachment"]
    assert service.upload(**args)["attachment"] == attachment
    preview = service.uploaded_attachment(attachment_id=attachment["id"])["attachment"]
    assert preview["type"] == "application/pdf"
    assert base64.b64decode(preview["data"]) == data
    with pytest.raises(ValueError, match="文件名"):
        service.upload(**(args | {"name": "../escape.pdf"}))
    with pytest.raises(ValueError, match="校验和"):
        service.upload(**(args | {"sha256": "0" * 64}))
    service.reply(account_id="work", storage_ref=ref, request_id="reply")
    service.save(account_id="work", draft_id="reply", revision=1,
                 body="Ticket attached.", attachment_ids=[attachment["id"]])
    path = next(service.attachments_root.rglob("ticket.pdf"))
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="发生变化"):
        service.draft(account_id="work", draft_id="reply")


def test_publish_uses_existing_server_draft_owner_and_never_sends(mailbox, monkeypatch):
    service, ref = mailbox
    draft = service.reply(account_id="work", storage_ref=ref, body="Approved reply.", request_id="reply")["draft"]
    calls = []
    def server_draft(**arguments):
        calls.append(arguments)
        return {"state": "draft", "server_verified": True, "send_allowed": False}
    monkeypatch.setattr(wb, "server_draft", server_draft)
    monkeypatch.setattr(service, "_server_result", lambda account, request, result: result)
    result = service.publish(account_id="work", draft_id="reply", revision=1)
    assert result["server_draft"]["send_allowed"] is False
    assert calls[0]["source_ref"] == ref and calls[0]["signature"] == draft["signature"]
    assert calls[0]["apply"] is True and calls[0]["inspect"] is False
    service.publish(account_id="work", draft_id="reply", revision=1)
    assert calls[1]["apply"] is False and calls[1]["inspect"] is True
    with pytest.raises(ValueError, match="提交"):
        service.save(account_id="work", draft_id="reply", revision=1, body="Changed", attachment_ids=[])


def test_unknown_publish_inspects_same_request_without_creating_another(mailbox, monkeypatch):
    service, ref = mailbox
    service.reply(account_id="work", storage_ref=ref, body="Approved reply.", request_id="reply")
    calls = []
    def server_draft(**arguments):
        calls.append(arguments)
        raise TimeoutError("uncertain append")
    monkeypatch.setattr(wb, "server_draft", server_draft)
    for _ in range(2):
        with pytest.raises(TimeoutError):
            service.publish(account_id="work", draft_id="reply", revision=1)
    assert calls[0]["request_id"] == calls[1]["request_id"]
    assert calls[0]["apply"] and calls[1]["inspect"] and not calls[1]["apply"]


def test_publish_validation_failure_before_remote_attempt_keeps_editable(mailbox, monkeypatch):
    service, ref = mailbox
    service.reply(account_id="work", storage_ref=ref, body="Approved reply.", request_id="reply")
    def invalid_draft(**arguments):
        raise ValueError("invalid reply headers")
    monkeypatch.setattr(wb, "server_draft", invalid_draft)
    with pytest.raises(ValueError, match="invalid reply headers"):
        service.publish(account_id="work", draft_id="reply", revision=1)
    assert service.draft(account_id="work", draft_id="reply")["draft"]["state"] == "editing"


def test_publish_value_error_after_durable_attempt_stays_locked(mailbox, monkeypatch):
    service, ref = mailbox
    service.reply(account_id="work", storage_ref=ref, body="Approved reply.", request_id="reply")
    def uncertain_draft(**arguments):
        conn = wb._ledger(service.ledger_path)
        conn.execute("INSERT INTO server_draft_requests VALUES (?,?,?,?,?,?)",
                     (arguments["request_id"], "work", "hash", b"raw", "attempted", "Drafts"))
        conn.commit()
        conn.close()
        raise ValueError("APPEND result unknown")
    monkeypatch.setattr(wb, "server_draft", uncertain_draft)
    with pytest.raises(ValueError, match="unknown"):
        service.publish(account_id="work", draft_id="reply", revision=1)
    assert service.draft(account_id="work", draft_id="reply")["draft"]["state"] == "publishing"


def test_send_requires_new_explicit_confirmation(mailbox, monkeypatch):
    service, _ = mailbox
    from codex_mail_workbench import server_send
    calls = []
    monkeypatch.setattr(server_send, "send_server_draft", lambda **values: calls.append(values) or {"state": "sent"})
    with pytest.raises(ValueError, match="确认发送"):
        service.send(account_id="work", request_id="draft", approval="sha256:test", confirmed=False)
    assert calls == []
    assert service.send(account_id="work", request_id="draft", approval="sha256:test", confirmed=True)["delivery"]["state"] == "sent"
    assert calls[0]["approval"] == "sha256:test"


def test_unknown_actions_and_credential_inputs_are_rejected(mailbox):
    service, _ = mailbox
    with pytest.raises(ValueError, match="不支持"):
        service.dispatch("delete", {"storage_ref": "anything"})
    with pytest.raises(TypeError):
        service.dispatch("read", {"account_id": "work", "storage_ref": "invalid", "password": "not-accepted"})


@pytest.mark.parametrize("host", ["generic", "codex", "opl"])
def test_connection_configs_select_one_installed_engine_and_profile(tmp_path, monkeypatch, host):
    profile = tmp_path / "profile with spaces"
    monkeypatch.setenv("OPL_PROFILE_WORKSPACE", str(profile))
    result = wb.connection_config(host=host)
    key = "mcp_servers" if host == "codex" else "mcpServers"
    entry = result["config"][key]["opl-relay"]
    assert entry["command"] == wb.sys.executable
    assert Path(entry["args"][0]).resolve() == wb.bundle_root().parent / "bin" / "opl-relay"
    assert entry["args"][1:] == ["--json", "workbench", "mcp"]
    assert entry["env"] == {"OPL_PROFILE_WORKSPACE": str(profile)}
    assert not profile.exists()


def test_dsh_native_bundle_is_configuration_only_and_preserves_existing_files(tmp_path, monkeypatch):
    import json

    profile = tmp_path / "profile"
    monkeypatch.setenv("OPL_PROFILE_WORKSPACE", str(profile))
    target = tmp_path / "dsh bundle"
    result = wb.connection_config(host="dsh", output=target)
    manifest = json.loads((target / "package.json").read_text())
    patch = json.loads((target / "cordis.patch.yml").read_text())
    assert manifest["dsh"]["bundle"]["patch"] == "./cordis.patch.yml"
    config = patch[0]["insert"][0]["config"]
    assert patch[0]["insert"][0]["name"] == "@deepseek-ai/dsh-mcp-client"
    assert config["env"]["OPL_PROFILE_WORKSPACE"] == str(profile)
    assert config["transport"] == "stdio" and config["failOnStartupError"] is True
    assert result["bundle_path"] == str(target)
    before = (target / "cordis.patch.yml").read_bytes()
    with pytest.raises(ValueError, match="目录必须为空"):
        wb.connection_config(host="dsh", output=target)
    assert (target / "cordis.patch.yml").read_bytes() == before
    assert not profile.exists()
