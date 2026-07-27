from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def load_persona_mail_context(path: Path) -> dict[str, Any]:
    """Read a Persona proposal bundle without creating or sending a draft."""
    bundle = json.loads(
        path.read_text(encoding="utf-8") if str(path) != "-" else sys.stdin.read()
    )
    if not isinstance(bundle, dict):
        raise ValueError("Persona input must be a JSON object")
    if bundle.get("schema_version") != "opl-persona-proposal.v1":
        raise ValueError("unsupported Persona proposal schema")
    contexts: list[dict[str, Any]] = []
    for proposal in bundle.get("proposals", []):
        if not isinstance(proposal, dict):
            raise ValueError("Persona proposals must be objects")
        if proposal.get("target") != "opl-relay.draft.context":
            continue
        if proposal.get("approval", {}).get("external_write_allowed") is not False:
            raise ValueError("Persona mail context must remain review-gated")
        payload = proposal.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("Persona mail context payload must be an object")
        contexts.append(
            {
                "proposal_id": proposal.get("proposal_id"),
                "subject_hint": str(payload.get("subject_hint") or "").strip(),
                "body_context": str(payload.get("body_context") or "").strip(),
                "tags": payload.get("tags", []),
                "source_refs": proposal.get("source_refs", []),
                "review_required": True,
                "send_allowed": False,
            }
        )
    return {
        "ok": True,
        "schema_version": "opl-relay-persona-mail-context.v1",
        "contexts": contexts,
        "mutation_policy": "read_only_until_user_approval",
    }
