from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import connect_email_store_readonly, fetch_raw_email_by_storage_ref, get_message_by_storage_ref


def _instant(value: Any) -> datetime:
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


class MemoryEvidence:
    """Verify evidence and claim freshness separately, without changing history."""

    def __init__(self, *, mail_db_path: Path,
                 workspace_root: Path | None = None, project_root: Path | None = None,
                 framework: Any = None):
        self.mail_db_path = mail_db_path
        if framework is None:
            from .knowledge import FrameworkKnowledge
            framework = FrameworkKnowledge(workspace_root=workspace_root, project_root=project_root)
        self.framework = framework

    @classmethod
    def beside_store(cls, store_path: Path) -> MemoryEvidence:
        # Relay stores are always Profile/data/relay children; do not rediscover
        # a second source registry beside the SQLite database.
        return cls(mail_db_path=store_path.parent / "mail.sqlite",
                   workspace_root=store_path.parent.parent.parent)

    def source_digest(self, source_ref: str, source_kind: str) -> str:
        if source_kind == "user" and source_ref.startswith("user://") and len(source_ref) > 7:
            return ""
        if source_kind == "knowledge" and source_ref.startswith("knowledge://"):
            knowledge_id = source_ref.removeprefix("knowledge://")
            if not knowledge_id:
                raise ValueError("source_identity_missing")
            source = self.framework.read_source(knowledge_id)
            digest = str(source.get("sha256", "")).removeprefix("sha256:")
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("source_revision_missing")
            return digest
        if source_kind in {"obsidian", "markdown"}:
            raise ValueError("source_requires_framework_registration")
        if source_kind == "email" and source_ref.startswith("email-store://"):
            conn = connect_email_store_readonly(self.mail_db_path)
            if conn is None:
                raise ValueError("source_unavailable")
            try:
                meta = get_message_by_storage_ref(conn, source_ref)
                data = fetch_raw_email_by_storage_ref(conn, source_ref)
                if meta is None or data is None:
                    raise ValueError("source_unavailable")
                digest = hashlib.sha256(data).hexdigest()
                if digest != meta["raw_sha256"]:
                    raise ValueError("source_integrity_mismatch")
                return digest
            finally:
                conn.close()
        raise ValueError("source_verification_unavailable")

    def _canonical_reasons(self, memory: dict[str, Any]) -> list[str]:
        knowledge_id = memory.get("knowledge_id")
        if not knowledge_id:
            return []
        versions = memory.get("source_versions") or {}
        target = memory.get("canonical_target") or {}
        expected = str(versions.get(knowledge_id, ""))
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
            return ["canonical_source_revision_missing"]
        if (target.get("sha256") != expected or not target.get("source_id") or not target.get("ref")
                or memory.get("writeback_status") not in {"accepted", "applied"}):
            return ["canonical_link_incomplete"]
        try:
            checked = self.framework.verify_dependencies(versions)
            if checked.get("valid") is not True:
                return list(map(str, checked.get("issues") or ["canonical_source_unavailable"]))
            source = self.framework.read_source(knowledge_id)
            if source.get("sha256") != expected:
                return ["canonical_source_revision_changed"]
            # Framework owns source resolution; linkage must not silently follow
            # a reused knowledge id to a different physical owner or scope.
            if source.get("source_id") != target["source_id"] or source.get("source_ref") != target["ref"]:
                return ["canonical_source_identity_changed"]
            if source.get("scope") != memory.get("scope") or source.get("content_kind") != memory.get("content_kind"):
                return ["canonical_source_semantics_changed"]
        except (OSError, ValueError) as exc:
            return [str(exc) if isinstance(exc, ValueError) else "canonical_source_unavailable"]
        return []

    def check(self, memory: dict[str, Any], *, approving: bool = False) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        evidence_reasons: list[str] = []
        validity_reasons: list[str] = []
        claim_reasons: list[str] = []
        lifecycle_reasons: list[str] = []
        statuses = {"candidate", "approved"} if approving else {"approved"}
        if memory.get("status") not in statuses:
            lifecycle_reasons.append("memory_not_approved")
        for field in ("valid_from", "valid_until"):
            value = memory.get(field)
            if not value:
                continue
            try:
                instant = _instant(value)
                if field == "valid_from" and instant > now:
                    validity_reasons.append("memory_not_yet_valid")
                if field == "valid_until" and instant <= now:
                    validity_reasons.append("memory_expired")
            except ValueError:
                validity_reasons.append("memory_validity_invalid")
        sources = memory.get("sources") or []
        if not sources:
            evidence_reasons.append("memory_sources_missing")
        for source in sources:
            try:
                actual = self.source_digest(str(source.get("source_ref", "")),
                                            str(source.get("source_kind", "")))
                expected = str(source.get("source_sha256") or "").removeprefix("sha256:")
                if actual and not re.fullmatch(r"[0-9a-f]{64}", expected):
                    evidence_reasons.append("source_revision_missing")
                elif actual and actual != expected:
                    evidence_reasons.append("source_revision_changed")
            except (OSError, ValueError, sqlite3.Error) as exc:
                evidence_reasons.append(str(exc) if isinstance(exc, ValueError) else "source_unavailable")
        evidence_reasons.extend(self._canonical_reasons(memory))

        historical = memory.get("category") == "event"
        if historical:
            if not memory.get("occurred_at"):
                claim_reasons.append("historical_event_date_missing")
            else:
                try:
                    if _instant(memory["occurred_at"]) > now:
                        claim_reasons.append("historical_event_in_future")
                except ValueError:
                    claim_reasons.append("historical_event_date_invalid")
        elif not memory.get("knowledge_id") and any(source.get("source_kind") == "user" for source in sources):
            claim_reasons.append("canonical_link_required")

        dynamic_claim = not historical and (
            memory.get("content_kind", "state") == "state" or memory.get("category") == "commitment")
        if dynamic_claim:
            for field, missing, invalid in (
                ("observed_at", "claim_observed_at_missing", "claim_observed_at_invalid"),
                ("next_review_at", "claim_review_deadline_missing", "claim_review_deadline_invalid"),
            ):
                value = memory.get(field)
                if not value:
                    claim_reasons.append(missing)
                    continue
                try:
                    instant = _instant(value)
                    if field == "observed_at" and instant > now:
                        claim_reasons.append("claim_observation_in_future")
                    if field == "next_review_at" and instant <= now:
                        claim_reasons.append("claim_review_overdue")
                except ValueError:
                    claim_reasons.append(invalid)
            if memory.get("observed_at") and memory.get("next_review_at"):
                try:
                    if _instant(memory["next_review_at"]) <= _instant(memory["observed_at"]):
                        claim_reasons.append("claim_review_interval_invalid")
                except ValueError:
                    pass

        evidence_reasons = list(dict.fromkeys(evidence_reasons))
        claim_reasons = list(dict.fromkeys([*validity_reasons, *claim_reasons]))
        claim_status = "needs_review" if claim_reasons else "historical" if historical else "current"
        # Domain approval records judgment of evidence, not publication as a
        # current cross-Agent fact. Canonical promotion is a separate operation.
        blocking = [*lifecycle_reasons, *evidence_reasons, *validity_reasons]
        if not approving:
            blocking.extend(claim_reasons)
        reasons = list(dict.fromkeys(blocking))
        status = "needs_review" if reasons else "current" if approving else claim_status
        return {
            "status": status, "checked_at": now.isoformat(), "reasons": reasons,
            "evidence_currentness": {"status": "needs_review" if evidence_reasons else "current",
                                     "reasons": evidence_reasons},
            "claim_freshness": {"status": claim_status, "reasons": claim_reasons,
                                "observed_at": memory.get("observed_at", ""),
                                "next_review_at": memory.get("next_review_at", "")},
            "repair": {"requires_canonical_link": "canonical_link_required" in claim_reasons,
                       "requires_semantic_review": claim_status == "needs_review"},
        }

    def current(self, memory: dict[str, Any]) -> bool:
        memory["source_currentness"] = self.check(memory)
        memory["evidence_currentness"] = memory["source_currentness"]["evidence_currentness"]
        memory["claim_freshness"] = memory["source_currentness"]["claim_freshness"]
        return memory["source_currentness"]["status"] == "current"

    def inspect(self, memory: dict[str, Any] | None) -> dict[str, Any] | None:
        if memory is None:
            return None
        currentness = self.check(memory)
        return {**memory, "source_currentness": currentness,
                "evidence_currentness": currentness["evidence_currentness"],
                "claim_freshness": currentness["claim_freshness"]}
