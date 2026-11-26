"""Centralized, ordered SQLite schema migrations using PRAGMA user_version.

Relay owns three SQLite stores under ``<profile>/data/relay``:
- ``mail.sqlite``: raw email evidence, FTS5 trigram body search, folder/review state, and Graph sync cursors.
- ``memory.sqlite``: relationship memory graph and rebuildable Framework knowledge metadata catalog.
- ``drafts.sqlite``: Apple Mail drafts, server review drafts, delivery ledgers, and workbench edits.

Every migration step is idempotent on both fresh databases and pre-versioned legacy databases.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


def get_user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def run_migrations(
    conn: sqlite3.Connection,
    migrations: Sequence[Migration],
    *,
    effective_version: int | None = None,
) -> int:
    """Apply pending migrations in order and advance PRAGMA user_version."""
    current = get_user_version(conn)
    start_version = current if effective_version is None else min(current, effective_version)
    target = migrations[-1].version if migrations else 0
    if start_version >= target:
        return current
    for migration in migrations:
        if migration.version <= start_version:
            continue
        migration.apply(conn)
        conn.execute(f"PRAGMA user_version = {int(migration.version)}")
        conn.commit()
    return get_user_version(conn)


# ---------------------------------------------------------------------------
# mail.sqlite migrations
# ---------------------------------------------------------------------------

def _mail_v1_core_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS email_messages (
          account_id TEXT NOT NULL,
          folder TEXT NOT NULL,
          folder_slug TEXT NOT NULL,
          uid INTEGER NOT NULL,
          uidvalidity INTEGER,
          message_id TEXT NOT NULL DEFAULT '',
          subject TEXT NOT NULL DEFAULT '',
          sender TEXT NOT NULL DEFAULT '',
          recipient TEXT NOT NULL DEFAULT '',
          date_iso TEXT NOT NULL DEFAULT '',
          raw_sha256 TEXT NOT NULL,
          attachments_json TEXT NOT NULL DEFAULT '[]',
          ingest_ts TEXT NOT NULL,
          storage_ref TEXT PRIMARY KEY,
          deleted INTEGER NOT NULL DEFAULT 0,
          deleted_ts TEXT NOT NULL DEFAULT '',
          present INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS email_blobs (
          raw_sha256 TEXT PRIMARY KEY,
          raw_eml BLOB NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mailbox_operations (
          operation_ref TEXT PRIMARY KEY,
          account_id TEXT NOT NULL,
          storage_ref TEXT NOT NULL,
          source_folder TEXT NOT NULL,
          source_uid INTEGER NOT NULL,
          destination_folder TEXT NOT NULL,
          operation TEXT NOT NULL,
          method TEXT NOT NULL,
          raw_sha256 TEXT NOT NULL,
          occurred_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS folder_snapshots (
          account_id TEXT NOT NULL,
          folder TEXT NOT NULL,
          folder_slug TEXT NOT NULL,
          uidvalidity INTEGER NOT NULL,
          remote_count INTEGER NOT NULL,
          checked_at TEXT NOT NULL,
          complete INTEGER NOT NULL,
          PRIMARY KEY(account_id, folder_slug)
        );

        CREATE TABLE IF NOT EXISTS mail_folders (
          account_id TEXT NOT NULL,
          folder TEXT NOT NULL,
          role TEXT NOT NULL,
          PRIMARY KEY(account_id, folder)
        );

        CREATE TABLE IF NOT EXISTS mail_reviews (
          account_id TEXT NOT NULL,
          raw_sha256 TEXT NOT NULL,
          storage_ref TEXT NOT NULL,
          action TEXT NOT NULL,
          status TEXT NOT NULL,
          note TEXT NOT NULL DEFAULT '',
          reviewed_at TEXT NOT NULL,
          category TEXT NOT NULL DEFAULT '',
          suggested_folder TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(account_id, raw_sha256)
        );

        CREATE TABLE IF NOT EXISTS email_search (
          raw_sha256 TEXT PRIMARY KEY,
          body_text TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS email_search_fts USING fts5(
          body_text, content='email_search', content_rowid='rowid', tokenize='trigram'
        );

        CREATE TRIGGER IF NOT EXISTS email_search_insert AFTER INSERT ON email_search BEGIN
          INSERT INTO email_search_fts(rowid, body_text) VALUES(new.rowid, new.body_text);
        END;

        CREATE TABLE IF NOT EXISTS graph_message_ids (
          uid INTEGER PRIMARY KEY AUTOINCREMENT,
          account_id TEXT NOT NULL,
          remote_id TEXT NOT NULL,
          UNIQUE(account_id, remote_id)
        );

        CREATE TABLE IF NOT EXISTS graph_folder_members (
          account_id TEXT NOT NULL,
          folder_id TEXT NOT NULL,
          remote_id TEXT NOT NULL,
          change_key TEXT NOT NULL,
          generation INTEGER NOT NULL,
          PRIMARY KEY(account_id, folder_id, remote_id)
        );

        CREATE TABLE IF NOT EXISTS graph_folder_cursors (
          account_id TEXT NOT NULL,
          folder_id TEXT NOT NULL,
          link TEXT NOT NULL DEFAULT '',
          generation INTEGER NOT NULL DEFAULT 1,
          baseline INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY(account_id, folder_id)
        );
        """
    )


