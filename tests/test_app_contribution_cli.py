import json
import hashlib
import io
import os
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_mail_workbench import cli
from codex_mail_workbench.cli import APP_CONTRIBUTION_DATA_CONTRACTS, APP_CONTRIBUTION_ACTION_CONTRACTS
from codex_mail_workbench.memory import MemoryStore
from codex_mail_workbench.store import connect_email_store, record_reviews, upsert_email_message
from test_drafts import registered_service
from test_triage import seed_message


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "opl-relay"
PACKAGE = json.loads((PLUGIN_ROOT / "opl-package.json").read_text())
REQUEST_SCHEMA = PACKAGE["codex_surface"]["app_contribution_abi"]["request_schema"]


def invoke(profile: Path, ref: str, *, operation: str = "read", payload=None):
    request = {"schema_version": REQUEST_SCHEMA, "operation": operation, "ref": ref}
    if payload is not None:
        request["input"] = payload
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["OPL_PROFILE_WORKSPACE"] = str(profile)
    process = subprocess.run(
        PACKAGE["codex_surface"]["app_contribution_abi"]["argv"], cwd=PLUGIN_ROOT,
        env=env, input=json.dumps(request), capture_output=True, text=True, check=False,
    )
    assert process.stderr == ""
    return process.returncode, json.loads(process.stdout)


def seed(profile: Path):
    store = MemoryStore(profile / "data" / "relay" / "memory.sqlite")
    entity = store.upsert_entity(kind="person", canonical_name="Test Colleague", aliases=["Alias"])
    memories = []
    for content in ("APPROVED FACT", "CANDIDATE FACT"):
        memories.append(store.propose_memory(
            entity_ref=entity["entity_ref"], category="fact", content=content,
            sources=[{"source_ref": "email-store://work/INBOX/1/abcdef", "source_kind": "email",
                      "excerpt": "SOURCE EXCERPT"}],
        ))
    store.approve(memories[0]["memory_ref"])
    return store, entity, memories


@pytest.mark.parametrize("ref", ["personal.memory.v1#people", "personal.memory.v1#search"])
def test_real_carrier_reads_missing_memory_without_profile_initialization(tmp_path: Path, ref):
    profile = tmp_path / "missing-profile"
    code, response = invoke(profile, ref)
    assert code == 0
    assert response["ok"] is True
    assert response["operation"] == "read"
    assert response["schema_version"] == "opl-package-app-contribution-response.v1"
    result = response["result"]
    assert result["kind"] == "data"
    assert result["state"] == "ready"
    assert result["data"]["items"] == []
    assert result["data"]["count"] == 0
    assert result["data"]["store_state"] == "missing"
    assert not profile.exists()


def test_ui_entries_discover_people_and_evidence_from_descriptor(tmp_path: Path):
    profile = tmp_path / "profile"
    store, entity, memories = seed(profile)
    before = store.path.read_bytes()
    contributions = PACKAGE["app_contributions"]
    commands = {item["command_id"]: item for item in contributions["commands"]}
    views = {item["view_id"]: item for item in contributions["views"]}
    mounted = [views[entry["view_id"]] for entry in contributions["ui"]
               if entry["slot"] == "settings.section" and entry["contribution_kind"] == "view"]
    memory_views = [view for view in mounted if view["data_ref"].startswith("personal.memory.v1#")]
    assert {view["view_type"] for view in memory_views} == {"list_detail", "timeline"}
    for view in memory_views:
        code, response = invoke(profile, view["data_ref"], payload={"limit": 1})
        assert code == 0
        result = response["result"]
        assert result["kind"] == "data" and result["state"] == "ready"
        assert result["data"]["count"] == 1
        item = result["data"]["items"][0]
        assert item["entity_ref"] == entity["entity_ref"]
        allowed_refs = {commands[identifier]["action_ref"] for identifier in view["command_ids"]}
        assert set(result["data"]["command_inputs"]) == allowed_refs
        for action in item["actions"]:
            assert action["action_ref"] in allowed_refs
            assert action["input"]
        assert "CANDIDATE FACT" not in json.dumps(result)
        assert "SOURCE EXCERPT" not in json.dumps(result)
        assert result["data"]["source_policy"]["only_approved_memory_included"] is True
    assert store.path.read_bytes() == before

    code, result = invoke(profile, "personal.memory.v1#search", payload={"entity": entity["entity_ref"]})
    assert code == 0
    item = result["result"]["data"]["items"][0]
    assert item["memory_ref"] == memories[0]["memory_ref"]
    code, inspected = invoke(
        profile, commands["relay.memory.inspect"]["action_ref"],
        operation="execute", payload=item["actions"][0]["input"],
    )
    assert code == 0
    assert inspected["result"]["memory"]["sources"][0]["excerpt"] == "SOURCE EXCERPT"
    assert not (profile / "data" / "relay" / "mail.sqlite").exists()


