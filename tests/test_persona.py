import json
from pathlib import Path

import pytest

from codex_mail_workbench.persona import load_persona_mail_context


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