def _mail_v2_legacy_columns(conn: sqlite3.Connection) -> None:
    msg_columns = _table_columns(conn, "email_messages")
    if msg_columns and "present" not in msg_columns:
        # Preserve every storage_ref and raw byte when replacing the old UID key.
        # A reused UID in a new UIDVALIDITY must not overwrite historical evidence.
        has_raw_eml = "raw_eml" in msg_columns
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ALTER TABLE email_messages RENAME TO email_messages_legacy")
            if has_raw_eml:
                conn.execute(
                    """
                    CREATE TABLE email_messages (
                      account_id TEXT NOT NULL, folder TEXT NOT NULL, folder_slug TEXT NOT NULL,
                      uid INTEGER NOT NULL, uidvalidity INTEGER,
                      message_id TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '',
                      sender TEXT NOT NULL DEFAULT '', recipient TEXT NOT NULL DEFAULT '',
                      date_iso TEXT NOT NULL DEFAULT '', raw_sha256 TEXT NOT NULL,
                      raw_eml BLOB NOT NULL, attachments_json TEXT NOT NULL DEFAULT '[]',
                      ingest_ts TEXT NOT NULL, storage_ref TEXT PRIMARY KEY,
                      deleted INTEGER NOT NULL DEFAULT 0, deleted_ts TEXT NOT NULL DEFAULT '',
                      present INTEGER NOT NULL DEFAULT 1
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO email_messages SELECT
                      account_id, folder, folder_slug, uid, uidvalidity, message_id, subject,
                      sender, recipient, date_iso, raw_sha256, raw_eml, attachments_json,
                      ingest_ts, storage_ref, deleted, deleted_ts,
                      CASE WHEN deleted=0 THEN 1 ELSE 0 END
                    FROM email_messages_legacy
                    """
                )
            else:
                conn.execute(
                    """
                    CREATE TABLE email_messages (
                      account_id TEXT NOT NULL, folder TEXT NOT NULL, folder_slug TEXT NOT NULL,
                      uid INTEGER NOT NULL, uidvalidity INTEGER,
                      message_id TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '',
                      sender TEXT NOT NULL DEFAULT '', recipient TEXT NOT NULL DEFAULT '',
                      date_iso TEXT NOT NULL DEFAULT '', raw_sha256 TEXT NOT NULL,
                      attachments_json TEXT NOT NULL DEFAULT '[]',
                      ingest_ts TEXT NOT NULL, storage_ref TEXT PRIMARY KEY,
                      deleted INTEGER NOT NULL DEFAULT 0, deleted_ts TEXT NOT NULL DEFAULT '',
                      present INTEGER NOT NULL DEFAULT 1
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO email_messages SELECT
                      account_id, folder, folder_slug, uid, uidvalidity, message_id, subject,
                      sender, recipient, date_iso, raw_sha256, attachments_json,
                      ingest_ts, storage_ref, deleted, deleted_ts,
                      CASE WHEN deleted=0 THEN 1 ELSE 0 END
                    FROM email_messages_legacy
                    """
                )
            conn.execute("DROP TABLE email_messages_legacy")

    review_columns = _table_columns(conn, "mail_reviews")
    for column in ("category", "suggested_folder"):
        if review_columns and column not in review_columns:
            conn.execute(f"ALTER TABLE mail_reviews ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")