@pytest.mark.parametrize("ref", ["personal.memory.v1#people", "personal.memory.v1#search"])
def test_memory_read_contracts_describe_and_refuse_execute(tmp_path: Path, ref):
    code, response = invoke(tmp_path / "profile", ref, operation="describe")
    assert code == 0
    assert response["result"]["operations"] == [APP_CONTRIBUTION_DATA_CONTRACTS[ref]]
    code, response = invoke(tmp_path / "profile", ref, operation="execute")
    assert code == 2
    assert response["ok"] is False
    assert "does not support execute" in response["error"]["message"]
    assert not (tmp_path / "profile").exists()


@pytest.mark.parametrize("payload", [
    {"limit": 0}, {"limit": 501}, {"limit": True}, {"limit": "1"},
    {"query": 1}, {"query": " "}, {"status": "candidate"}, {"apply": True},
    {"action_ref": "personal.memory.v1#approve"},
])
def test_people_rejects_invalid_or_write_inputs_without_side_effects(tmp_path: Path, payload):
    profile = tmp_path / "profile"
    code, response = invoke(profile, "personal.memory.v1#people", payload=payload)
    assert code == 2
    assert response["error"]["code"] == "invalid_request"
    assert not profile.exists()


def test_people_query_and_candidate_inspection_are_separate(tmp_path: Path):
    profile = tmp_path / "profile"
    store, entity, memories = seed(profile)
    code, response = invoke(profile, "personal.memory.v1#people", payload={"query": "Alias", "limit": 1})
    assert code == 0
    assert response["result"]["data"]["items"][0]["id"] == entity["entity_ref"]
    code, response = invoke(profile, "personal.memory.v1#people", payload={"query": "CANDIDATE FACT"})
    assert code == 0
    assert response["result"]["data"]["count"] == 0
    code, response = invoke(profile, "personal.memory.v1#inspect", operation="execute",
                            payload={"memory_ref": memories[1]["memory_ref"]})
    assert code == 0
    assert response["result"]["memory"]["status"] == "candidate"
    assert store.get_memory(memories[1]["memory_ref"])["status"] == "candidate"


def test_every_read_view_supplies_only_descriptor_declared_command_schemas(tmp_path: Path):
    commands = {item["command_id"]: item for item in PACKAGE["app_contributions"]["commands"]}
    for view in PACKAGE["app_contributions"]["views"]:
        code, response = invoke(tmp_path / "missing-profile", view["data_ref"])
        assert code == 0
        result = response["result"]
        assert result["kind"] == "data"
        assert result["state"] in {"ready", "input_required"}
        assert result["data"]["items"] == []
        assert set(result["input_schema"]) == set(APP_CONTRIBUTION_DATA_CONTRACTS[view["data_ref"]]["input"])
        allowed_refs = {commands[identifier]["action_ref"] for identifier in view["command_ids"]}
        metadata = result["data"]["command_inputs"]
        assert set(metadata) == allowed_refs
        for ref, schema in metadata.items():
            expected = {name: {**field, "type": "string_list" if field["type"] == "string[]" else field["type"]}
                        for name, field in APP_CONTRIBUTION_ACTION_CONTRACTS[ref]["input"].items()}
            assert schema["input_schema"] == expected
            assert schema["defaults"] == {}
            assert schema["confirmation_required"] == APP_CONTRIBUTION_ACTION_CONTRACTS[ref]["confirmation_required"]
            assert "scope" not in schema["input_schema"]
        if result["state"] == "input_required":
            assert result["reason"]
    assert not (tmp_path / "missing-profile").exists()


