import copy
import hashlib
import io
import json
from pathlib import Path

import pytest

from codex_mail_workbench import cli
from codex_mail_workbench.store import connect_email_store, upsert_email_message
from codex_mail_workbench.triage import build_triage_evidence, validate_triage_evidence


def seed_message(db: Path) -> str:
    raw = (
        b"Subject: Evidence thread\r\n"
        b"From: editor@example.test\r\n"
        b"To: work@example.com\r\n"
        b"Message-ID: <triage@example.test>\r\n"
        b"\r\n"
        b"The original message body is available for review."
    )
    conn = connect_email_store(db)
    try:
        return upsert_email_message(
            conn,
            account_id="work",
            folder="INBOX",
            folder_slug="INBOX",
            uid=7,
            uidvalidity=1,
            message_id="<triage@example.test>",
            subject="Evidence thread",
            sender="editor@example.test",
            recipient="work@example.com",
            date_iso="2026-07-28T09:00:00+08:00",
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            raw_eml=raw,
            attachments=[],
            ingest_ts="2026-07-28T09:01:00+08:00",
        )
    finally:
        conn.close()


def evidence(db: Path, source_ref: str) -> dict[str, object]:
    return build_triage_evidence(
        mail_db_path=db,
        source_ref=source_ref,
        policy_refs=["policy://relay/triage/v1", "policy://user/review-required/v1"],
        observed_at="2026-07-28T09:02:00+00:00",
    )


def test_triage_evidence_reads_original_mail_without_provider_write(tmp_path: Path) -> None:
    db = tmp_path / "mail.sqlite"
    source_ref = seed_message(db)
    before = db.read_bytes()

    result = evidence(db, source_ref)

    assert db.read_bytes() == before
    assert result["source_refs"] == [source_ref]
    assert result["mail"]["raw_readback"]["status"] == "available"
    assert "original message body" in result["mail"]["raw_readback"]["body_text"]
    assert result["freshness"] == {
        "status": "local_store_readback",
        "observed_at": "2026-07-28T09:02:00+00:00",
        "ingested_at": "2026-07-28T09:01:00+08:00",
        "message_date": "2026-07-28T09:00:00+08:00",
        "external_mailbox_freshness": "unknown",
    }
    assert result["risk"]["external_write_allowed"] is False
    assert result["risk"]["provider_write_reachable"] is False
    assert result["provider_write"]["status"] == "unreachable"
    assert result["triage"] == {
        "mode": "evidence_only",
        "personal_judgment": "not_provided",
    }
    assert validate_triage_evidence(result)["ok"] is True


def test_triage_rejects_noncanonical_email_evidence_ref(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="canonical email-store"):
        build_triage_evidence(
            mail_db_path=tmp_path / "mail.sqlite",
            source_ref="imap://work/INBOX/7",
            policy_refs=["policy://relay/triage/v1"],
        )


def test_triage_validation_rejects_missing_provenance_or_changed_policy(tmp_path: Path) -> None:
    db = tmp_path / "mail.sqlite"
    source_ref = seed_message(db)
    baseline = evidence(db, source_ref)

    missing_provenance = copy.deepcopy(baseline)
    missing_provenance.pop("source_refs")
    with pytest.raises(ValueError, match="source_refs"):
        validate_triage_evidence(missing_provenance)

    empty_digest = copy.deepcopy(baseline)
    empty_digest["policy"]["policy_digest"] = ""
    with pytest.raises(ValueError, match="policy_digest"):
        validate_triage_evidence(empty_digest)

    changed_policy = copy.deepcopy(baseline)
    changed_policy["policy"]["policy_refs"].append("policy://relay/changed/v1")
    with pytest.raises(ValueError, match="policy_digest"):
        validate_triage_evidence(changed_policy)


def test_triage_validation_rejects_reachable_provider_write(tmp_path: Path) -> None:
    db = tmp_path / "mail.sqlite"
    source_ref = seed_message(db)
    changed = evidence(db, source_ref)
    changed["risk"]["provider_write_reachable"] = True

    with pytest.raises(ValueError, match="provider writes"):
        validate_triage_evidence(changed)


def test_triage_cli_evidence_and_validate_are_read_only(tmp_path: Path, capsys) -> None:
    db = tmp_path / "mail.sqlite"
    source_ref = seed_message(db)
    before = db.read_bytes()

    assert cli.main(
        [
            "--json",
            "--db",
            str(db),
            "triage",
            "evidence",
            source_ref,
            "--policy-ref",
            "policy://relay/triage/v1",
        ]
    ) == 0
    envelope = json.loads(capsys.readouterr().out)["evidence"]
    assert db.read_bytes() == before

    input_path = tmp_path / "evidence.json"
    input_path.write_text(json.dumps(envelope), encoding="utf-8")
    assert cli.main(["--json", "triage", "validate", "--input", str(input_path)]) == 0
    assert json.loads(capsys.readouterr().out)["provider_write"] == "unreachable"


def test_triage_cli_evidence_output_pipes_to_validate_without_writing(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    db = tmp_path / "mail.sqlite"
    source_ref = seed_message(db)
    before = db.read_bytes()

    assert cli.main(
        [
            "--json",
            "--db",
            str(db),
            "triage",
            "evidence",
            source_ref,
            "--policy-ref",
            "policy://relay/triage/v1",
        ]
    ) == 0
    evidence_output = capsys.readouterr().out
    assert json.loads(evidence_output)["ok"] is True
    assert db.read_bytes() == before

    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(evidence_output))
    assert cli.main(["--json", "triage", "validate", "--input", "-"]) == 0
    assert json.loads(capsys.readouterr().out)["provider_write"] == "unreachable"
    assert db.read_bytes() == before


def test_triage_cli_rejects_unsuccessful_evidence_wrapper(tmp_path: Path, capsys, monkeypatch) -> None:
    db = tmp_path / "mail.sqlite"
    source_ref = seed_message(db)
    before = db.read_bytes()
    failed_wrapper = {"ok": False, "evidence": evidence(db, source_ref)}

    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(failed_wrapper)))
    assert cli.main(["--json", "triage", "validate", "--input", "-"]) == 1
    assert "must be successful" in json.loads(capsys.readouterr().err)["error"]
    assert db.read_bytes() == before
