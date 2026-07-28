from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


PERSONA_PROPOSAL_SCHEMA = "opl-persona-proposal.v1"
RELAY_DRAFT_CONTEXT_TARGET = "opl-relay.draft.context"
PERSONA_DRAFT_CONTEXT_KIND = "mail.draft_context"
PERSONA_DRAFT_CONTEXT_OPERATION = "prepare"
_REFERENCE_PATTERN = re.compile(r"^[a-z][a-z0-9+.-]*://\S+$")
_MAX_SUBJECT_LENGTH = 998
_MAX_BODY_LENGTH = 1_000_000
_MAX_TAG_LENGTH = 128


def _read_bundle(path: Path) -> object:
    return json.loads(
        path.read_text(encoding="utf-8") if str(path) != "-" else sys.stdin.read()
    )


def _validate_reference(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 2048
        or not _REFERENCE_PATTERN.fullmatch(value)
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError(f"{field} must be a safe URI reference")
    return value


def _validated_text(
    value: object,
    *,
    field: str,
    max_length: int,
    allow_newlines: bool,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field} must be non-empty")
    if len(text) > max_length or "\0" in text:
        raise ValueError(f"{field} is unsafe or too large")
    if not allow_newlines and ("\r" in text or "\n" in text):
        raise ValueError(f"{field} must not contain line breaks")
    return text


def validate_approved_persona_draft_context(bundle: object) -> dict[str, Any]:
    """Validate one approved Persona proposal for Relay draft creation.

    This approves only creation of an Apple Mail review draft. Relay's
    fingerprint-bound send flow remains a separate operation.
    """
    if not isinstance(bundle, dict):
        raise ValueError("Persona input must be a JSON object")
    if bundle.get("schema_version") != PERSONA_PROPOSAL_SCHEMA:
        raise ValueError("unsupported Persona proposal schema")
    proposals = bundle.get("proposals")
    if not isinstance(proposals, list) or not all(
        isinstance(item, dict) for item in proposals
    ):
        raise ValueError("Persona proposals must be a list of objects")
    matches = [
        proposal
        for proposal in proposals
        if proposal.get("target") == RELAY_DRAFT_CONTEXT_TARGET
    ]
    if not matches:
        raise ValueError("Persona proposal target does not match Relay draft context")
    if len(matches) != 1:
        raise ValueError("Persona proposal bundle must contain exactly one Relay draft context")
    proposal = matches[0]

    if proposal.get("proposal_kind") != PERSONA_DRAFT_CONTEXT_KIND:
        raise ValueError("Persona proposal kind does not match mail.draft_context")
    if proposal.get("operation") != PERSONA_DRAFT_CONTEXT_OPERATION:
        raise ValueError("Persona proposal operation must be prepare")
    proposal_id = _validate_reference(
        proposal.get("proposal_id"),
        field="proposal_id",
    )

    source_refs_value = proposal.get("source_refs")
    if (
        not isinstance(source_refs_value, list)
        or not source_refs_value
        or not all(isinstance(item, str) for item in source_refs_value)
    ):
        raise ValueError("Persona proposal requires non-empty source_refs")
    source_refs = [
        _validate_reference(item, field="source_refs")
        for item in source_refs_value
    ]
    if len(source_refs) != len(set(source_refs)):
        raise ValueError("Persona proposal source_refs must be unique")

    approval = proposal.get("approval")
    if not isinstance(approval, dict):
        raise ValueError("Persona proposal requires approval evidence")
    if approval.get("required") is not True:
        raise ValueError("Persona proposal approval.required must be true")
    if approval.get("status") != "approved":
        raise ValueError("Persona proposal is not approved")
    if approval.get("external_write_allowed") is not False:
        raise ValueError("Persona proposal must not authorize sending")
    approval_ref = _validate_reference(
        approval.get("approval_ref"),
        field="approval.approval_ref",
    )

    payload = proposal.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Persona mail context payload must be an object")
    unexpected = sorted(set(payload) - {"subject_hint", "body_context", "tags"})
    if unexpected:
        raise ValueError(
            "Persona mail context payload contains unsupported fields: "
            + ", ".join(unexpected)
        )
    subject = _validated_text(
        payload.get("subject_hint"),
        field="subject_hint",
        max_length=_MAX_SUBJECT_LENGTH,
        allow_newlines=False,
    )
    body = _validated_text(
        payload.get("body_context"),
        field="body_context",
        max_length=_MAX_BODY_LENGTH,
        allow_newlines=True,
    )
    tags_value = payload.get("tags", [])
    if not isinstance(tags_value, list) or not all(
        isinstance(item, str) for item in tags_value
    ):
        raise ValueError("tags must be a list of strings")
    tags: list[str] = []
    for item in tags_value:
        tag = item.strip()
        if (
            not tag
            or len(tag) > _MAX_TAG_LENGTH
            or any(ord(char) < 32 for char in tag)
        ):
            raise ValueError("tags must contain safe non-empty strings")
        tags.append(tag)
    if len(tags) != len(set(tags)):
        raise ValueError("tags must be unique")

    return {
        "proposal_id": proposal_id,
        "approval_ref": approval_ref,
        "subject": subject,
        "body_text": body,
        "tags": tags,
        "source_refs": source_refs,
        "review_required": True,
        "send_allowed": False,
    }


def load_approved_persona_draft_context(path: Path) -> dict[str, Any]:
    return validate_approved_persona_draft_context(_read_bundle(path))


def load_persona_mail_context(path: Path) -> dict[str, Any]:
    """Read a Persona proposal bundle without creating or sending a draft."""
    bundle = _read_bundle(path)
    if not isinstance(bundle, dict):
        raise ValueError("Persona input must be a JSON object")
    if bundle.get("schema_version") != PERSONA_PROPOSAL_SCHEMA:
        raise ValueError("unsupported Persona proposal schema")
    contexts: list[dict[str, Any]] = []
    for proposal in bundle.get("proposals", []):
        if not isinstance(proposal, dict):
            raise ValueError("Persona proposals must be objects")
        if proposal.get("target") != RELAY_DRAFT_CONTEXT_TARGET:
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