def test_recent_collection_preserves_messages_and_binds_actual_account(tmp_path: Path):
    profile = tmp_path / "profile"
    db = profile / "data" / "relay" / "mail.sqlite"
    conn = connect_email_store(db)
    try:
        reference = upsert_email_message(
            conn, account_id="work", folder="INBOX", folder_slug="INBOX", uid=1,
            uidvalidity=1, message_id="<fixture@example.test>", subject="Fixture mail",
            sender="author@example.test", recipient="work@example.test",
            date_iso="2026-09-30T00:00:00Z", raw_sha256="a" * 64,
            raw_eml=b"Subject: Fixture mail\r\n\r\nSynthetic evidence.", attachments=[],
            ingest_ts="2026-09-30T00:00:00Z",
        )
    finally:
        conn.close()
    before = db.read_bytes()
    code, response = invoke(profile, "communications.mail.v1#recent")
    assert code == 0
    result = response["result"]
    assert result["messages"][0]["storage_ref"] == reference
    item = result["data"]["items"][0]
    assert item["id"] == item["storage_ref"] == reference
    assert item["actions"] == [{"action_ref": "communications.mail.v1#sync.incremental", "input": {"account": "work"}}]
    assert item["body_text"] == "Synthetic evidence."
    assert item["review_target"]["source_ref"] == reference
    assert db.read_bytes() == before


def call_in_process(args, ref, *, operation="read", payload=None, monkeypatch, capsys):
    request = {"schema_version": REQUEST_SCHEMA, "operation": operation, "ref": ref}
    if payload is not None:
        request["input"] = payload
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))
    code = cli.main(["--json", *args, "app-contribution"])
    return code, json.loads(capsys.readouterr().out)


def checkpoint_draft_fixture(path: Path):
    # Settle fixture WAL bytes before comparing the read-only subprocess snapshot.
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        conn.close()


