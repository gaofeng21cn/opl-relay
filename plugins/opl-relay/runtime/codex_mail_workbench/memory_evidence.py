from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .knowledge import load_sources_config, resolve_source_ref
from .store import connect_email_store_readonly, fetch_raw_email_by_storage_ref, get_message_by_storage_ref


class MemoryEvidence:
    """Request-local source verification; it never changes historical memory."""

    def __init__(self, *, mail_db_path: Path, sources_config_path: Path):
        self.mail_db_path = mail_db_path
        self.sources = load_sources_config(sources_config_path)

    @classmethod
    def beside_store(cls, store_path: Path) -> MemoryEvidence:
        return cls(mail_db_path=store_path.parent / "mail.sqlite",
                   sources_config_path=store_path.parent / "sources.toml")

    def source_digest(self, source_ref: str, source_kind: str) -> str:
        if source_kind == "user" and source_ref.startswith("user://") and len(source_ref) > 7:
            return ""
        if source_kind in {"obsidian", "markdown"}:
            if not source_ref.startswith(source_kind + "://"):
                raise ValueError("source_kind_mismatch")
            return hashlib.sha256(resolve_source_ref(source_ref, self.sources)).hexdigest()
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

    def check(self, memory: dict[str, Any], *, approving: bool = False) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        reasons: list[str] = []
        statuses = {"candidate", "approved"} if approving else {"approved"}
        if memory.get("status") not in statuses:
            reasons.append("memory_not_approved")
        for field in ("valid_from", "valid_until"):
            value = memory.get(field)
            if not value:
                continue
            try:
                instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if instant.tzinfo is None:
                    instant = instant.replace(tzinfo=timezone.utc)
                if field == "valid_from" and instant > now:
                    reasons.append("memory_not_yet_valid")
                if field == "valid_until" and instant <= now:
                    reasons.append("memory_expired")
            except ValueError:
                reasons.append("memory_validity_invalid")
        if not memory.get("sources"):
            reasons.append("memory_sources_missing")
        for source in memory.get("sources", []):
            try:
                actual = self.source_digest(source["source_ref"], source["source_kind"])
                expected = str(source.get("source_sha256") or "").removeprefix("sha256:")
                if actual and not re.fullmatch(r"[0-9a-f]{64}", expected):
                    reasons.append("source_revision_missing")
                elif actual and actual != expected:
                    reasons.append("source_revision_changed")
            except (OSError, ValueError, sqlite3.Error) as exc:
                code = str(exc) if isinstance(exc, ValueError) else "source_unavailable"
                reasons.append(code)
        return {"status": "needs_review" if reasons else "current",
                "checked_at": now.isoformat(), "reasons": list(dict.fromkeys(reasons))}

    def current(self, memory: dict[str, Any]) -> bool:
        memory["source_currentness"] = self.check(memory)
        return memory["source_currentness"]["status"] == "current"

    def inspect(self, memory: dict[str, Any] | None) -> dict[str, Any] | None:
        if memory is None:
            return None
        return {**memory, "source_currentness": self.check(memory)}
