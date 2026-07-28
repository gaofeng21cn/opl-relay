import json
from pathlib import Path

import pytest

from codex_mail_workbench.persona import (
    load_persona_mail_context,
    validate_approved_persona_draft_context,
)


def approved_bundle(**overrides: object) -> dict[str, object]:
    proposal: dict[str, object] = {
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
    proposal.update(overrides)
    return {
        "schema_version": "opl-persona-proposal.v1",
        "proposals": [proposal],
    }


def test_persona_mail_context_is_read_only(tmp_path: Path) -> None:
    bundle = {
        "schema_version": "opl-persona-proposal.v1",
        "proposals": [
            {
                "proposal_id": "persona-proposal://memo/1",
                "target": "opl-relay.draft.context",
                "payload": {
                    "subject_hint": "Technical memo",
                    "body_context": "Evidence-backed context.",
                    "tags": ["OPL"],
                },
                "source_refs": ["obsidian://vault/memo.md"],
                "approval": {"external_write_allowed": False},
            }
        ],
    }
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    result = load_persona_mail_context(path)
    assert result["contexts"][0]["subject_hint"] == "Technical memo"
    assert result["contexts"][0]["send_allowed"] is False
    assert result["mutation_policy"] == "read_only_until_user_approval"


def test_persona_context_rejects_unapproved_write_flag(tmp_path: Path) -> None:
    path = tmp_path / "bundle.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "opl-persona-proposal.v1",
                "proposals": [
                    {
                        "target": "opl-relay.draft.context",
                        "payload": {},
                        "approval": {"external_write_allowed": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="review-gated"):
        load_persona_mail_context(path)


def test_approved_persona_draft_context_is_strict_and_send_gated() -> None:
    result = validate_approved_persona_draft_context(approved_bundle())
    assert result == {
        "proposal_id": "persona-proposal://memo/1",
        "approval_ref": "approval://user/example",
        "subject": "Technical memo",
        "body_text": "Evidence-backed context.",
        "tags": ["OPL"],
        "source_refs": ["obsidian://vault/memo.md"],
        "review_required": True,
        "send_allowed": False,
    }


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {"approval": {"status": "pending", "required": True, "external_write_allowed": False}},
            "not approved",
        ),
        (
            {
                "approval": {
                    "status": "approved",
                    "required": True,
                    "external_write_allowed": True,
                    "approval_ref": "approval://user/example",
                }
            },
            "must not authorize sending",
        ),
        (
            {"source_refs": []},
            "source_refs",
        ),
        (
            {"target": "gflab_web.content.post"},
            "target does not match",
        ),
        (
            {"payload": {"subject_hint": "Subject", "body_context": "Body", "send": True}},
            "unsupported fields",
        ),
    ],
)
def test_approved_persona_draft_context_fails_closed(
    change: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_approved_persona_draft_context(approved_bundle(**change))


def test_approved_persona_draft_context_rejects_unsafe_text() -> None:
    with pytest.raises(ValueError, match="subject_hint"):
        validate_approved_persona_draft_context(
            approved_bundle(
                payload={
                    "subject_hint": "Subject\nInjected header",
                    "body_context": "Body",
                    "tags": [],
                }
            )
        )
