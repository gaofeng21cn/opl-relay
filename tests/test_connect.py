import hashlib
import json
import subprocess
import sys
from email.message import EmailMessage
from pathlib import Path

import pytest

from codex_mail_workbench.connect import SCHEMA_VERSION, ConnectProvider
from codex_mail_workbench.store import connect_email_store, upsert_email_message
from codex_mail_workbench.workbench import Workbench


@pytest.fixture
def provider(tmp_path):
    state = tmp_path / "profile" / "data" / "relay"
    state.mkdir(parents=True)
    profile = tmp_path / "profile"
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
credential_ref = "fixture-only"
[[accounts]]
account_id = "other"
email = "other@example.test"
[accounts.imap]
host = "imap.example.test"
port = 993
security = "ssl"
username = "other@example.test"
credential_ref = "fixture-only"
''', encoding="utf-8")
    message = EmailMessage()
    message["From"] = "Editor <editor@example.test>"
    message["To"] = "Owner <owner@example.test>"
    message["Subject"] = "Research update"
    message["Message-ID"] = "<fixture@example.test>"
    message.set_content("Please review the manuscript.")
    raw = message.as_bytes()
    db = state / "mail.sqlite"
    conn = connect_email_store(db)
    try:
        storage_ref = upsert_email_message(
            conn, account_id="work", folder="INBOX", folder_slug="INBOX",
            uid=1, uidvalidity=1, message_id=str(message["Message-ID"]),
            subject=str(message["Subject"]), sender=str(message["From"]),
            recipient=str(message["To"]), date_iso="2026-11-08T09:00:00+08:00",
            raw_sha256=hashlib.sha256(raw).hexdigest(), raw_eml=raw,
            attachments=[], ingest_ts="2026-11-08T09:01:00+08:00",
        )
    finally:
        conn.close()
    ledger = state / "drafts.sqlite"
    workbench = Workbench(config_path=config, db_path=db, ledger_path=ledger, profile_path=profile)
    return ConnectProvider(workbench=workbench), config, db, ledger, storage_ref


def envelope(operation, **fields):
    return {"schema_version": SCHEMA_VERSION, "operation": operation, **fields}


def test_list_and_inspect_are_secret_free_and_account_revision_bound(provider):
    service, config, _, _, _ = provider
    listed = service.dispatch(envelope("list"))
    assert listed["ok"] is True
    mailboxes = listed["result"]["mailboxes"]
    assert len(mailboxes) == 2
    assert all("credential_ref" not in json.dumps(mailbox) for mailbox in mailboxes)
    assert "fixture-only" not in json.dumps(listed)
    work = next(item for item in mailboxes if item["account_id"] == "work")
    inspected = service.dispatch(envelope("inspect", mailbox_ref=work["mailbox_ref"]))
    assert inspected["result"]["mailbox"] == work
    assert inspected["result"]["capabilities"] == ["mailbox.search", "mailbox.read", "draft.create"]

    config.write_text(config.read_text(encoding="utf-8").replace(
        'email = "owner@example.test"', 'email = "changed@example.test"'), encoding="utf-8")
    stale = service.dispatch(envelope("invoke", credential_handle={"opaque": work["mailbox_ref"], "revision": work["revision"]},
                                      capability="mailbox.search", input={"query": ""}))
    assert stale["error"]["code"] == "STALE_CREDENTIAL_HANDLE"


def test_invoke_search_and_read_are_bound_to_mailbox(provider):
    service, _, _, _, storage_ref = provider
    work = next(item for item in service.dispatch(envelope("list"))["result"]["mailboxes"] if item["account_id"] == "work")
    handle = {"opaque": work["mailbox_ref"], "revision": work["revision"]}
    search = service.dispatch(envelope("invoke", credential_handle=handle, capability="mailbox.search",
                                       input={"query": "manuscript", "limit": 10}))
    assert search["ok"] is True and search["disposition"] == "completed"
    assert search["result"]["messages"][0]["storage_ref"] == storage_ref
    assert search["result"]["source"] == "relay-local-cache"
    read = service.dispatch(envelope("invoke", credential_handle=handle, capability="mailbox.read",
                                     input={"storage_ref": storage_ref}))
    assert "Please review" in read["result"]["message"]["body_text"]
    assert read["result"]["source"] == "relay-local-cache"
    other = next(item for item in service.dispatch(envelope("list"))["result"]["mailboxes"]
                 if item["account_id"] == "other")
    cross_mailbox = service.dispatch(envelope(
        "invoke", credential_handle={"opaque": other["mailbox_ref"], "revision": other["revision"]},
        capability="mailbox.read", input={"storage_ref": storage_ref},
    ))
    assert cross_mailbox["ok"] is False and cross_mailbox["disposition"] == "not_dispatched"


def test_caller_cannot_select_another_mailbox_in_input(provider):
    service, _, _, _, _ = provider
    work = next(item for item in service.dispatch(envelope("list"))["result"]["mailboxes"] if item["account_id"] == "work")
    result = service.dispatch(envelope("invoke", credential_handle={"opaque": work["mailbox_ref"], "revision": work["revision"]},
                                        capability="mailbox.search", input={"account_id": "other", "query": ""}))
    assert result == {"ok": False, "disposition": "not_dispatched",
                      "error": {"code": "INVALID_ARGUMENTS", "message": "调用参数无效"}}


def test_draft_create_only_creates_local_review_draft_and_is_idempotent(provider):
    service, _, _, ledger, storage_ref = provider
    work = next(item for item in service.dispatch(envelope("list"))["result"]["mailboxes"] if item["account_id"] == "work")
    handle = {"opaque": work["mailbox_ref"], "revision": work["revision"]}
    request = envelope("invoke", credential_handle=handle, capability="draft.create",
                       input={"storage_ref": storage_ref, "body": "Thank you.", "request_id": "connect-reply"})
    first = service.dispatch(request)
    second = service.dispatch(request)
    assert first["ok"] is True and second["ok"] is True
    assert first["result"]["persistence"] == "relay-local-review"
    assert first["result"]["send_allowed"] is False
    assert first["result"]["draft"]["id"] == second["result"]["draft"]["id"]
    assert ledger.exists()


def test_provider_errors_do_not_return_underlying_exception(provider, monkeypatch):
    service, _, _, _, _ = provider
    work = next(item for item in service.dispatch(envelope("list"))["result"]["mailboxes"] if item["account_id"] == "work")
    handle = {"opaque": work["mailbox_ref"], "revision": work["revision"]}
    monkeypatch.setattr(service.workbench, "read", lambda **_: (_ for _ in ()).throw(RuntimeError("private-token-value")))
    result = service.dispatch(envelope("invoke", credential_handle=handle, capability="mailbox.read",
                                       input={"storage_ref": "email-store://missing"}))
    assert result["ok"] is False and result["disposition"] == "not_dispatched"
    assert "private-token-value" not in json.dumps(result)


def test_cli_connect_reads_and_writes_only_json_stdout(provider):
    _, config, db, ledger, _ = provider
    command = [sys.executable, "-m", "codex_mail_workbench.cli", "--json", "--config", str(config),
               "--db", str(db), "--draft-db", str(ledger), "connect"]
    result = subprocess.run(command, input=json.dumps(envelope("list")), text=True, capture_output=True, check=False,
                            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "plugins" / "opl-relay" / "runtime")})
    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout)["ok"] is True
