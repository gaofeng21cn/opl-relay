from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .message import extract_text_body


SCOPES = ("active", "history", "all")
ACTIVE_FOLDERS = ("inbox", "sent", "sent items", "sent messages", "[gmail]/sent mail")
HISTORY_FOLDERS = ACTIVE_FOLDERS + ("archive", "archives", "[gmail]/all mail")
MESSAGE_COLUMNS = (
    "account_id, folder, folder_slug, uid, uidvalidity, message_id, subject, "
    "sender, recipient, date_iso, attachments_json, ingest_ts, storage_ref, raw_sha256"
)


def ensure_email_store_schema(conn: sqlite3.Connection) -> None:
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
          raw_eml BLOB NOT NULL,
          attachments_json TEXT NOT NULL DEFAULT '[]',
          ingest_ts TEXT NOT NULL,
          storage_ref TEXT NOT NULL UNIQUE,
          deleted INTEGER NOT NULL DEFAULT 0,
          deleted_ts TEXT NOT NULL DEFAULT '',
          present INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY (storage_ref)
        );

        CREATE INDEX IF NOT EXISTS idx_email_messages_folder_live
        ON email_messages(account_id, folder_slug, deleted);

        CREATE INDEX IF NOT EXISTS idx_email_messages_msgid
        ON email_messages(account_id, message_id);

        CREATE INDEX IF NOT EXISTS idx_email_messages_date_live
        ON email_messages(date_iso, deleted);

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

        CREATE INDEX IF NOT EXISTS idx_mailbox_operations_storage_ref
        ON mailbox_operations(storage_ref);
        """
    )
    columns = conn.execute("PRAGMA table_info(email_messages)").fetchall()
    if "present" not in {row[1] for row in columns}:
        # Preserve every storage_ref and raw byte when replacing the old UID key.
        # A reused UID in a new UIDVALIDITY must not overwrite historical evidence.
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ALTER TABLE email_messages RENAME TO email_messages_legacy")
            conn.execute("""
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
            """)
            conn.execute("""INSERT INTO email_messages SELECT
                account_id,folder,folder_slug,uid,uidvalidity,message_id,subject,sender,recipient,
                date_iso,raw_sha256,raw_eml,attachments_json,ingest_ts,storage_ref,deleted,deleted_ts,
                CASE WHEN deleted=0 THEN 1 ELSE 0 END FROM email_messages_legacy""")
            conn.execute("DROP TABLE email_messages_legacy")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS email_blobs (
          raw_sha256 TEXT PRIMARY KEY, raw_eml BLOB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_email_messages_folder_live
          ON email_messages(account_id, folder_slug, present, deleted);
        CREATE INDEX IF NOT EXISTS idx_email_messages_msgid ON email_messages(account_id, message_id);
        CREATE INDEX IF NOT EXISTS idx_email_messages_date_live ON email_messages(date_iso, deleted);
        CREATE INDEX IF NOT EXISTS idx_email_messages_uid ON email_messages(account_id, folder_slug, uidvalidity, uid);
        CREATE INDEX IF NOT EXISTS idx_email_messages_hash ON email_messages(raw_sha256);
        CREATE INDEX IF NOT EXISTS idx_email_messages_present ON email_messages(account_id, folder_slug, present, deleted);
        CREATE TABLE IF NOT EXISTS folder_snapshots (
          account_id TEXT NOT NULL, folder TEXT NOT NULL, folder_slug TEXT NOT NULL,
          uidvalidity INTEGER NOT NULL, remote_count INTEGER NOT NULL,
          checked_at TEXT NOT NULL, complete INTEGER NOT NULL,
          PRIMARY KEY(account_id, folder_slug)
        );
        CREATE TABLE IF NOT EXISTS mail_reviews (
          account_id TEXT NOT NULL, raw_sha256 TEXT NOT NULL,
          storage_ref TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL,
          note TEXT NOT NULL DEFAULT '', reviewed_at TEXT NOT NULL,
          PRIMARY KEY(account_id, raw_sha256)
        );
        CREATE TABLE IF NOT EXISTS email_search (
          raw_sha256 TEXT PRIMARY KEY, body_text TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS email_search_fts USING fts5(
          body_text, content='email_search', content_rowid='rowid', tokenize='trigram'
        );
        CREATE TRIGGER IF NOT EXISTS email_search_insert AFTER INSERT ON email_search BEGIN
          INSERT INTO email_search_fts(rowid, body_text) VALUES(new.rowid, new.body_text);
        END;
    """)
    review_columns = {r[1] for r in conn.execute("PRAGMA table_info(mail_reviews)")}
    for column in ("category", "suggested_folder"):
        if column not in review_columns:
            conn.execute(f"ALTER TABLE mail_reviews ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    conn.commit()
    if "raw_eml" in {r[1] for r in conn.execute("PRAGMA table_info(email_messages)")}:
        # Content is immutable and shared by all folder identities. Keep the
        # metadata table and every stable reference; remove only duplicate bytes.
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO email_blobs SELECT raw_sha256,raw_eml FROM email_messages")
            mismatch = conn.execute("""SELECT 1 FROM email_messages m JOIN email_blobs b USING(raw_sha256)
                WHERE m.raw_eml<>b.raw_eml LIMIT 1""").fetchone()
            if mismatch:
                raise ValueError("raw hash collision or corrupt evidence; refusing content deduplication")
            conn.execute("ALTER TABLE email_messages DROP COLUMN raw_eml")
    conn.commit()


def connect_email_store(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=120.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=120000")
    ensure_email_store_schema(conn)
    return conn


def connect_email_store_readonly(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def build_storage_ref(
    account_id: str, folder_slug: str, uid: int, raw_sha256: str
) -> str:
    return f"email-store://{account_id}/{folder_slug}/{int(uid)}/{raw_sha256[:16]}"


def upsert_email_message(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    folder: str,
    folder_slug: str,
    uid: int,
    uidvalidity: int | None,
    message_id: str,
    subject: str,
    sender: str,
    recipient: str,
    date_iso: str,
    raw_sha256: str,
    raw_eml: bytes,
    attachments: list[dict[str, Any]],
    ingest_ts: str,
) -> str:
    storage_ref = build_storage_ref(account_id, folder_slug, uid, raw_sha256)
    existing = conn.execute("SELECT raw_eml FROM email_blobs WHERE raw_sha256=?", (raw_sha256,)).fetchone()
    if existing is not None and bytes(existing[0]) != raw_eml:
        raise ValueError("raw hash already exists with different bytes")
    conn.execute("INSERT OR IGNORE INTO email_blobs VALUES (?,?)", (raw_sha256, raw_eml))
    conn.execute(
        "UPDATE email_messages SET present=0 WHERE account_id=? AND folder_slug=? AND uid=? AND storage_ref<>?",
        (account_id, folder_slug, int(uid), storage_ref),
    )
    conn.execute(
        """
        INSERT INTO email_messages (
          account_id, folder, folder_slug, uid, uidvalidity,
          message_id, subject, sender, recipient, date_iso,
          raw_sha256, attachments_json, ingest_ts,
          storage_ref, deleted, deleted_ts
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '')
        ON CONFLICT(storage_ref) DO UPDATE SET
          folder=excluded.folder,
          uidvalidity=excluded.uidvalidity,
          message_id=excluded.message_id,
          subject=excluded.subject,
          sender=excluded.sender,
          recipient=excluded.recipient,
          date_iso=excluded.date_iso,
          raw_sha256=excluded.raw_sha256,
          attachments_json=excluded.attachments_json,
          storage_ref=excluded.storage_ref,
          present=1,
          deleted=0,
          deleted_ts=''
        """,
        (
            account_id,
            folder,
            folder_slug,
            int(uid),
            uidvalidity,
            message_id,
            subject,
            sender,
            recipient,
            date_iso,
            raw_sha256,
            json.dumps(attachments, ensure_ascii=False),
            ingest_ts,
            storage_ref,
        ),
    )
    index_message_body(conn, raw_sha256, raw_eml)
    conn.commit()
    return storage_ref


def fetch_raw_email_by_storage_ref(
    conn: sqlite3.Connection, storage_ref: str
) -> bytes | None:
    row = conn.execute(
        "SELECT raw_eml FROM email_messages JOIN email_blobs USING(raw_sha256) WHERE storage_ref=? LIMIT 1",
        (storage_ref,),
    ).fetchone()
    if not row:
        return None
    raw = row[0]
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, memoryview):
        return raw.tobytes()
    if isinstance(raw, str):
        return raw.encode("utf-8", errors="ignore")
    return bytes(raw)


def _decode_attachments(raw: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _row_to_message(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    return {
        "account_id": row[0],
        "folder": row[1],
        "folder_slug": row[2],
        "uid": int(row[3]),
        "uidvalidity": row[4],
        "message_id": str(row[5] or ""),
        "subject": str(row[6] or ""),
        "from": str(row[7] or ""),
        "to": str(row[8] or ""),
        "date": str(row[9] or ""),
        "attachments": _decode_attachments(str(row[10] or "[]")),
        "ingest_ts": str(row[11] or ""),
        "storage_ref": str(row[12] or ""),
        "raw_sha256": str(row[13] or ""),
    }


def scope_filter(scope: str, folder: str | None = None) -> tuple[list[str], list[Any]]:
    if scope not in SCOPES:
        raise ValueError(f"unknown mail scope: {scope}")
    if folder:
        # An explicit folder is itself a scope (including Junk/Trash).
        return ["present=1", "deleted=0", "(folder_slug=? OR folder=?)"], [folder, folder]
    if scope == "all":
        return ["1=1"], []
    names = ACTIVE_FOLDERS if scope == "active" else HISTORY_FOLDERS
    folder_clause = f"lower(folder) IN ({','.join('?' for _ in names)})"
    if scope == "history":
        folder_clause = "(" + folder_clause + " OR lower(folder) LIKE 'archive/%' OR lower(folder) LIKE 'archives/%' OR lower(folder)='bill' OR lower(folder) LIKE 'bill/%' OR lower(folder) LIKE 'bill %')"
    where = [folder_clause]
    if scope == "active":
        where.extend(["present=1", "deleted=0"])
    else:
        # Retain historical correspondence, but do not resurrect mail known to
        # be in Trash/Junk or deliberately moved to Trash through Relay.
        where.append("""NOT EXISTS (
            SELECT 1 FROM email_messages rejected
            WHERE rejected.account_id=email_messages.account_id
              AND rejected.raw_sha256=email_messages.raw_sha256 AND rejected.present=1
              AND lower(rejected.folder) IN ('trash','junk e-mail','junk','spam','[gmail]/trash','[gmail]/spam')
        )""")
        where.append("""NOT EXISTS (
            SELECT 1 FROM mailbox_operations op WHERE op.storage_ref=email_messages.storage_ref
              AND lower(op.destination_folder) IN ('trash','deleted items','deleted messages','bin')
        )""")
    return where, list(names)


def index_message_body(conn: sqlite3.Connection, raw_sha256: str, raw: bytes) -> None:
    if conn.execute("SELECT 1 FROM email_search WHERE raw_sha256=?", (raw_sha256,)).fetchone():
        return
    conn.execute("INSERT OR IGNORE INTO email_search VALUES (?, ?)",
                 (raw_sha256, extract_text_body(raw).casefold()))


def index_messages(conn: sqlite3.Connection, *, batch_size: int = 100) -> dict[str, Any]:
    indexed = 0
    while True:
        rows = conn.execute("""
            SELECT raw_sha256, raw_eml FROM email_blobs
            WHERE raw_sha256 NOT IN (SELECT raw_sha256 FROM email_search)
            LIMIT ?
        """, (batch_size,)).fetchall()
        if not rows:
            break
        with conn:
            for digest, raw in rows:
                index_message_body(conn, digest, raw)
                indexed += 1
    return {"ok": True, "indexed": indexed,
            "total": conn.execute("SELECT count(*) FROM email_search").fetchone()[0]}


def list_messages(
    conn: sqlite3.Connection,
    *,
    account_ids: list[str] | None = None,
    folder_slug: str | None = None,
    query: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
    scope: str = "active",
) -> list[dict[str, Any]]:
    where, params = scope_filter(scope, folder_slug)
    if account_ids:
        placeholders = ",".join(["?"] * len(account_ids))
        where.append(f"account_id IN ({placeholders})")
        params.extend(account_ids)
    if query:
        like = f"%{query.lower()}%"
        where.append(
            "(lower(subject) LIKE ? OR lower(sender) LIKE ? OR lower(recipient) LIKE ? OR lower(message_id) LIKE ?)"
        )
        params.extend([like, like, like, like])
    if since:
        where.append("datetime(date_iso) >= datetime(?)")
        params.append(since)
    if until:
        where.append("datetime(date_iso) < datetime(?)")
        params.append(until)
    params.append(max(1, min(int(limit), 500)))
    sql = (
        "SELECT account_id, folder, folder_slug, uid, uidvalidity, message_id, subject, "
        "sender, recipient, date_iso, attachments_json, ingest_ts, storage_ref, raw_sha256 "
        "FROM email_messages "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY date_iso DESC, ingest_ts DESC, uid DESC LIMIT ?"
    )
    return [_row_to_message(row) for row in conn.execute(sql, params).fetchall()]


def list_messages_with_raw(
    conn: sqlite3.Connection,
    *,
    account_ids: list[str] | None = None,
    folder_slug: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 200,
    scope: str = "active",
) -> list[tuple[dict[str, Any], bytes]]:
    where, params = scope_filter(scope, folder_slug)
    if account_ids:
        placeholders = ",".join(["?"] * len(account_ids))
        where.append(f"account_id IN ({placeholders})")
        params.extend(account_ids)
    if since:
        where.append("datetime(date_iso) >= datetime(?)")
        params.append(since)
    if until:
        where.append("datetime(date_iso) < datetime(?)")
        params.append(until)
    params.append(max(1, int(limit)))
    sql = (
        "SELECT account_id, folder, folder_slug, uid, uidvalidity, message_id, subject, "
        "sender, recipient, date_iso, attachments_json, ingest_ts, storage_ref, raw_sha256, raw_eml "
        "FROM email_messages JOIN email_blobs USING(raw_sha256) "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY date_iso DESC, ingest_ts DESC, uid DESC LIMIT ?"
    )
    out: list[tuple[dict[str, Any], bytes]] = []
    for row in conn.execute(sql, params).fetchall():
        meta = _row_to_message(row[:14])
        raw = row[14]
        if isinstance(raw, memoryview):
            raw = raw.tobytes()
        elif isinstance(raw, str):
            raw = raw.encode("utf-8", errors="ignore")
        elif not isinstance(raw, bytes):
            raw = bytes(raw)
        out.append((meta, raw))
    return out


def search_messages(
    conn: sqlite3.Connection,
    *,
    queries: Iterable[str],
    account_ids: list[str] | None = None,
    folder_slug: str | None = None,
    since: str | None = None,
    until: str | None = None,
    include_body: bool = True,
    max_scan: int = 500,
    limit: int = 20,
    scope: str = "active",
) -> list[dict[str, Any]]:
    # max_scan is retained for CLI compatibility; the index searches the whole
    # selected scope. It never silently truncates candidate mail by recency.
    terms = list(dict.fromkeys(term.strip() for term in queries if term.strip()))
    if not terms:
        return []
    bounded_limit = max(1, min(int(limit), 500))
    where, params = scope_filter(scope, folder_slug)
    if account_ids:
        where.append(f"account_id IN ({','.join('?' for _ in account_ids)})")
        params.extend(account_ids)
    if since:
        where.append("datetime(date_iso)>=datetime(?)")
        params.append(since)
    if until:
        where.append("datetime(date_iso)<datetime(?)")
        params.append(until)
    if include_body:
        missing = conn.execute(
            f"SELECT 1 FROM email_messages WHERE {' AND '.join(where)} "
            "AND raw_sha256 NOT IN (SELECT raw_sha256 FROM email_search) LIMIT 1", params
        ).fetchone()
        if missing:
            raise ValueError("mail body index incomplete; run opl-relay index before searching")
    matches, match_params = [], []
    for term in terms:
        term = term.casefold()
        matches.append("(instr(lower(subject),?) OR instr(lower(sender),?) OR instr(lower(recipient),?) OR instr(lower(message_id),?))")
        match_params.extend([term] * 4)
        if include_body:
            # FTS trigram accelerates substrings of >=3 codepoints. Short names
            # (e.g. 吉野) use the compact decoded-text table, not raw MIME scans.
            if len(term) >= 3 and not any(c in term for c in "%_"):
                matches.append("raw_sha256 IN (SELECT s.raw_sha256 FROM email_search s JOIN email_search_fts f ON f.rowid=s.rowid WHERE f.body_text LIKE ?)")
                match_params.append(f"%{term}%")
            else:
                matches.append("raw_sha256 IN (SELECT raw_sha256 FROM email_search WHERE instr(body_text,?))")
                match_params.append(term)
    where.append("(" + " OR ".join(matches) + ")")
    sql = f"SELECT {MESSAGE_COLUMNS}, present, deleted FROM email_messages WHERE {' AND '.join(where)} ORDER BY present DESC, datetime(date_iso) DESC, ingest_ts DESC, uid DESC"
    rows, seen = [], set()
    for row in conn.execute(sql, params + match_params):
        meta = _row_to_message(row)
        key = (meta["account_id"], meta["message_id"] or meta["raw_sha256"])
        if key in seen:
            continue
        seen.add(key)
        meta["present"] = bool(row[14]) and not bool(row[15])
        meta["body_hit"] = not any(t.casefold() in " ".join(str(meta[k]) for k in ("subject", "from", "to", "message_id")).casefold() for t in terms)
        rows.append(meta)
        if len(rows) >= bounded_limit:
            break
    return rows


def get_message_by_storage_ref(
    conn: sqlite3.Connection, storage_ref: str
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT account_id, folder, folder_slug, uid, uidvalidity, message_id, subject,
               sender, recipient, date_iso, attachments_json, ingest_ts, storage_ref, raw_sha256
        FROM email_messages
        WHERE storage_ref=?
        LIMIT 1
        """,
        (storage_ref,),
    ).fetchone()
    if not row:
        return None
    message = _row_to_message(row)
    present, deleted = conn.execute("SELECT present,deleted FROM email_messages WHERE storage_ref=?", (storage_ref,)).fetchone()
    message["present"] = bool(present) and not bool(deleted)
    return message


def record_mailbox_move(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    storage_ref: str,
    source_folder: str,
    source_uid: int,
    destination_folder: str,
    method: str,
    raw_sha256: str,
    occurred_at: str,
) -> str:
    operation_ref = f"mailbox-operation://{uuid.uuid4()}"
    cursor = conn.execute(
        """
        UPDATE email_messages
        SET deleted=1, deleted_ts=?, present=0
        WHERE storage_ref=? AND deleted=0 AND present=1
        """,
        (occurred_at, storage_ref),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise LookupError("live local message not found for mailbox receipt")
    conn.execute(
        """
        INSERT INTO mailbox_operations (
          operation_ref, account_id, storage_ref, source_folder, source_uid,
          destination_folder, operation, method, raw_sha256, occurred_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'move', ?, ?, ?)
        """,
        (
            operation_ref,
            account_id,
            storage_ref,
            source_folder,
            int(source_uid),
            destination_folder,
            method,
            raw_sha256,
            occurred_at,
        ),
    )
    conn.commit()
    return operation_ref


def reconcile_folder(conn: sqlite3.Connection, *, account: str, folder: str,
                     folder_slug: str, uidvalidity: int, uids: list[int],
                     checked_at: str, complete: bool) -> None:
    """Project a validated SEARCH ALL snapshot; never delete stored evidence."""
    with conn:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS remote_uids(uid INTEGER PRIMARY KEY)")
        conn.execute("DELETE FROM remote_uids")
        conn.executemany("INSERT INTO remote_uids VALUES (?)", ((uid,) for uid in uids))
        conn.execute("""UPDATE email_messages SET present=CASE
            WHEN uidvalidity=? AND uid IN (SELECT uid FROM remote_uids) AND deleted=0
              AND NOT EXISTS (SELECT 1 FROM email_messages newer
                WHERE newer.account_id=email_messages.account_id
                  AND newer.folder_slug=email_messages.folder_slug
                  AND newer.uid=email_messages.uid AND newer.uidvalidity=email_messages.uidvalidity
                  AND (newer.ingest_ts>email_messages.ingest_ts OR
                    (newer.ingest_ts=email_messages.ingest_ts AND newer.rowid>email_messages.rowid)))
              THEN 1
            ELSE 0 END WHERE account_id=? AND folder_slug=?""",
            (uidvalidity, account, folder_slug))
        conn.execute("""INSERT INTO folder_snapshots VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(account_id,folder_slug) DO UPDATE SET
            folder=excluded.folder, uidvalidity=excluded.uidvalidity,
            remote_count=excluded.remote_count, checked_at=excluded.checked_at,
            complete=excluded.complete""",
            (account, folder, folder_slug, uidvalidity, len(uids), checked_at, int(complete)))


def folder_status(conn: sqlite3.Connection, account: str = "") -> list[dict[str, Any]]:
    where = "WHERE account_id=?" if account else ""
    rows = conn.execute(f"SELECT account_id,folder,folder_slug,uidvalidity,remote_count,checked_at,complete FROM folder_snapshots {where}", (account,) if account else ()).fetchall()
    return [dict(zip(("account_id", "folder", "folder_slug", "uidvalidity", "remote_count", "checked_at", "complete"), row),
                 local_present=conn.execute("SELECT count(*) FROM email_messages WHERE account_id=? AND folder_slug=? AND present=1 AND deleted=0", (row[0], row[2])).fetchone()[0]) for row in rows]


def review_pending(conn: sqlite3.Connection, *, account: str, limit: int = 50) -> dict[str, Any]:
    condition = """account_id=? AND lower(folder)='inbox' AND present=1 AND deleted=0
        AND NOT EXISTS (SELECT 1 FROM mail_reviews r WHERE r.account_id=email_messages.account_id
                        AND r.raw_sha256=email_messages.raw_sha256)"""
    rows = conn.execute(f"SELECT {MESSAGE_COLUMNS} FROM email_messages WHERE {condition} ORDER BY datetime(date_iso) DESC,ingest_ts DESC,uid DESC LIMIT ?", (account, max(1, min(limit, 500)))).fetchall()
    count = conn.execute(f"SELECT count(*) FROM email_messages WHERE {condition}", (account,)).fetchone()[0]
    return {"ok": True, "account": account, "pending_count": count,
            "has_more": count > len(rows), "messages": [_row_to_message(r) for r in rows],
            "folders": folder_status(conn, account)}


REVIEW_ACTIONS = ("remind", "needs_user_reply", "draft_candidate", "archive_candidate", "trash_candidate", "fyi", "needs_more_context", "legacy_reviewed")


def record_reviews(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Store AI/user judgments by exact evidence identity; no provider side effects."""
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        for record in records:
            message = get_message_by_storage_ref(conn, str(record.get("storage_ref", "")))
            if not message:
                raise ValueError("review storage_ref not found")
            action, status = record.get("action"), record.get("status", "open")
            if action not in REVIEW_ACTIONS or status not in ("none", "open", "waiting", "closed"):
                raise ValueError("invalid review action or status")
            conn.execute("""INSERT INTO mail_reviews
                (account_id,raw_sha256,storage_ref,action,status,note,reviewed_at,category,suggested_folder)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account_id,raw_sha256) DO UPDATE SET
                storage_ref=excluded.storage_ref, action=excluded.action, status=excluded.status,
                note=excluded.note, reviewed_at=excluded.reviewed_at,
                category=excluded.category, suggested_folder=excluded.suggested_folder""",
                (message["account_id"], message["raw_sha256"], message["storage_ref"],
                 action, status, str(record.get("note", "")), now,
                 str(record.get("category", "")), str(record.get("suggested_folder", ""))))
    return {"ok": True, "recorded": len(records), "reviewed_at": now}


def review_status(conn: sqlite3.Connection, *, account: str) -> dict[str, Any]:
    pending = review_pending(conn, account=account, limit=1)
    rows = conn.execute("""SELECT r.storage_ref,r.action,r.status,r.note,r.reviewed_at,r.category,r.suggested_folder
        FROM mail_reviews r WHERE r.account_id=? AND r.status IN ('open','waiting') AND EXISTS (
            SELECT 1 FROM email_messages m WHERE m.account_id=r.account_id
              AND m.raw_sha256=r.raw_sha256 AND lower(m.folder)='inbox'
              AND m.present=1 AND m.deleted=0)
        ORDER BY r.reviewed_at""", (account,)).fetchall()
    return {"ok": True, "account": account, "pending_count": pending["pending_count"],
            "folders": pending["folders"],
            "reviewed_count": conn.execute("SELECT count(*) FROM mail_reviews WHERE account_id=?", (account,)).fetchone()[0],
            "last_reviewed_at": conn.execute("SELECT max(reviewed_at) FROM mail_reviews WHERE account_id=?", (account,)).fetchone()[0],
            "open_items": [dict(zip(("storage_ref", "action", "status", "note", "reviewed_at", "category", "suggested_folder"), r)) for r in rows]}