def test_draft_collection_is_readonly_and_actions_do_not_auto_approve_send(tmp_path: Path, monkeypatch, capsys):
    profile = tmp_path / "profile"
    ref = "communications.mail.v1#draft.inspect"
    service, provider, draft_ref = registered_service(profile / "data" / "relay")
    checkpoint_draft_fixture(service.ledger.path)
    before = service.ledger.path.read_bytes()
    code, response = invoke(profile, ref)
    assert code == 0
    item = response["result"]["data"]["items"][0]
    assert item["id"] == item["draft_ref"] == draft_ref
    assert item["inspection_required"] is True
    assert item["read_input"] == {"draft_ref": draft_ref}
    assert "body_text" not in item
    assert {action["action_ref"] for action in item["actions"]} == {
        "communications.mail.v1#draft.inspect", "communications.mail.v1#draft.open", "communications.mail.v1#draft.send",
    }
    for action in item["actions"]:
        assert action["input"] == {"draft_ref": draft_ref}
        assert "scope" not in action
        assert "approval" not in action["input"]
    assert service.ledger.path.read_bytes() == before
    send_schema = response["result"]["data"]["command_inputs"]["communications.mail.v1#draft.send"]
    assert send_schema["confirmation_required"] is True
    assert send_schema["input_schema"]["approval"]["required"] is True
    assert send_schema["defaults"] == {}

    monkeypatch.setattr(cli, "draft_service", lambda args: (service, provider))
    args = ["--draft-db", str(service.ledger.path)]
    code, inspected = call_in_process(
        args, ref, payload=item["read_input"],
        monkeypatch=monkeypatch, capsys=capsys,
    )
    assert code == 0
    assert inspected["ref"] == ref and inspected["operation"] == "read"
    assert inspected["result"]["kind"] == "data" and inspected["result"]["state"] == "ready"
    fingerprint = inspected["result"]["draft"]["approval_fingerprint"]
    inspected_item = inspected["result"]["data"]["items"][0]
    assert inspected_item["id"] == item["id"]
    assert inspected_item["read_input"] == item["read_input"]
    assert inspected_item["inspection_required"] is False
    assert inspected_item["body_text"] == provider.current.body_text
    assert inspected_item["review_target"]["to"] == [{"address": "reviewer@example.test", "name": "Reviewer"}]
    assert inspected_item["review_target"]["attachments"][0]["name"] == "paper.pdf"
    assert all("approval" not in action["input"] for action in inspected_item["actions"])
    send_input = next(action["input"] for action in item["actions"] if action["action_ref"].endswith("#draft.send"))
    code, failure = call_in_process(args, "communications.mail.v1#draft.send", operation="execute",
                                   payload=send_input, monkeypatch=monkeypatch, capsys=capsys)
    assert code == 2 and failure["ok"] is False
    provider.current = replace(provider.current, body_text="Changed after review")
    code, failure = call_in_process(args, "communications.mail.v1#draft.send", operation="execute",
                                   payload={**send_input, "approval": fingerprint}, monkeypatch=monkeypatch, capsys=capsys)
    assert code == 2 and failure["ok"] is False
    assert provider.send_calls == 0
    assert service.ledger.get(draft_ref)["state"] == "draft"


def test_unknown_draft_state_cannot_offer_open_or_send(tmp_path: Path):
    profile = tmp_path / "profile"
    service, provider, draft_ref = registered_service(profile / "data" / "relay")
    service.ledger.claim_send(draft_ref, fingerprint="sha256:fixture")
    service.ledger.mark_unknown(draft_ref, detail="synthetic interruption")
    code, response = invoke(profile, "communications.mail.v1#draft.inspect")
    assert code == 0
    item = response["result"]["data"]["items"][0]
    assert item["state"] == "unknown"
    assert item["read_input"] == {"draft_ref": draft_ref}
    assert item["actions"] == [{"action_ref": "communications.mail.v1#draft.inspect", "input": {"draft_ref": draft_ref}}]
    assert provider.send_calls == 0


def test_plain_memory_search_cli_keeps_its_non_app_interface(tmp_path: Path):
    profile = tmp_path / "profile"
    _, _, memories = seed(profile)
    env = os.environ.copy()
    env["OPL_PROFILE_WORKSPACE"] = str(profile)
    process = subprocess.run([str(PLUGIN_ROOT / "bin" / "opl-relay"), "--json", "memory", "search"],
                             input="", capture_output=True, text=True, check=False, env=env)
    assert process.returncode == 0, process.stderr
    payload = json.loads(process.stdout)
    assert payload["ok"] is True
    assert [memory["memory_ref"] for memory in payload["memories"]] == [memories[0]["memory_ref"]]