def _mail_v3_dedup_raw_eml(conn: sqlite3.Connection) -> None:
    if "raw_eml" in _table_columns(conn, "email_messages"):
        # Content is immutable and shared by all folder identities. Keep the
        # metadata table and every stable reference; remove only duplicate bytes.
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO email_blobs SELECT raw_sha256, raw_eml FROM email_messages"
            )
            mismatch = conn.execute(
                """
                SELECT 1 FROM email_messages m JOIN email_blobs b USING(raw_sha256)
                WHERE m.raw_eml<>b.raw_eml LIMIT 1
                """
            ).fetchone()
            if mismatch:
                raise ValueError(
                    "raw hash collision or corrupt evidence; refusing content deduplication"
                )
            conn.execute("ALTER TABLE email_messages DROP COLUMN raw_eml")


def _mail_v4_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_email_messages_folder_live
          ON email_messages(account_id, folder_slug, present, deleted);
        CREATE INDEX IF NOT EXISTS idx_email_messages_msgid
          ON email_messages(account_id, message_id);
        CREATE INDEX IF NOT EXISTS idx_email_messages_date_live
          ON email_messages(date_iso, deleted);
        CREATE INDEX IF NOT EXISTS idx_email_messages_uid
          ON email_messages(account_id, folder_slug, uidvalidity, uid);
        CREATE INDEX IF NOT EXISTS idx_email_messages_hash
          ON email_messages(raw_sha256);
        CREATE INDEX IF NOT EXISTS idx_email_messages_present
          ON email_messages(account_id, folder_slug, present, deleted);
        CREATE INDEX IF NOT EXISTS idx_mailbox_operations_storage_ref
          ON mailbox_operations(storage_ref);
        """
    )


MAIL_MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "core email store, FTS5 search, and Graph sync tables", _mail_v1_core_tables),
    Migration(2, "legacy email_messages present column and mail_reviews columns", _mail_v2_legacy_columns),
    Migration(3, "deduplicate legacy email_messages.raw_eml into email_blobs", _mail_v3_dedup_raw_eml),
    Migration(4, "ensure email store and operation indexes", _mail_v4_indexes),
)
MAIL_SCHEMA_VERSION = MAIL_MIGRATIONS[-1].version


def ensure_mail_schema(conn: sqlite3.Connection) -> int:
    effective = get_user_version(conn)
    if effective >= MAIL_SCHEMA_VERSION:
        msg_cols = _table_columns(conn, "email_messages")
        if not msg_cols:
            effective = 0
        elif "present" not in msg_cols:
            effective = 1
        elif "raw_eml" in msg_cols:
            effective = 2
        elif not _table_exists(conn, "graph_message_ids"):
            effective = 0
        else:
            review_cols = _table_columns(conn, "mail_reviews")
            if review_cols and not {"category", "suggested_folder"} <= review_cols:
                effective = 1
    return run_migrations(conn, MAIL_MIGRATIONS, effective_version=effective)


# ---------------------------------------------------------------------------
# memory.sqlite migrations
# ---------------------------------------------------------------------------

def _memory_v1_core_tables(conn: sqlite3.Connection) -> None:
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

        CREATE TABLE IF NOT EXISTS memory_semantics (
          memory_ref TEXT PRIMARY KEY REFERENCES memories(memory_ref),
          observed_at TEXT NOT NULL DEFAULT '',
          next_review_at TEXT NOT NULL DEFAULT '',
          content_kind TEXT NOT NULL DEFAULT 'state',
          scope_json TEXT NOT NULL DEFAULT '{}',
          knowledge_id TEXT NOT NULL DEFAULT '',
          source_versions_json TEXT NOT NULL DEFAULT '{}',
          canonical_target_json TEXT NOT NULL DEFAULT '{}',
          writeback_status TEXT NOT NULL DEFAULT 'candidate',
          proposal_ref TEXT NOT NULL DEFAULT '',
          promotion_target_id TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS memory_semantic_events (
          event_id INTEGER PRIMARY KEY AUTOINCREMENT,
          memory_ref TEXT NOT NULL REFERENCES memories(memory_ref),
          event_kind TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          recorded_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS memory_sources (
          memory_ref TEXT NOT NULL REFERENCES memories(memory_ref) ON DELETE CASCADE,
          source_ref TEXT NOT NULL,
          source_kind TEXT NOT NULL,
          excerpt TEXT NOT NULL DEFAULT '',
          source_sha256 TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (memory_ref, source_ref)
        );

        CREATE TABLE IF NOT EXISTS knowledge_catalog (
          knowledge_id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL,
          metadata_json TEXT NOT NULL,
          indexed_at TEXT NOT NULL
        );
        """
    )


