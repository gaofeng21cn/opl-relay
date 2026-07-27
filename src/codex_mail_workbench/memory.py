from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ENTITY_KINDS = {"person", "organization", "project"}
MEMORY_CATEGORIES = {
    "fact",
    "relationship",
    "preference",
    "commitment",
    "event",
    "style",
    "inference",
    "note",
}
MEMORY_STATUSES = {"candidate", "approved", "rejected", "superseded", "forgotten"}
SENSITIVITIES = {"private", "sensitive", "restricted"}
SOURCE_KINDS = {"email", "user", "obsidian", "calendar", "contact", "model", "other"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_key(value: str) -> str:
    return " ".join(value.split()).casefold()


def _memory_fingerprint(entity_ref: str, category: str, content: str) -> str:
    value = "\0".join([entity_ref, category, normalize_key(content)])
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class MemoryStore:
    """Private structured memory with evidence and explicit lifecycle states."""

    def __init__(self, path: Path):
        self.path = path

    def _connect(self, *, create: bool) -> sqlite3.Connection | None:
        if not create and not self.path.exists():
            return None
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=30)
        else:
            conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        if create:
            conn.execute("PRAGMA journal_mode=WAL")
            self._ensure_schema(conn)
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_entities (
              entity_ref TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              canonical_name TEXT NOT NULL,
              canonical_key TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              CHECK (kind IN ('person', 'organization', 'project'))
            );

            CREATE INDEX IF NOT EXISTS idx_memory_entities_name
            ON memory_entities(kind, canonical_key);

            CREATE TABLE IF NOT EXISTS memory_entity_aliases (
              entity_ref TEXT NOT NULL REFERENCES memory_entities(entity_ref) ON DELETE CASCADE,
              alias TEXT NOT NULL,
              normalized_alias TEXT NOT NULL,
              PRIMARY KEY (entity_ref, normalized_alias)
            );

            CREATE INDEX IF NOT EXISTS idx_memory_aliases_normalized
            ON memory_entity_aliases(normalized_alias);

            CREATE TABLE IF NOT EXISTS memory_entity_emails (
              entity_ref TEXT NOT NULL REFERENCES memory_entities(entity_ref) ON DELETE CASCADE,
              email TEXT NOT NULL,
              normalized_email TEXT NOT NULL UNIQUE,
              PRIMARY KEY (entity_ref, normalized_email)
            );

            CREATE TABLE IF NOT EXISTS memories (
              memory_ref TEXT PRIMARY KEY,
              entity_ref TEXT NOT NULL REFERENCES memory_entities(entity_ref),
              category TEXT NOT NULL,
              content TEXT NOT NULL,
              status TEXT NOT NULL,
              confidence REAL NOT NULL,
              sensitivity TEXT NOT NULL,
              occurred_at TEXT NOT NULL DEFAULT '',
              valid_from TEXT NOT NULL DEFAULT '',
              valid_until TEXT NOT NULL DEFAULT '',
              supersedes_ref TEXT REFERENCES memories(memory_ref),
              fingerprint TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              CHECK (status IN ('candidate', 'approved', 'rejected', 'superseded', 'forgotten'))
            );

            CREATE INDEX IF NOT EXISTS idx_memories_entity_status
            ON memories(entity_ref, status, updated_at);

            CREATE TABLE IF NOT EXISTS memory_sources (
              memory_ref TEXT NOT NULL REFERENCES memories(memory_ref) ON DELETE CASCADE,
              source_ref TEXT NOT NULL,
              source_kind TEXT NOT NULL,
              excerpt TEXT NOT NULL DEFAULT '',
              source_sha256 TEXT NOT NULL DEFAULT '',
              PRIMARY KEY (memory_ref, source_ref)
            );
            """
        )
        conn.commit()

    @staticmethod
    def _entity_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        aliases = [
            str(item["alias"])
            for item in conn.execute(
                "SELECT alias FROM memory_entity_aliases WHERE entity_ref=? ORDER BY alias",
                (row["entity_ref"],),
            ).fetchall()
        ]
        emails = [
            str(item["email"])
            for item in conn.execute(
                "SELECT email FROM memory_entity_emails WHERE entity_ref=? ORDER BY email",
                (row["entity_ref"],),
            ).fetchall()
        ]
        return {
            "entity_ref": str(row["entity_ref"]),
            "kind": str(row["kind"]),
            "canonical_name": str(row["canonical_name"]),
            "aliases": aliases,
            "emails": emails,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def upsert_entity(
        self,
        *,
        kind: str,
        canonical_name: str,
        aliases: Iterable[str] = (),
        emails: Iterable[str] = (),
    ) -> dict[str, Any]:
        kind = kind.strip().lower()
        canonical_name = " ".join(canonical_name.split())
        if kind not in ENTITY_KINDS:
            raise ValueError(f"unsupported entity kind: {kind}")
        if not canonical_name:
            raise ValueError("canonical_name is required")

        alias_values = {
            " ".join(value.split())
            for value in aliases
            if normalize_key(value)
        }
        email_values = {
            value.strip()
            for value in emails
            if value.strip()
        }
        email_keys = {value.casefold(): value for value in email_values}
        conn = self._connect(create=True)
        assert conn is not None
        try:
            matches: set[str] = set()
            if email_keys:
                placeholders = ",".join("?" for _ in email_keys)
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT entity_ref FROM memory_entity_emails
                    WHERE normalized_email IN ({placeholders})
                    """,
                    list(email_keys),
                ).fetchall()
                matches.update(str(row["entity_ref"]) for row in rows)
            name_row = conn.execute(
                """
                SELECT entity_ref FROM memory_entities
                WHERE kind=? AND canonical_key=?
                ORDER BY created_at LIMIT 1
                """,
                (kind, normalize_key(canonical_name)),
            ).fetchone()
            if name_row:
                matches.add(str(name_row["entity_ref"]))
            if len(matches) > 1:
                raise ValueError("identity conflict: supplied name or emails match multiple entities")

            now = utc_now()
            if matches:
                entity_ref = matches.pop()
                previous = conn.execute(
                    "SELECT canonical_name FROM memory_entities WHERE entity_ref=?",
                    (entity_ref,),
                ).fetchone()
                if previous and normalize_key(str(previous["canonical_name"])) != normalize_key(
                    canonical_name
                ):
                    alias_values.add(str(previous["canonical_name"]))
                conn.execute(
                    """
                    UPDATE memory_entities
                    SET kind=?, canonical_name=?, canonical_key=?, updated_at=?
                    WHERE entity_ref=?
                    """,
                    (kind, canonical_name, normalize_key(canonical_name), now, entity_ref),
                )
            else:
                entity_ref = f"mail-memory://entity/{uuid.uuid4()}"
                conn.execute(
                    """
                    INSERT INTO memory_entities (
                      entity_ref, kind, canonical_name, canonical_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entity_ref,
                        kind,
                        canonical_name,
                        normalize_key(canonical_name),
                        now,
                        now,
                    ),
                )

            alias_values.add(canonical_name)
            for alias in sorted(alias_values):
                conn.execute(
                    """
                    INSERT INTO memory_entity_aliases (entity_ref, alias, normalized_alias)
                    VALUES (?, ?, ?)
                    ON CONFLICT(entity_ref, normalized_alias) DO UPDATE SET alias=excluded.alias
                    """,
                    (entity_ref, alias, normalize_key(alias)),
                )
            for normalized_email, email in sorted(email_keys.items()):
                owner = conn.execute(
                    """
                    SELECT entity_ref FROM memory_entity_emails WHERE normalized_email=?
                    """,
                    (normalized_email,),
                ).fetchone()
                if owner and str(owner["entity_ref"]) != entity_ref:
                    raise ValueError(f"email already belongs to another entity: {email}")
                conn.execute(
                    """
                    INSERT INTO memory_entity_emails (entity_ref, email, normalized_email)
                    VALUES (?, ?, ?)
                    ON CONFLICT(normalized_email) DO UPDATE SET email=excluded.email
                    """,
                    (entity_ref, email, normalized_email),
                )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM memory_entities WHERE entity_ref=?", (entity_ref,)
            ).fetchone()
            assert row is not None
            return self._entity_payload(conn, row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_entities(self, *, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        conn = self._connect(create=False)
        if conn is None:
            return []
        try:
            try:
                rows = conn.execute(
                    "SELECT * FROM memory_entities ORDER BY canonical_name LIMIT ?",
                    (max(1, min(limit, 500)),),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            key = normalize_key(query)
            payloads = [self._entity_payload(conn, row) for row in rows]
            if not key:
                return payloads
            return [
                item
                for item in payloads
                if key in normalize_key(item["canonical_name"])
                or any(key in normalize_key(alias) for alias in item["aliases"])
                or any(key in email.casefold() for email in item["emails"])
            ][:limit]
        finally:
            conn.close()

    def resolve_entity(self, selector: str) -> dict[str, Any]:
        selector = selector.strip()
        if not selector:
            raise ValueError("entity selector is required")
        conn = self._connect(create=False)
        if conn is None:
            raise KeyError(f"entity not found: {selector}")
        try:
            try:
                if selector.startswith("mail-memory://entity/"):
                    row = conn.execute(
                        "SELECT * FROM memory_entities WHERE entity_ref=?", (selector,)
                    ).fetchone()
                    if row:
                        return self._entity_payload(conn, row)
                    raise KeyError(f"entity not found: {selector}")

                key = normalize_key(selector)
                rows = conn.execute(
                    """
                    SELECT DISTINCT e.*
                    FROM memory_entities e
                    LEFT JOIN memory_entity_aliases a ON a.entity_ref=e.entity_ref
                    LEFT JOIN memory_entity_emails m ON m.entity_ref=e.entity_ref
                    WHERE e.canonical_key=? OR a.normalized_alias=? OR m.normalized_email=?
                    ORDER BY e.created_at
                    """,
                    (key, key, selector.casefold()),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                raise KeyError(f"entity not found: {selector}") from exc
            if not rows:
                raise KeyError(f"entity not found: {selector}")
            if len(rows) > 1:
                raise ValueError(f"ambiguous entity selector: {selector}")
            return self._entity_payload(conn, rows[0])
        finally:
            conn.close()

    @staticmethod
    def _memory_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        sources = [
            {
                "source_ref": str(item["source_ref"]),
                "source_kind": str(item["source_kind"]),
                "excerpt": str(item["excerpt"]),
                "source_sha256": str(item["source_sha256"]),
            }
            for item in conn.execute(
                """
                SELECT source_ref, source_kind, excerpt, source_sha256
                FROM memory_sources WHERE memory_ref=? ORDER BY source_ref
                """,
                (row["memory_ref"],),
            ).fetchall()
        ]
        return {
            "memory_ref": str(row["memory_ref"]),
            "entity_ref": str(row["entity_ref"]),
            "entity_name": str(row["entity_name"]),
            "entity_kind": str(row["entity_kind"]),
            "category": str(row["category"]),
            "content": str(row["content"]),
            "status": str(row["status"]),
            "confidence": float(row["confidence"]),
            "sensitivity": str(row["sensitivity"]),
            "occurred_at": str(row["occurred_at"]),
            "valid_from": str(row["valid_from"]),
            "valid_until": str(row["valid_until"]),
            "supersedes_ref": str(row["supersedes_ref"] or ""),
            "sources": sources,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _memory_select() -> str:
        return """
            SELECT m.*, e.canonical_name AS entity_name, e.kind AS entity_kind
            FROM memories m
            JOIN memory_entities e ON e.entity_ref=m.entity_ref
        """

    def propose_memory(
        self,
        *,
        entity_ref: str,
        category: str,
        content: str,
        sources: Iterable[dict[str, str]],
        confidence: float = 1.0,
        sensitivity: str = "private",
        occurred_at: str = "",
        valid_from: str = "",
        valid_until: str = "",
        supersedes_ref: str = "",
    ) -> dict[str, Any]:
        category = category.strip().lower()
        content = " ".join(content.split())
        sensitivity = sensitivity.strip().lower()
        source_items = list(sources)
        if category not in MEMORY_CATEGORIES:
            raise ValueError(f"unsupported memory category: {category}")
        if not content:
            raise ValueError("memory content is required")
        if not 0 <= float(confidence) <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if sensitivity not in SENSITIVITIES:
            raise ValueError(f"unsupported sensitivity: {sensitivity}")
        if not source_items:
            raise ValueError("at least one evidence source is required")
        for source in source_items:
            if not str(source.get("source_ref") or "").strip():
                raise ValueError("source_ref is required")
            if str(source.get("source_kind") or "") not in SOURCE_KINDS:
                raise ValueError(
                    f"unsupported source kind: {source.get('source_kind') or ''}"
                )

        conn = self._connect(create=True)
        assert conn is not None
        try:
            entity = conn.execute(
                "SELECT entity_ref FROM memory_entities WHERE entity_ref=?", (entity_ref,)
            ).fetchone()
            if not entity:
                raise KeyError(f"entity not found: {entity_ref}")
            if supersedes_ref:
                old = conn.execute(
                    "SELECT entity_ref FROM memories WHERE memory_ref=?", (supersedes_ref,)
                ).fetchone()
                if not old:
                    raise KeyError(f"superseded memory not found: {supersedes_ref}")
                if str(old["entity_ref"]) != entity_ref:
                    raise ValueError("a memory can only supersede memory for the same entity")

            fingerprint = _memory_fingerprint(entity_ref, category, content)
            existing = conn.execute(
                "SELECT memory_ref FROM memories WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            now = utc_now()
            if existing:
                memory_ref = str(existing["memory_ref"])
                conn.execute(
                    """
                    UPDATE memories SET confidence=MAX(confidence, ?), updated_at=?
                    WHERE memory_ref=?
                    """,
                    (float(confidence), now, memory_ref),
                )
            else:
                memory_ref = f"mail-memory://fact/{uuid.uuid4()}"
                conn.execute(
                    """
                    INSERT INTO memories (
                      memory_ref, entity_ref, category, content, status, confidence,
                      sensitivity, occurred_at, valid_from, valid_until, supersedes_ref,
                      fingerprint, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'candidate', ?, ?, ?, ?, ?, NULLIF(?, ''),
                              ?, ?, ?)
                    """,
                    (
                        memory_ref,
                        entity_ref,
                        category,
                        content,
                        float(confidence),
                        sensitivity,
                        occurred_at,
                        valid_from,
                        valid_until,
                        supersedes_ref,
                        fingerprint,
                        now,
                        now,
                    ),
                )

            for source in source_items:
                conn.execute(
                    """
                    INSERT INTO memory_sources (
                      memory_ref, source_ref, source_kind, excerpt, source_sha256
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(memory_ref, source_ref) DO UPDATE SET
                      source_kind=excluded.source_kind,
                      excerpt=CASE
                        WHEN excluded.excerpt='' THEN memory_sources.excerpt
                        ELSE excluded.excerpt
                      END,
                      source_sha256=CASE
                        WHEN excluded.source_sha256='' THEN memory_sources.source_sha256
                        ELSE excluded.source_sha256
                      END
                    """,
                    (
                        memory_ref,
                        str(source["source_ref"]).strip(),
                        str(source["source_kind"]),
                        str(source.get("excerpt") or "").strip(),
                        str(source.get("source_sha256") or "").strip(),
                    ),
                )
            conn.commit()
            row = conn.execute(
                self._memory_select() + " WHERE m.memory_ref=?", (memory_ref,)
            ).fetchone()
            assert row is not None
            return self._memory_payload(conn, row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_memory(self, memory_ref: str) -> dict[str, Any] | None:
        conn = self._connect(create=False)
        if conn is None:
            return None
        try:
            try:
                row = conn.execute(
                    self._memory_select() + " WHERE m.memory_ref=?", (memory_ref,)
                ).fetchone()
            except sqlite3.OperationalError:
                return None
            return self._memory_payload(conn, row) if row else None
        finally:
            conn.close()

    def list_memories(
        self,
        *,
        entity: str = "",
        statuses: Iterable[str] = ("approved",),
        query: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        conn = self._connect(create=False)
        if conn is None:
            return []
        try:
            status_values = tuple(dict.fromkeys(value.strip().lower() for value in statuses))
            if not status_values or any(value not in MEMORY_STATUSES for value in status_values):
                raise ValueError("invalid memory status filter")
            where = [f"m.status IN ({','.join('?' for _ in status_values)})"]
            params: list[Any] = list(status_values)
            if entity:
                resolved = self.resolve_entity(entity)
                where.append("m.entity_ref=?")
                params.append(resolved["entity_ref"])
            if query:
                where.append("(lower(m.content) LIKE ? OR lower(e.canonical_name) LIKE ?)")
                like = f"%{query.lower()}%"
                params.extend([like, like])
            params.append(max(1, min(int(limit), 500)))
            try:
                rows = conn.execute(
                    self._memory_select()
                    + f" WHERE {' AND '.join(where)}"
                    + " ORDER BY m.updated_at DESC, m.created_at DESC LIMIT ?",
                    params,
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            return [self._memory_payload(conn, row) for row in rows]
        finally:
            conn.close()

    def _transition(self, memory_ref: str, target: str) -> dict[str, Any]:
        allowed_from = {
            "approved": {"candidate", "approved"},
            "rejected": {"candidate", "rejected"},
            "forgotten": MEMORY_STATUSES,
        }
        if target not in allowed_from:
            raise ValueError(f"unsupported transition target: {target}")
        conn = self._connect(create=True)
        assert conn is not None
        try:
            row = conn.execute(
                "SELECT status, supersedes_ref FROM memories WHERE memory_ref=?",
                (memory_ref,),
            ).fetchone()
            if not row:
                raise KeyError(f"memory not found: {memory_ref}")
            current = str(row["status"])
            if current not in allowed_from[target]:
                raise ValueError(f"cannot transition memory from {current} to {target}")
            now = utc_now()
            conn.execute(
                "UPDATE memories SET status=?, updated_at=? WHERE memory_ref=?",
                (target, now, memory_ref),
            )
            if target == "approved" and row["supersedes_ref"]:
                conn.execute(
                    """
                    UPDATE memories SET status='superseded', updated_at=?
                    WHERE memory_ref=? AND status='approved'
                    """,
                    (now, row["supersedes_ref"]),
                )
            conn.commit()
            result = conn.execute(
                self._memory_select() + " WHERE m.memory_ref=?", (memory_ref,)
            ).fetchone()
            assert result is not None
            return self._memory_payload(conn, result)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def approve(self, memory_ref: str) -> dict[str, Any]:
        return self._transition(memory_ref, "approved")

    def reject(self, memory_ref: str) -> dict[str, Any]:
        return self._transition(memory_ref, "rejected")

    def forget(self, memory_ref: str) -> dict[str, Any]:
        return self._transition(memory_ref, "forgotten")
