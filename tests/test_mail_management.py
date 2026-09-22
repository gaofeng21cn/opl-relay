import hashlib
import sqlite3

import pytest

from codex_mail_workbench.store import (
    connect_email_store, ensure_email_store_schema, fetch_raw_email_by_storage_ref,
    list_messages, search_messages, upsert_email_message, record_reviews,
    review_pending, review_status, reconcile_folder,
)


def seed(conn, uid, folder="INBOX", body="ordinary", date="2026-09-20T10:00:00+08:00", epoch=1):
    raw = f"Subject: message {uid}\r\nMessage-ID: <{uid}@test>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}".encode()
    return upsert_email_message(conn, account_id="work", folder=folder,
        folder_slug=folder.replace(" ", "_"), uid=uid, uidvalidity=epoch,
        message_id=f"<{uid}@test>", subject=f"message {uid}", sender="a@test",
        recipient="b@test", date_iso=date, raw_sha256=hashlib.sha256(raw).hexdigest(),
        raw_eml=raw, attachments=[], ingest_ts="2026-09-20T10:01:00+08:00")


def test_scopes_preserve_archive_history_but_exclude_trash(tmp_path):
    conn = connect_email_store(tmp_path / "mail.sqlite")
    old = seed(conn, 1, body="吉野 DDW 行程", date="2025-12-01T00:00:00+08:00")
    archive = seed(conn, 1, folder="Archive", body="吉野 DDW 行程", date="2025-12-01T00:00:00+08:00")
    seed(conn, 2, folder="Trash", body="吉野 DDW 行程")
    reconcile_folder(conn, account="work", folder="INBOX", folder_slug="INBOX",
                     uidvalidity=1, uids=[], checked_at="2026-09-20", complete=True)
    assert search_messages(conn, queries=["吉野"]) == []
    assert [r["storage_ref"] for r in search_messages(conn, queries=["吉野"], scope="history")] == [archive]
    assert fetch_raw_email_by_storage_ref(conn, old) is not None
    assert len(search_messages(conn, queries=["DDW"], scope="all")) == 2
    conn.close()


def test_index_finds_old_body_beyond_former_scan_cap(tmp_path):
    conn = connect_email_store(tmp_path / "mail.sqlite")
    old = seed(conn, 1, body="unique historical needle", date="2025-01-01")
    # This reproduces the actual 2,000-candidate cutoff without parsing MIME on search.
    for uid in range(2, 2005):
        seed(conn, uid)
    assert search_messages(conn, queries=["historical needle"], max_scan=1)[0]["storage_ref"] == old
    conn.close()


def test_pending_uses_identity_and_late_arrival_not_header_date(tmp_path):
    conn = connect_email_store(tmp_path / "mail.sqlite")
    first = seed(conn, 1)
    record_reviews(conn, [{"storage_ref": first, "action": "needs_user_reply", "status": "open", "note": "reviewed",
                           "category": "travel", "suggested_folder": "Bill/Travel"}])
    assert review_pending(conn, account="work")["pending_count"] == 0
    late = seed(conn, 2, date="2025-01-01")
    assert review_pending(conn, account="work")["messages"][0]["storage_ref"] == late
    assert len(review_status(conn, account="work")["open_items"]) == 1
    assert review_status(conn, account="work")["open_items"][0]["category"] == "travel"
    assert review_status(conn, account="work")["open_items"][0]["suggested_folder"] == "Bill/Travel"
    seed(conn, 1, folder="Archive")
    reconcile_folder(conn, account="work", folder="INBOX", folder_slug="INBOX",
                     uidvalidity=1, uids=[2], checked_at="2026-09-20", complete=True)
    assert review_status(conn, account="work")["open_items"] == []
    assert conn.execute("SELECT count(*) FROM mail_reviews").fetchone()[0] == 1
    conn.close()


