from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .message import extract_text_body


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
          PRIMARY KEY (account_id, folder_slug, uid)
        );

        CREATE INDEX IF NOT EXISTS idx_email_messages_folder_live
        ON email_messages(account_id, folder_slug, deleted);

        CREATE INDEX IF NOT EXISTS idx_email_messages_msgid
        ON email_messages(account_id, message_id);

        CREATE INDEX IF NOT EXISTS idx_email_messages_date_live
        ON email_messages(date_iso, deleted);
        """
    )
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
    conn.execute(
        """
        INSERT INTO email_messages (
          account_id, folder, folder_slug, uid, uidvalidity,
          message_id, subject, sender, recipient, date_iso,
          raw_sha256, raw_eml, attachments_json, ingest_ts,
          storage_ref, deleted, deleted_ts
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '')
        ON CONFLICT(account_id, folder_slug, uid) DO UPDATE SET
          folder=excluded.folder,
          uidvalidity=excluded.uidvalidity,
          message_id=excluded.message_id,
          subject=excluded.subject,
          sender=excluded.sender,
          recipient=excluded.recipient,
          date_iso=excluded.date_iso,
          raw_sha256=excluded.raw_sha256,
          raw_eml=excluded.raw_eml,
          attachments_json=excluded.attachments_json,
          ingest_ts=excluded.ingest_ts,
          storage_ref=excluded.storage_ref,
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
            raw_eml,
            json.dumps(attachments, ensure_ascii=False),
            ingest_ts,
            storage_ref,
        ),
    )
    conn.commit()
    return storage_ref


def fetch_raw_email_by_storage_ref(
    conn: sqlite3.Connection, storage_ref: str
) -> bytes | None:
    row = conn.execute(
        "SELECT raw_eml FROM email_messages WHERE storage_ref=? AND deleted=0 LIMIT 1",
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


def list_messages(
    conn: sqlite3.Connection,
    *,
    account_ids: list[str] | None = None,
    folder_slug: str | None = None,
    query: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    where = ["deleted=0"]
    params: list[Any] = []
    if account_ids:
        placeholders = ",".join(["?"] * len(account_ids))
        where.append(f"account_id IN ({placeholders})")
        params.extend(account_ids)
    if folder_slug:
        where.append("folder_slug=?")
        params.append(folder_slug)
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
) -> list[tuple[dict[str, Any], bytes]]:
    where = ["deleted=0"]
    params: list[Any] = []
    if account_ids:
        placeholders = ",".join(["?"] * len(account_ids))
        where.append(f"account_id IN ({placeholders})")
        params.extend(account_ids)
    if folder_slug:
        where.append("folder_slug=?")
        params.append(folder_slug)
    if since:
        where.append("datetime(date_iso) >= datetime(?)")
        params.append(since)
    if until:
        where.append("datetime(date_iso) < datetime(?)")
        params.append(until)
    params.append(max(1, min(int(limit), 2000)))
    sql = (
        "SELECT account_id, folder, folder_slug, uid, uidvalidity, message_id, subject, "
        "sender, recipient, date_iso, attachments_json, ingest_ts, storage_ref, raw_sha256, raw_eml "
        "FROM email_messages "
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
) -> list[dict[str, Any]]:
    terms = list(dict.fromkeys(term.strip() for term in queries if term.strip()))
    if not terms:
        return []

    bounded_limit = max(1, min(int(limit), 500))
    rows_by_ref: dict[str, dict[str, Any]] = {}
    for term in terms:
        for row in list_messages(
            conn,
            account_ids=account_ids,
            folder_slug=folder_slug,
            query=term,
            since=since,
            until=until,
            limit=max(bounded_limit * 2, 10),
        ):
            rows_by_ref[row["storage_ref"]] = row

    if include_body and len(rows_by_ref) < bounded_limit:
        folded_terms = [term.casefold() for term in terms]
        for meta, raw in list_messages_with_raw(
            conn,
            account_ids=account_ids,
            folder_slug=folder_slug,
            since=since,
            until=until,
            limit=max_scan,
        ):
            if meta["storage_ref"] in rows_by_ref:
                continue
            body = extract_text_body(raw).casefold()
            if not any(term in body for term in folded_terms):
                continue
            meta["body_hit"] = True
            rows_by_ref[meta["storage_ref"]] = meta
            if len(rows_by_ref) >= bounded_limit:
                break

    return sorted(
        rows_by_ref.values(),
        key=lambda item: (item["date"], item["ingest_ts"], item["uid"]),
        reverse=True,
    )[:bounded_limit]


def get_message_by_storage_ref(
    conn: sqlite3.Connection, storage_ref: str
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT account_id, folder, folder_slug, uid, uidvalidity, message_id, subject,
               sender, recipient, date_iso, attachments_json, ingest_ts, storage_ref, raw_sha256
        FROM email_messages
        WHERE storage_ref=? AND deleted=0
        LIMIT 1
        """,
        (storage_ref,),
    ).fetchone()
    if not row:
        return None
    return _row_to_message(row)
