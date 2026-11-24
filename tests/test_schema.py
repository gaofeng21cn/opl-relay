from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from codex_mail_workbench.schema import (
    DRAFTS_SCHEMA_VERSION,
    MAIL_SCHEMA_VERSION,
    MEMORY_SCHEMA_VERSION,
    ensure_drafts_schema,
    ensure_mail_schema,
    ensure_memory_schema,
    get_user_version,
)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def test_fresh_databases_advance_user_version_and_are_idempotent(tmp_path: Path) -> None:
    mail_conn = sqlite3.connect(tmp_path / "mail.sqlite")
    try:
        assert get_user_version(mail_conn) == 0
        assert ensure_mail_schema(mail_conn) == MAIL_SCHEMA_VERSION
        assert ensure_mail_schema(mail_conn) == MAIL_SCHEMA_VERSION
        assert {
            "email_messages",
            "email_blobs",
            "mailbox_operations",
            "folder_snapshots",
            "mail_folders",
            "mail_reviews",
            "email_search",
            "email_search_fts",
            "graph_message_ids",
            "graph_folder_members",
            "graph_folder_cursors",
        } <= _tables(mail_conn)
    finally:
        mail_conn.close()

    mem_conn = sqlite3.connect(tmp_path / "memory.sqlite")
    try:
        assert get_user_version(mem_conn) == 0
        assert ensure_memory_schema(mem_conn) == MEMORY_SCHEMA_VERSION
        assert ensure_memory_schema(mem_conn) == MEMORY_SCHEMA_VERSION
        assert {
            "memory_entities",
            "memory_entity_aliases",
            "memory_entity_emails",
            "memories",
            "memory_semantics",
            "memory_semantic_events",
            "memory_sources",
            "knowledge_catalog",
        } <= _tables(mem_conn)
    finally:
        mem_conn.close()

    draft_conn = sqlite3.connect(tmp_path / "drafts.sqlite")
    try:
        assert get_user_version(draft_conn) == 0
        assert ensure_drafts_schema(draft_conn) == DRAFTS_SCHEMA_VERSION
        assert ensure_drafts_schema(draft_conn) == DRAFTS_SCHEMA_VERSION
        assert {
            "mail_drafts",
            "mail_draft_events",
            "server_draft_requests",
            "server_draft_handoffs",
            "server_draft_smtp_deliveries",
            "graph_server_drafts",
            "workbench_edits",
            "workbench_attachments",
        } <= _tables(draft_conn)
    finally:
        draft_conn.close()


def test_legacy_unversioned_mail_and_memory_databases_migrate_cleanly(tmp_path: Path) -> None:
    raw = b"Subject: Legacy\r\n\r\nPreserved body."
    digest = hashlib.sha256(raw).hexdigest()

    mail_conn = sqlite3.connect(tmp_path / "legacy-mail.sqlite")
    try:
        mail_conn.executescript(
            """
            CREATE TABLE email_messages (
              account_id TEXT NOT NULL, folder TEXT NOT NULL, folder_slug TEXT NOT NULL,
              uid INTEGER NOT NULL, uidvalidity INTEGER,
              message_id TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '',
              sender TEXT NOT NULL DEFAULT '', recipient TEXT NOT NULL DEFAULT '',
              date_iso TEXT NOT NULL DEFAULT '', raw_sha256 TEXT NOT NULL,
              raw_eml BLOB NOT NULL, attachments_json TEXT NOT NULL DEFAULT '[]',
              ingest_ts TEXT NOT NULL, storage_ref TEXT NOT NULL UNIQUE,
              deleted INTEGER NOT NULL DEFAULT 0, deleted_ts TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE mail_reviews (
              account_id TEXT NOT NULL, raw_sha256 TEXT NOT NULL,
              storage_ref TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL,
              note TEXT NOT NULL DEFAULT '', reviewed_at TEXT NOT NULL,
              PRIMARY KEY(account_id, raw_sha256)
            );
            """
        )
        mail_conn.execute(
            """
            INSERT INTO email_messages
              (account_id, folder, folder_slug, uid, uidvalidity, subject, raw_sha256, raw_eml,
               ingest_ts, storage_ref, deleted)
            VALUES ('work', 'INBOX', 'INBOX', 1, 1, 'Legacy', ?, ?, '2026-01-01T00:00:00Z',
                    'email-store://work/INBOX/1/legacy', 0)
            """,
            (digest, raw),
        )
        mail_conn.commit()
        assert get_user_version(mail_conn) == 0

        assert ensure_mail_schema(mail_conn) == MAIL_SCHEMA_VERSION
        msg_cols = {r[1] for r in mail_conn.execute("PRAGMA table_info(email_messages)")}
        review_cols = {r[1] for r in mail_conn.execute("PRAGMA table_info(mail_reviews)")}
        assert "present" in msg_cols
        assert "raw_eml" not in msg_cols
        assert {"category", "suggested_folder"} <= review_cols
        assert mail_conn.execute("SELECT raw_eml FROM email_blobs WHERE raw_sha256=?", (digest,)).fetchone()[0] == raw
        assert mail_conn.execute("SELECT present FROM email_messages").fetchone()[0] == 1
    finally:
        mail_conn.close()

    mem_conn = sqlite3.connect(tmp_path / "legacy-memory.sqlite")
    try:
        mem_conn.executescript(
            """
            CREATE TABLE memories (
              memory_ref TEXT PRIMARY KEY,
              entity_ref TEXT NOT NULL,
              status TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE memory_semantics (
              memory_ref TEXT PRIMARY KEY,
              observed_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE knowledge_documents (doc_id TEXT PRIMARY KEY, body TEXT);
            """
        )
        mem_conn.commit()
        assert get_user_version(mem_conn) == 0
        assert ensure_memory_schema(mem_conn) == MEMORY_SCHEMA_VERSION
        cols = {r[1] for r in mem_conn.execute("PRAGMA table_info(memory_semantics)")}
        assert "promotion_target_id" in cols
        assert "knowledge_documents" not in _tables(mem_conn)
    finally:
        mem_conn.close()