def _memory_v2_legacy_semantics_and_cleanup(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "memory_semantics")
    if columns and "promotion_target_id" not in columns:
        conn.execute(
            "ALTER TABLE memory_semantics ADD COLUMN promotion_target_id TEXT NOT NULL DEFAULT ''"
        )
    # Remove retired source-body cache table while preserving memories and evidence.
    conn.execute("DROP TABLE IF EXISTS knowledge_documents")


MEMORY_MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "core memory graph and knowledge catalog tables", _memory_v1_core_tables),
    Migration(2, "add promotion_target_id and drop retired knowledge_documents", _memory_v2_legacy_semantics_and_cleanup),
)
MEMORY_SCHEMA_VERSION = MEMORY_MIGRATIONS[-1].version


def ensure_memory_schema(conn: sqlite3.Connection) -> int:
    effective = get_user_version(conn)
    if effective >= MEMORY_SCHEMA_VERSION:
        required = (
            "memory_entities",
            "memories",
            "memory_semantics",
            "memory_semantic_events",
            "memory_sources",
            "knowledge_catalog",
        )
        if any(not _table_exists(conn, table) for table in required):
            effective = 0
        elif "promotion_target_id" not in _table_columns(conn, "memory_semantics"):
            effective = 1
    return run_migrations(conn, MEMORY_MIGRATIONS, effective_version=effective)


# ---------------------------------------------------------------------------
# drafts.sqlite migrations
# ---------------------------------------------------------------------------

def _drafts_v1_core_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mail_drafts (
          draft_ref TEXT PRIMARY KEY,
          account_id TEXT NOT NULL,
          provider_account TEXT NOT NULL,
          provider_uuid TEXT NOT NULL,
          provider_message_id TEXT NOT NULL DEFAULT '',
          state TEXT NOT NULL CHECK(state IN ('draft', 'sending', 'sent', 'unknown')),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          last_fingerprint TEXT NOT NULL DEFAULT '',
          approval_fingerprint TEXT NOT NULL DEFAULT '',
          send_started_at TEXT NOT NULL DEFAULT '',
          sent_at TEXT NOT NULL DEFAULT '',
          sent_receipt_json TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS mail_draft_events (
          event_id INTEGER PRIMARY KEY AUTOINCREMENT,
          draft_ref TEXT NOT NULL,
          event_type TEXT NOT NULL,
          event_at TEXT NOT NULL,
          fingerprint TEXT NOT NULL DEFAULT '',
          detail_json TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS server_draft_requests (
          request_id TEXT PRIMARY KEY,
          account_id TEXT NOT NULL,
          input_hash TEXT NOT NULL,
          raw BLOB NOT NULL,
          state TEXT NOT NULL,
          folder TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS server_draft_handoffs (
          request_id TEXT PRIMARY KEY,
          draft_ref TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS server_draft_smtp_deliveries (
          request_id TEXT PRIMARY KEY,
          sent_folder TEXT NOT NULL,
          smtp_accepted INTEGER NOT NULL DEFAULT 0,
          sent_receipt TEXT
        );

        CREATE TABLE IF NOT EXISTS graph_server_drafts (
          request_id TEXT PRIMARY KEY,
          remote_id TEXT NOT NULL,
          verified_raw BLOB NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workbench_edits (
          id TEXT PRIMARY KEY,
          account_id TEXT NOT NULL,
          source_ref TEXT NOT NULL,
          body TEXT NOT NULL,
          attachment_ids TEXT NOT NULL,
          revision INTEGER NOT NULL,
          state TEXT NOT NULL,
          server_request_id TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workbench_attachments (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          sha256 TEXT NOT NULL,
          size INTEGER NOT NULL,
          relative_path TEXT NOT NULL
        );
        """
    )


DRAFTS_MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "drafts, server drafts, delivery handoffs, and workbench tables", _drafts_v1_core_tables),
)
DRAFTS_SCHEMA_VERSION = DRAFTS_MIGRATIONS[-1].version


def ensure_drafts_schema(conn: sqlite3.Connection) -> int:
    effective = get_user_version(conn)
    if effective >= DRAFTS_SCHEMA_VERSION:
        required = (
            "mail_drafts",
            "server_draft_requests",
            "server_draft_handoffs",
            "graph_server_drafts",
            "workbench_edits",
        )
        if any(not _table_exists(conn, table) for table in required):
            effective = 0
    return run_migrations(conn, DRAFTS_MIGRATIONS, effective_version=effective)