def test_nested_archives_are_searchable_only_in_requested_history(tmp_path):
    conn = connect_email_store(tmp_path / "mail.sqlite")
    for i, folder in enumerate(("Archive/Academic-History", "Archives/2015", "Bill/Travel", "Bill Travel", "Bill")):
        seed(conn, i, folder=folder, body="historical itinerary")
    assert search_messages(conn, queries=["itinerary"]) == []
    assert len(search_messages(conn, queries=["itinerary"], scope="history")) == 5
    conn.close()


def test_dedup_migration_populates_blobs_and_refuses_conflicting_bytes(tmp_path):
    conn = connect_email_store(tmp_path / "mail.sqlite")
    inbox = seed(conn, 1)
    archived = seed(conn, 1, folder="Archive")
    raw = fetch_raw_email_by_storage_ref(conn, inbox)
    conn.execute("ALTER TABLE email_messages ADD COLUMN raw_eml BLOB")
    conn.execute("UPDATE email_messages SET raw_eml=?", (raw,))
    conn.execute("DELETE FROM email_blobs")
    conn.execute("UPDATE email_messages SET raw_eml=? WHERE storage_ref=?", (b'conflicting evidence', archived))
    conn.commit()
    with pytest.raises(ValueError, match="refusing content deduplication"):
        ensure_email_store_schema(conn)
    assert 'raw_eml' in {r[1] for r in conn.execute('PRAGMA table_info(email_messages)')}
    assert conn.execute('SELECT count(*) FROM email_blobs').fetchone()[0] == 0
    conn.execute('UPDATE email_messages SET raw_eml=?', (raw,))
    conn.commit()
    ensure_email_store_schema(conn)
    assert conn.execute('SELECT count(*) FROM email_blobs').fetchone()[0] == 1
    assert fetch_raw_email_by_storage_ref(conn, archived) == raw
    conn.close()


def test_review_batch_rolls_back_if_any_reference_is_invalid(tmp_path):
    conn = connect_email_store(tmp_path / "mail.sqlite")
    first = seed(conn, 1)
    with pytest.raises(ValueError):
        record_reviews(conn, [{"storage_ref": first, "action": "fyi", "status": "closed"},
                              {"storage_ref": "missing", "action": "fyi"}])
    assert review_pending(conn, account="work")["pending_count"] == 1
    conn.close()


def test_legacy_schema_migration_preserves_evidence_and_allows_uid_reuse(tmp_path):
    conn = connect_email_store(tmp_path / "mail.sqlite")
    first = seed(conn, 1)
    raw = fetch_raw_email_by_storage_ref(conn, first)
    conn.execute("DROP INDEX idx_email_messages_present")
    conn.execute("DROP INDEX idx_email_messages_folder_live")
    conn.execute("ALTER TABLE email_messages ADD COLUMN raw_eml BLOB")
    conn.execute("UPDATE email_messages SET raw_eml=(SELECT b.raw_eml FROM email_blobs b WHERE b.raw_sha256=email_messages.raw_sha256)")
    conn.execute("ALTER TABLE email_messages DROP COLUMN present")
    conn.commit()
    ensure_email_store_schema(conn)
    assert fetch_raw_email_by_storage_ref(conn, first) == raw
    new = seed(conn, 1, body="different UID epoch", epoch=2)
    assert new != first
    assert fetch_raw_email_by_storage_ref(conn, first) == raw
    assert [r["storage_ref"] for r in list_messages(conn)] == [new]
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_raw_content_is_shared_without_losing_folder_references(tmp_path):
    conn = connect_email_store(tmp_path / "mail.sqlite")
    inbox = seed(conn, 1)
    archived = seed(conn, 1, folder="Archive")
    assert inbox != archived
    assert conn.execute("SELECT count(*) FROM email_blobs").fetchone()[0] == 1
    assert fetch_raw_email_by_storage_ref(conn, inbox) == fetch_raw_email_by_storage_ref(conn, archived)
    assert conn.execute("SELECT count(*) FROM email_messages").fetchone()[0] == 2
    conn.close()