def test_declared_reply_all_abi_uses_only_supplied_native_mail_identity(monkeypatch, capsys):
    calls = {}

    class Service:
        def reply_all(self, **kwargs):
            calls.update(kwargs)
            return {"draft_ref": "mail-draft://apple-mail/work/fixture", "state": "draft"}

    monkeypatch.setattr(cli, "load_account", lambda *args: SimpleNamespace(account_id="work", email="work@example.test"))
    monkeypatch.setattr(cli, "draft_service", lambda args: (Service(), object()))
    payload = {"account": "work", "apple_mail_account": "Work", "apple_mail_id": 123,
               "mailbox_path": "INBOX/Conference", "body": "Synthetic reply", "open": False}
    code, response = call_in_process([], "communications.mail.v1#draft.reply_all", operation="execute",
                                     payload=payload, monkeypatch=monkeypatch, capsys=capsys)
    assert code == 0
    assert response["result"]["draft"]["state"] == "draft"
    assert calls == {"account_id": "work", "sender": "work@example.test", "provider_account": "Work",
                     "source_message_id": 123, "mailbox_path": "INBOX/Conference", "body_text": "Synthetic reply",
                     "attachments": [], "visible": False}
    code, response = call_in_process([], "communications.mail.v1#draft.reply_all", operation="execute",
                                     payload={"storage_ref": "email-store://work/INBOX/1/abcdef"},
                                     monkeypatch=monkeypatch, capsys=capsys)
    assert code == 2 and response["ok"] is False


def seed_mail_batch(profile: Path, count: int):
    db = profile / "data" / "relay" / "mail.sqlite"
    conn = connect_email_store(db)
    refs = []
    try:
        for uid in range(1, count + 1):
            body = "indexed needle" if uid % 2 == 0 else "ordinary body"
            raw = f"Subject: Message {uid}\r\n\r\n{body}".encode()
            refs.append(upsert_email_message(
                conn, account_id="work", folder="INBOX", folder_slug="INBOX", uid=uid,
                uidvalidity=1, message_id=f"<batch-{uid}@example.test>", subject=f"Message {uid}",
                sender="author@example.test", recipient="work@example.test",
                date_iso="2026-09-30T00:00:00Z", raw_sha256=hashlib.sha256(raw).hexdigest(),
                raw_eml=raw, attachments=[], ingest_ts="2026-09-30T00:00:00Z",
            ))
        record_reviews(conn, records=[
            {"storage_ref": refs[1], "action": "needs_user_reply", "status": "open", "note": "Review this exact source"},
            {"storage_ref": refs[3], "action": "remind", "status": "waiting"},
        ])
    finally:
        conn.close()
    return db, refs


def test_recent_paginates_and_filters_entire_store_before_limiting(tmp_path: Path):
    profile = tmp_path / "profile"
    db, refs = seed_mail_batch(profile, 530)
    before = db.read_bytes()
    code, response = invoke(profile, "communications.mail.v1#recent", payload={"offset": 520, "limit": 3})
    assert code == 0
    data = response["result"]["data"]
    assert data["pagination"] == {"offset": 520, "limit": 3, "total": 530, "has_more": True}
    assert [item["storage_ref"] for item in data["items"]] == refs[7:10][::-1]

    code, response = invoke(profile, "communications.mail.v1#recent", payload={"query": "indexed needle", "offset": 260, "limit": 3})
    assert code == 0
    data = response["result"]["data"]
    assert data["pagination"] == {"offset": 260, "limit": 3, "total": 265, "has_more": True}
    assert [item["uid"] for item in data["items"]] == [10, 8, 6]
    code, response = invoke(profile, "communications.mail.v1#recent", payload={"query": " indexed needle ", "account": " work ", "status": "open", "limit": 1})
    assert code == 0
    data = response["result"]["data"]
    assert data["pagination"]["total"] == 1
    assert data["items"][0]["storage_ref"] == refs[1]
    assert data["items"][0]["review_target"]["note"] == "Review this exact source"

    for payload, total in (({"offset": 530}, 530), ({"query": "no match"}, 0), ({"status": "waiting"}, 1),
                           ({"status": "unreviewed", "account": "work"}, 528), ({"account": "other"}, 0),
                           ({"until": "2026-09-29T00:00:00Z"}, 0)):
        code, response = invoke(profile, "communications.mail.v1#recent", payload=payload)
        assert code == 0
        assert response["result"]["data"]["pagination"]["total"] == total
        if "offset" in payload or total == 0:
            assert response["result"]["data"]["items"] == []
            assert response["result"]["data"]["pagination"]["has_more"] is False
    assert db.read_bytes() == before


