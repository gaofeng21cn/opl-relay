from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .message import extract_text_body, parse_headers
from .store import (
    connect_email_store_readonly,
    fetch_raw_email_by_storage_ref,
    get_message_by_storage_ref,
)


TRIAGE_EVIDENCE_SCHEMA = "opl-relay-mail-triage-evidence.v2"
_EMAIL_STORE_REF = re.compile(
    r"^email-store://[^/\s]+/[^/\s]+/[1-9][0-9]*/[0-9a-f]{16}$"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_email_store_ref(value: object) -> str:
    if not isinstance(value, str) or not _EMAIL_STORE_REF.fullmatch(value):
        raise ValueError("source_ref must be a canonical email-store:// reference")
    return value


def _policy_refs(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("policy_refs must be a non-empty string array")
    refs = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if len(refs) != len(value) or len(set(refs)) != len(refs):
        raise ValueError("policy_refs must contain unique non-empty strings")
    return sorted(refs)


def policy_refs_digest(policy_refs: list[str]) -> str:
    """Return a digest of Relay policy references, never policy contents."""
    normalized = _policy_refs(policy_refs)
    payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_triage_evidence(
    *,
    mail_db_path: Path,
    source_ref: str,
    policy_refs: list[str],
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Read one stored message into a non-mutating triage evidence envelope."""
    source_ref = validate_email_store_ref(source_ref)
    normalized_policy_refs = _policy_refs(policy_refs)
    conn = connect_email_store_readonly(mail_db_path)
    if conn is None:
        raise FileNotFoundError(f"mail database not found: {mail_db_path}")
    try:
        message = get_message_by_storage_ref(conn, source_ref)
        raw = fetch_raw_email_by_storage_ref(conn, source_ref)
    finally:
        conn.close()
    if message is None or raw is None:
        raise ValueError("email evidence not found")

    headers = parse_headers(raw)
    recipient_addresses: list[str] = []
    for field in ("to_addresses", "cc_addresses", "bcc_addresses"):
        for recipient in headers[field]:
            assert isinstance(recipient, dict)
            address = str(recipient["address"]).casefold()
            if address not in recipient_addresses:
                recipient_addresses.append(address)
    return {
        "schema_version": TRIAGE_EVIDENCE_SCHEMA,
        "source_refs": [source_ref],
        "mail": {
            "source_ref": source_ref,
            "metadata": message,
            "headers": {
                field: str(headers[field])
                for field in ("subject", "from", "to", "cc", "bcc")
            },
            "routing_facts": {
                "to_addresses": headers["to_addresses"],
                "cc_addresses": headers["cc_addresses"],
                "bcc_addresses": headers["bcc_addresses"],
                "recipient_count": len(recipient_addresses),
                "is_unique_recipient": len(recipient_addresses) == 1,
            },
            "raw_readback": {
                "status": "available",
                "raw_sha256": message["raw_sha256"],
                "raw_eml_sha256": hashlib.sha256(raw).hexdigest(),
                "body_text": extract_text_body(raw),
            },
        },
        "freshness": {
            "status": "local_store_readback",
            "observed_at": observed_at or _utc_now(),
            "ingested_at": message["ingest_ts"],
            "message_date": message["date"],
            "external_mailbox_freshness": "unknown",
        },
        "policy": {
            "policy_refs": normalized_policy_refs,
            "policy_digest": policy_refs_digest(normalized_policy_refs),
        },
        "triage": {
            "mode": "evidence_only",
            "personal_judgment": "not_provided",
        },
        "risk": {
            "requires_human_review": True,
            "external_write_allowed": False,
            "provider_write_reachable": False,
        },
        "provider_write": {
            "status": "unreachable",
            "reason": "triage_evidence_is_read_only",
        },
        "mutation_boundary": "Relay triage evidence cannot directly write, send, alter, or delete mailbox data.",
    }


def validate_triage_evidence(value: object) -> dict[str, Any]:
    """Validate portable evidence without querying a mailbox or provider."""
    if not isinstance(value, dict):
        raise ValueError("triage evidence must be an object")
    if value.get("schema_version") != TRIAGE_EVIDENCE_SCHEMA:
        raise ValueError(f"schema_version must be {TRIAGE_EVIDENCE_SCHEMA}")

    source_refs = value.get("source_refs")
    if not isinstance(source_refs, list) or not source_refs:
        raise ValueError("source_refs must be a non-empty array")
    normalized_source_refs = [validate_email_store_ref(item) for item in source_refs]
    if len(set(normalized_source_refs)) != len(normalized_source_refs):
        raise ValueError("source_refs must not contain duplicates")

    mail = value.get("mail")
    if not isinstance(mail, dict) or mail.get("source_ref") != normalized_source_refs[0]:
        raise ValueError("mail.source_ref must match source_refs[0]")
    headers = mail.get("headers")
    if not isinstance(headers, dict):
        raise ValueError("mail.headers is required")
    for field in ("subject", "from", "to", "cc", "bcc"):
        if not isinstance(headers.get(field), str):
            raise ValueError(f"mail.headers.{field} is required")
    routing_facts = mail.get("routing_facts")
    if not isinstance(routing_facts, dict):
        raise ValueError("mail.routing_facts is required")
    for field in ("to_addresses", "cc_addresses", "bcc_addresses"):
        recipients = routing_facts.get(field)
        if not isinstance(recipients, list):
            raise ValueError(f"mail.routing_facts.{field} must be an array")
        for recipient in recipients:
            if (
                not isinstance(recipient, dict)
                or not isinstance(recipient.get("name"), str)
                or not isinstance(recipient.get("address"), str)
                or not recipient["address"].strip()
            ):
                raise ValueError(f"mail.routing_facts.{field} contains an invalid recipient")
    recipient_count = routing_facts.get("recipient_count")
    if isinstance(recipient_count, bool) or not isinstance(recipient_count, int) or recipient_count < 0:
        raise ValueError("mail.routing_facts.recipient_count must be a non-negative integer")
    if routing_facts.get("is_unique_recipient") != (recipient_count == 1):
        raise ValueError("mail.routing_facts.is_unique_recipient does not match recipient_count")
    raw_readback = mail.get("raw_readback")
    if not isinstance(raw_readback, dict) or raw_readback.get("status") != "available":
        raise ValueError("mail.raw_readback must be available")
    for field in ("raw_sha256", "raw_eml_sha256"):
        if not isinstance(raw_readback.get(field), str) or not raw_readback[field]:
            raise ValueError(f"mail.raw_readback.{field} is required")

    freshness = value.get("freshness")
    if not isinstance(freshness, dict) or freshness.get("status") != "local_store_readback":
        raise ValueError("freshness must record a local_store_readback")
    for field in ("observed_at", "ingested_at", "message_date"):
        if not isinstance(freshness.get(field), str) or not freshness[field]:
            raise ValueError(f"freshness.{field} is required")

    policy = value.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("policy is required")
    refs = _policy_refs(policy.get("policy_refs"))
    digest = policy.get("policy_digest")
    if not isinstance(digest, str) or digest != policy_refs_digest(refs):
        raise ValueError("policy_digest does not match policy_refs")

    triage = value.get("triage")
    if not isinstance(triage, dict) or triage.get("mode") != "evidence_only":
        raise ValueError("triage must remain evidence_only")
    risk = value.get("risk")
    if not isinstance(risk, dict) or risk.get("external_write_allowed") is not False:
        raise ValueError("triage evidence must forbid external writes")
    if risk.get("provider_write_reachable") is not False:
        raise ValueError("triage evidence must not expose provider writes")
    provider_write = value.get("provider_write")
    if not isinstance(provider_write, dict) or provider_write.get("status") != "unreachable":
        raise ValueError("provider_write must remain unreachable")

    return {
        "ok": True,
        "schema_version": TRIAGE_EVIDENCE_SCHEMA,
        "source_refs": normalized_source_refs,
        "policy_digest": digest,
        "provider_write": "unreachable",
    }
