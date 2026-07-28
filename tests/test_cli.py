import json
import io
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_mail_workbench import cli
from codex_mail_workbench.store import connect_email_store, upsert_email_message


def run_cli(db: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(root / "src")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "codex_mail_workbench.cli",
            "--db",
            str(db),
            "--json",
            *args,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def run_app_contribution(
    db: Path,
    request: dict[str, object],
    *,
    memory_db: Path | None = None,
    sources_config: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(root / "src")
    command = [
        sys.executable,
        "-m",
        "codex_mail_workbench.cli",
        "--db",
        str(db),
        "--json",
    ]
    if memory_db is not None:
        command.extend(["--memory-db", str(memory_db)])
    if sources_config is not None:
        command.extend(["--sources-config", str(sources_config)])
    command.append("app-contribution")
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        input=json.dumps(request),
        env=env,
    )


def seed_message(db: Path) -> str:
    conn = connect_email_store(db)
    try:
        raw = (
            b"Subject: Research thread\r\n"
            b"From: editor@example.test\r\n"
            b"To: work@example.com\r\n"
            b"Message-ID: <seed@example.test>\r\n"
            b"\r\n"
            b"Please review this manuscript."
        )
        return upsert_email_message(
            conn,
            account_id="work",
            folder="INBOX",
            folder_slug="INBOX",
            uid=1,
            uidvalidity=1,
            message_id="<seed@example.test>",
            subject="Research thread",
            sender="editor@example.test",
            recipient="work@example.com",
            date_iso="2026-05-17T09:00:00+08:00",
            raw_sha256="2" * 64,
            raw_eml=raw,
            attachments=[],
            ingest_ts="2026-05-17T09:01:00+08:00",
        )
    finally:
        conn.close()


def test_cli_recent_search_and_read_json(tmp_path: Path) -> None:
    db = tmp_path / "mail.sqlite"
    storage_ref = seed_message(db)

    recent = run_cli(db, "recent", "--account", "work", "--limit", "10")
    search = run_cli(db, "search", "manuscript", "--account", "work")
    read = run_cli(db, "read", storage_ref)

    assert recent.returncode == 0, recent.stderr
    assert search.returncode == 0, search.stderr
    assert read.returncode == 0, read.stderr

    recent_payload = json.loads(recent.stdout)
    search_payload = json.loads(search.stdout)
    read_payload = json.loads(read.stdout)

    assert recent_payload["ok"] is True
    assert recent_payload["messages"][0]["subject"] == "Research thread"
    assert search_payload["messages"][0]["storage_ref"] == storage_ref
    assert "Please review" in read_payload["message"]["body_text"]


def test_app_contribution_abi_describes_declared_refs_and_reads_package_owned_data(
    tmp_path: Path,
) -> None:
    db = tmp_path / "mail.sqlite"
    seed_message(db)
    schema = "opl-package-app-contribution-request.v1"

    describe = run_app_contribution(
        db,
        {
            "schema_version": schema,
            "operation": "describe",
            "ref": "communications.mail.v1#draft.send",
        },
    )
    recent = run_app_contribution(
        db,
        {
            "schema_version": schema,
            "operation": "read",
            "ref": "communications.mail.v1#recent",
            "input": {"account": "work", "limit": 10},
        },
    )
    memory = run_app_contribution(
        db,
        {
            "schema_version": schema,
            "operation": "read",
            "ref": "personal.memory.v1#search",
        },
        memory_db=tmp_path / "memory.sqlite",
    )

    assert describe.returncode == 0, describe.stderr
    assert recent.returncode == 0, recent.stderr
    assert memory.returncode == 0, memory.stderr
    described = json.loads(describe.stdout)
    recent_payload = json.loads(recent.stdout)
    memory_payload = json.loads(memory.stdout)
    assert described["schema_version"] == "opl-package-app-contribution-response.v1"
    assert described["result"]["abi"] == "opl-package-app-contribution-cli.v1"
    assert described["result"]["operations"] == [{
        "operation": "execute",
        "confirmation_required": True,
        "input": {
        "draft_ref": {"type": "string", "required": True},
        "approval": {"type": "string", "required": True},
        },
        "result": "communications.mail.v1#draft.send.result",
    }]
    assert recent_payload["result"]["messages"][0]["storage_ref"].startswith("email-store://")
    assert memory_payload["result"] == {"memories": []}


def test_app_contribution_reads_facts_only_triage_evidence(tmp_path: Path) -> None:
    db = tmp_path / "mail.sqlite"
    source_ref = seed_message(db)

    result = run_app_contribution(
        db,
        {
            "schema_version": "opl-package-app-contribution-request.v1",
            "operation": "read",
            "ref": "communications.mail.v1#triage.evidence",
            "input": {
                "source_ref": source_ref,
                "policy_refs": ["policy://persona/mail-triage/v1"],
            },
        },
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    evidence = payload["result"]["evidence"]
    assert evidence["schema_version"] == "opl-relay-mail-triage-evidence.v2"
    assert evidence["source_refs"] == [source_ref]
    assert evidence["risk"]["external_write_allowed"] is False
    assert evidence["provider_write"]["status"] == "unreachable"


def test_app_contribution_creates_review_draft_from_approved_persona_bundle(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    calls: dict[str, object] = {}

    class FakeService:
        def create(self, **kwargs: object) -> dict[str, object]:
            calls.update(kwargs)
            return {
                "draft_ref": "mail-draft://apple-mail/work/persona",
                "state": "draft",
            }

    monkeypatch.setattr(
        cli,
        "load_account",
        lambda path, account_id: SimpleNamespace(
            account_id=account_id,
            email="work@example.test",
        ),
    )
    monkeypatch.setattr(cli, "draft_service", lambda args: (FakeService(), object()))
    bundle = {
        "schema_version": "opl-persona-proposal.v1",
        "proposals": [
            {
                "proposal_id": "persona-proposal://memo/1",
                "proposal_kind": "mail.draft_context",
                "target": "opl-relay.draft.context",
                "operation": "prepare",
                "payload": {
                    "subject_hint": "Technical memo",
                    "body_context": "Evidence-backed context.",
                    "tags": ["OPL"],
                },
                "source_refs": ["obsidian://vault/memo.md"],
                "approval": {
                    "status": "approved",
                    "required": True,
                    "external_write_allowed": False,
                    "approval_ref": "approval://user/example",
                },
            }
        ],
    }
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "schema_version": "opl-package-app-contribution-request.v1",
                    "operation": "execute",
                    "ref": "communications.mail.v1#draft.create_from_persona",
                    "input": {
                        "proposal_bundle": bundle,
                        "account": "work",
                        "to": ["reviewer@example.test"],
                    },
                }
            )
        ),
    )

    assert (
        cli.main(
            [
                "--json",
                "--draft-db",
                str(tmp_path / "drafts.sqlite"),
                "app-contribution",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["review_required"] is True
    assert payload["result"]["send_allowed"] is False
    assert payload["result"]["proposal_id"] == "persona-proposal://memo/1"
    assert calls["subject"] == "Technical memo"
    assert calls["body_text"] == "Evidence-backed context."
    assert calls["to"][0].address == "reviewer@example.test"


def test_app_contribution_rejects_unapproved_persona_bundle_before_service(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    class ExplodingService:
        def create(self, **kwargs: object) -> dict[str, object]:
            raise AssertionError("unapproved proposal reached draft service")

    monkeypatch.setattr(cli, "draft_service", lambda args: (ExplodingService(), object()))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "schema_version": "opl-package-app-contribution-request.v1",
                    "operation": "execute",
                    "ref": "communications.mail.v1#draft.create_from_persona",
                    "input": {
                        "proposal_bundle": {
                            "schema_version": "opl-persona-proposal.v1",
                            "proposals": [
                                {
                                    "proposal_kind": "mail.draft_context",
                                    "target": "opl-relay.draft.context",
                                    "operation": "prepare",
                                    "payload": {
                                        "subject_hint": "Subject",
                                        "body_context": "Body",
                                    },
                                    "source_refs": ["obsidian://vault/memo.md"],
                                    "approval": {
                                        "status": "pending",
                                        "required": True,
                                        "external_write_allowed": False,
                                    },
                                }
                            ],
                        },
                        "account": "work",
                        "to": ["reviewer@example.test"],
                    },
                }
            )
        ),
    )

    assert (
        cli.main(
            [
                "--json",
                "--draft-db",
                str(tmp_path / "drafts.sqlite"),
                "app-contribution",
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_request"


def test_app_contribution_abi_rejects_undeclared_refs_and_cross_kind_calls(tmp_path: Path) -> None:
    db = tmp_path / "mail.sqlite"
    unknown = run_app_contribution(
        db,
        {
            "schema_version": "opl-package-app-contribution-request.v1",
            "operation": "read",
            "ref": "communications.mail.v1#unknown",
        },
    )
    wrong_kind = run_app_contribution(
        db,
        {
            "schema_version": "opl-package-app-contribution-request.v1",
            "operation": "execute",
            "ref": "communications.mail.v1#recent",
        },
    )
    unsupported_input = run_app_contribution(
        db,
        {
            "schema_version": "opl-package-app-contribution-request.v1",
            "operation": "read",
            "ref": "communications.mail.v1#recent",
            "input": {"private_path": "/tmp/relay.sqlite"},
        },
    )

    for result in [unknown, wrong_kind, unsupported_input]:
        assert result.returncode == 2
        payload = json.loads(result.stdout)
        assert payload["schema_version"] == "opl-package-app-contribution-response.v1"
        assert payload["ok"] is False
        assert payload["error"]["code"] == "invalid_request"


def test_app_contribution_abi_describes_shared_read_and_execute_ref(tmp_path: Path) -> None:
    result = run_app_contribution(
        tmp_path / "mail.sqlite",
        {
            "schema_version": "opl-package-app-contribution-request.v1",
            "operation": "describe",
            "ref": "communications.mail.v1#draft.inspect",
        },
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert [entry["operation"] for entry in payload["result"]["operations"]] == [
        "read",
        "execute",
    ]


def test_app_contribution_abi_routes_send_only_with_package_owned_approval(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    calls: dict[str, str] = {}

    class FakeService:
        def send(self, draft_ref: str, *, approval: str) -> dict[str, str]:
            calls["draft_ref"] = draft_ref
            calls["approval"] = approval
            return {"draft_ref": draft_ref, "state": "sent"}

    monkeypatch.setattr(cli, "draft_service", lambda args: (FakeService(), object()))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "schema_version": "opl-package-app-contribution-request.v1",
                    "operation": "execute",
                    "ref": "communications.mail.v1#draft.send",
                    "input": {
                        "draft_ref": "mail-draft://apple-mail/work/uuid",
                        "approval": "sha256:current-approval",
                    },
                }
            )
        ),
    )

    assert cli.main(["--json", "--draft-db", str(tmp_path / "drafts.sqlite"), "app-contribution"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert calls == {
        "draft_ref": "mail-draft://apple-mail/work/uuid",
        "approval": "sha256:current-approval",
    }
    assert payload["result"]["draft"]["state"] == "sent"


def test_cli_recent_filters_by_date_window(tmp_path: Path) -> None:
    db = tmp_path / "mail.sqlite"
    conn = connect_email_store(db)
    try:
        for uid, subject, date_iso in [
            (1, "older thread", "2026-05-16T09:00:00+08:00"),
            (2, "current thread", "2026-05-17T09:00:00+08:00"),
        ]:
            upsert_email_message(
                conn,
                account_id="work",
                folder="INBOX",
                folder_slug="INBOX",
                uid=uid,
                uidvalidity=1,
                message_id=f"<cli-{uid}@example.test>",
                subject=subject,
                sender="editor@example.test",
                recipient="work@example.com",
                date_iso=date_iso,
                raw_sha256=str(uid) * 64,
                raw_eml=b"Subject: test\r\n\r\nbody",
                attachments=[],
                ingest_ts=date_iso,
            )
    finally:
        conn.close()

    result = run_cli(
        db,
        "recent",
        "--account",
        "work",
        "--since",
        "2026-05-17T00:00:00+08:00",
        "--until",
        "2026-05-18T00:00:00+08:00",
        "--limit",
        "10",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert [row["subject"] for row in payload["messages"]] == ["current thread"]


def test_cli_doctor_reports_current_private_paths(tmp_path: Path) -> None:
    db = tmp_path / "mail.sqlite"
    result = run_cli(db, "doctor")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["keychain_services"] == ["codex-mail-workbench"]
    assert payload["product"] == "opl-relay"
    assert payload["workspace"]["path"]
    assert payload["draft_db_path"].endswith("drafts.sqlite")
    assert payload["memory_db_path"].endswith("memory.sqlite")
    assert payload["sources_config_path"].endswith("sources.toml")


def test_cli_exposes_workspace_commands() -> None:
    parser = cli.build_parser()
    for command in [
        ["workspace", "inspect"],
        ["workspace", "init"],
        ["workspace", "migrate", "--from", "/tmp/source-overlay"],
    ]:
        args = parser.parse_args(command)
        assert callable(args.func)

    with pytest.raises(SystemExit):
        parser.parse_args(["--workspace", "/tmp/another-profile", "workspace", "inspect"])


def test_cli_exposes_persona_draft_create_command() -> None:
    args = cli.build_parser().parse_args(
        [
            "persona",
            "draft-create",
            "--input",
            "proposal.json",
            "--account",
            "work",
            "--to",
            "reviewer@example.test",
        ]
    )
    assert callable(args.func)


def test_cli_persona_draft_create_reads_approved_bundle_and_only_creates(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    calls: dict[str, object] = {}

    class FakeService:
        def create(self, **kwargs: object) -> dict[str, object]:
            calls.update(kwargs)
            return {"draft_ref": "mail-draft://apple-mail/work/persona", "state": "draft"}

    monkeypatch.setattr(
        cli,
        "load_account",
        lambda path, account_id: SimpleNamespace(
            account_id=account_id,
            email="work@example.test",
        ),
    )
    monkeypatch.setattr(cli, "draft_service", lambda args: (FakeService(), object()))
    proposal_path = tmp_path / "approved.json"
    proposal_path.write_text(
        json.dumps(
            {
                "schema_version": "opl-persona-proposal.v1",
                "proposals": [
                    {
                        "proposal_id": "persona-proposal://memo/1",
                        "proposal_kind": "mail.draft_context",
                        "target": "opl-relay.draft.context",
                        "operation": "prepare",
                        "payload": {
                            "subject_hint": "Technical memo",
                            "body_context": "Evidence-backed context.",
                            "tags": [],
                        },
                        "source_refs": ["obsidian://vault/memo.md"],
                        "approval": {
                            "status": "approved",
                            "required": True,
                            "external_write_allowed": False,
                            "approval_ref": "approval://user/example",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert (
        cli.main(
            [
                "--json",
                "--draft-db",
                str(tmp_path / "drafts.sqlite"),
                "persona",
                "draft-create",
                "--input",
                str(proposal_path),
                "--account",
                "work",
                "--to",
                "reviewer@example.test",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["handoff"]["send_allowed"] is False
    assert calls["subject"] == "Technical memo"


def test_cli_memory_candidate_approval_flow(tmp_path: Path) -> None:
    db = tmp_path / "mail.sqlite"
    memory_db = tmp_path / "memory.sqlite"
    entity_result = run_cli(
        db,
        "--memory-db",
        str(memory_db),
        "memory",
        "entity",
        "upsert",
        "--kind",
        "person",
        "--name",
        "Professor Example",
        "--email",
        "person@example.test",
    )
    assert entity_result.returncode == 0, entity_result.stderr
    entity_ref = json.loads(entity_result.stdout)["entity"]["entity_ref"]

    proposal = run_cli(
        db,
        "--memory-db",
        str(memory_db),
        "memory",
        "propose",
        "--entity",
        entity_ref,
        "--category",
        "event",
        "--content",
        "We met at the annual consortium meeting.",
        "--source",
        "user://statement/2026-07-27",
    )
    assert proposal.returncode == 0, proposal.stderr
    memory_ref = json.loads(proposal.stdout)["memory"]["memory_ref"]

    before = run_cli(db, "--memory-db", str(memory_db), "memory", "search")
    approved = run_cli(
        db,
        "--memory-db",
        str(memory_db),
        "memory",
        "approve",
        memory_ref,
    )
    after = run_cli(db, "--memory-db", str(memory_db), "memory", "search")

    assert json.loads(before.stdout)["memories"] == []
    assert json.loads(approved.stdout)["memory"]["status"] == "approved"
    assert json.loads(after.stdout)["memories"][0]["memory_ref"] == memory_ref


def test_cli_exposes_draft_lifecycle() -> None:
    parser = cli.build_parser()
    for action in ["create", "adopt", "inspect", "open", "send"]:
        if action == "create":
            args = parser.parse_args(
                [
                    "draft",
                    action,
                    "--account",
                    "work",
                    "--to",
                    "reviewer@example.test",
                    "--subject",
                    "Review",
                    "--body",
                    "Body",
                ]
            )
        elif action == "adopt":
            args = parser.parse_args(
                [
                    "draft",
                    action,
                    "--account",
                    "work",
                    "--apple-mail-uuid",
                    "uuid",
                ]
            )
        elif action == "send":
            args = parser.parse_args(
                ["draft", action, "mail-draft://apple-mail/work/uuid", "--approval", "sha256:x"]
            )
        else:
            args = parser.parse_args(
                ["draft", action, "mail-draft://apple-mail/work/uuid"]
            )
        assert callable(args.func)


def test_cli_exposes_memory_sources_and_context_commands() -> None:
    parser = cli.build_parser()
    commands = [
        ["memory", "candidates"],
        ["memory", "search"],
        ["sources", "list"],
        ["sources", "index"],
        ["sources", "search", "query"],
        ["context", "build", "--person", "Professor Example"],
    ]
    for command in commands:
        args = parser.parse_args(command)
        assert callable(args.func)


def test_cli_draft_create_routes_to_service(monkeypatch, capsys, tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    class FakeService:
        def create(self, **kwargs: object) -> dict[str, object]:
            calls.update(kwargs)
            return {"draft_ref": "mail-draft://apple-mail/work/uuid"}

    monkeypatch.setattr(
        cli,
        "load_account",
        lambda path, account_id: SimpleNamespace(
            account_id=account_id,
            email="work@example.test",
        ),
    )
    monkeypatch.setattr(
        cli,
        "draft_service",
        lambda args: (FakeService(), object()),
    )

    result = cli.main(
        [
            "--json",
            "--draft-db",
            str(tmp_path / "drafts.sqlite"),
            "draft",
            "create",
            "--account",
            "work",
            "--to",
            "Reviewer <reviewer@example.test>",
            "--subject",
            "Review request",
            "--body",
            "\nFirst paragraph.\n\nSecond paragraph.",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["draft"]["draft_ref"].startswith("mail-draft://")
    assert calls["sender"] == "work@example.test"
    assert calls["to"][0].address == "reviewer@example.test"