def test_draft_query_and_state_filter_page_existing_ledger_without_inspection(tmp_path: Path):
    profile = tmp_path / "profile"
    service, provider, draft_ref = registered_service(profile / "data" / "relay")
    with sqlite3.connect(service.ledger.path) as conn:
        for number in range(30):
            conn.execute("""INSERT INTO mail_drafts
                (draft_ref, account_id, provider_account, provider_uuid, state, created_at, updated_at)
                VALUES (?, 'work', 'Work', ?, ?, '2026-09-30', '2026-09-30')""",
                (f"mail-draft://apple-mail/work/batch-{number:02}", f"batch-{number:02}", "sent" if number < 3 else "draft"))
    checkpoint_draft_fixture(service.ledger.path)
    before = service.ledger.path.read_bytes()
    code, response = invoke(profile, "communications.mail.v1#draft.inspect", payload={"query": "batch-", "status": "all", "offset": 25, "limit": 3})
    assert code == 0
    data = response["result"]["data"]
    assert data["pagination"] == {"offset": 25, "limit": 3, "total": 30, "has_more": True}
    assert [item["provider_uuid"] for item in data["items"]] == ["batch-25", "batch-26", "batch-27"]
    code, response = invoke(profile, "communications.mail.v1#draft.inspect", payload={"query": "batch-", "status": "sent", "offset": 1, "limit": 2})
    assert code == 0
    data = response["result"]["data"]
    assert data["pagination"] == {"offset": 1, "limit": 2, "total": 3, "has_more": False}
    assert all(item["state"] == "sent" and len(item["actions"]) == 1 for item in data["items"])
    assert all(item["read_input"] == {"draft_ref": item["draft_ref"]} for item in data["items"])
    assert all("body_text" not in item and "approval_fingerprint" not in item for item in data["items"])
    code, response = invoke(profile, "communications.mail.v1#draft.inspect", payload={"query": "batch-", "offset": 27})
    assert code == 0
    assert response["result"]["data"]["pagination"]["total"] == 27
    assert response["result"]["data"]["items"] == []
    assert service.ledger.path.read_bytes() == before
    assert provider.send_calls == 0


def test_recent_body_projection_is_bounded_and_incomplete_index_is_not_silently_searched(tmp_path: Path):
    profile = tmp_path / "profile"
    db = profile / "data" / "relay" / "mail.sqlite"
    conn = connect_email_store(db)
    try:
        raw = b"Subject: Long body\r\n\r\n" + b"x" * 20001
        upsert_email_message(
            conn, account_id="work", folder="INBOX", folder_slug="INBOX", uid=1,
            uidvalidity=1, message_id="<long@example.test>", subject="Long body",
            sender="author@example.test", recipient="work@example.test", date_iso="2026-09-30T00:00:00Z",
            raw_sha256=hashlib.sha256(raw).hexdigest(), raw_eml=raw, attachments=[], ingest_ts="2026-09-30T00:00:00Z",
        )
        conn.execute("DELETE FROM email_search")
        conn.commit()
    finally:
        conn.close()
    before = db.read_bytes()
    code, response = invoke(profile, "communications.mail.v1#recent")
    assert code == 0
    item = response["result"]["data"]["items"][0]
    assert len(item["body_text"]) == 20000 and item["body_truncated"] is True
    code, response = invoke(profile, "communications.mail.v1#recent", payload={"query": "Long body"})
    assert code == 2
    assert "index incomplete" in response["error"]["message"]
    assert db.read_bytes() == before


@pytest.mark.parametrize("ref", ["communications.mail.v1#recent", "communications.mail.v1#draft.inspect"])
@pytest.mark.parametrize("payload", [{"offset": -1}, {"offset": True}, {"offset": "1"}, {"status": "invalid"},
                                     {"limit": 0}, {"query": 1}, {"query": " "}, {"apply": True}])
def test_collection_read_rejects_invalid_filters_without_initializing_profile(tmp_path: Path, ref, payload):
    code, response = invoke(tmp_path / "missing", ref, payload=payload)
    assert code == 2
    assert response["error"]["code"] == "invalid_request"
    assert not (tmp_path / "missing").exists()


def test_triage_input_required_has_usable_read_form_and_only_existing_options(tmp_path: Path):
    missing = tmp_path / "missing"
    code, response = invoke(missing, "communications.mail.v1#triage.evidence")
    assert code == 0
    result = response["result"]
    assert result["state"] == "input_required" and result["data"]["items"] == []
    assert result["input_schema"]["source_ref"]["required"] is True
    assert result["input_schema"]["policy_refs"]["type"] == "string_list"
    assert result["input_schema"]["policy_refs"]["required"] is True
    assert all(field["options"] == [] for field in result["input_schema"].values())
    assert not missing.exists()

    profile = tmp_path / "profile"
    db = profile / "data" / "relay" / "mail.sqlite"
    source_ref = seed_message(db)
    policies = profile / "policies"
    policies.mkdir()
    policy = policies / "review.md"
    policy.write_text("# Review\nPrivate policy content must not be projected.\n")
    before = db.read_bytes()
    code, response = invoke(profile, "communications.mail.v1#triage.evidence", payload={"source_ref": source_ref})
    assert code == 0
    result = response["result"]
    assert result["state"] == "input_required"
    assert result["input_schema"]["source_ref"]["options"] == [
        {"value": source_ref, "label_i18n": {"en-US": "Evidence thread", "zh-CN": "Evidence thread"}},
    ]
    assert result["input_schema"]["policy_refs"]["options"] == [
        {"value": policy.as_uri(), "label_i18n": {"en-US": "review.md", "zh-CN": "review.md"}},
    ]
    assert "Private policy content" not in json.dumps(result)
    code, response = invoke(profile, "communications.mail.v1#triage.evidence",
                            payload={"source_ref": source_ref, "policy_refs": [policy.as_uri()]})
    assert code == 0
    result = response["result"]
    assert result["state"] == "ready"
    item = result["data"]["items"][0]
    assert item["body_text"] == result["evidence"]["mail"]["raw_readback"]["body_text"]
    assert item["review_target"]["source_ref"] == source_ref
    assert item["review_target"]["policy_refs"] == [policy.as_uri()]
    assert item["risk"]["external_write_allowed"] is False
    assert db.read_bytes() == before


def test_collection_commands_are_explicit_even_without_rows(tmp_path: Path):
    expected = {
        "communications.mail.v1#recent": {"communications.mail.v1#sync.incremental"},
        "communications.mail.v1#draft.inspect": {"communications.mail.v1#draft.create", "communications.mail.v1#draft.reply_all",
                                                "communications.mail.v1#draft.create_from_persona"},
        "communications.mail.v1#triage.evidence": set(),
        "personal.memory.v1#people": {"personal.context.v1#build"},
        "personal.memory.v1#search": set(),
    }
    for ref, action_refs in expected.items():
        code, response = invoke(tmp_path / "missing", ref)
        assert code == 0
        data = response["result"]["data"]
        assert data["items"] == []
        actions = data["collection_actions"]
        assert {action["action_ref"] for action in actions} == action_refs
        for action in actions:
            assert action["input"] == {}
            assert set(action["label_i18n"]) == {"en-US", "zh-CN"}
            assert action["action_ref"] in data["command_inputs"]
    code, response = invoke(tmp_path / "missing", "communications.mail.v1#recent", payload={"account": "work"})
    assert code == 0
    assert response["result"]["data"]["collection_actions"][0]["input"] == {"account": "work"}
    assert not (tmp_path / "missing").exists()
