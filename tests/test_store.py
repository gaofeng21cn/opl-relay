from pathlib import Path

from codex_mail_workbench.store import (
    connect_email_store,
    fetch_raw_email_by_storage_ref,
    list_messages,
    search_messages,
    upsert_email_message,
)


def test_upsert_list_and_fetch_raw_message(tmp_path: Path) -> None:
    conn = connect_email_store(tmp_path / "mail.sqlite")
    try:
        raw = b"Subject: hello\r\nMessage-ID: <m1@example.test>\r\n\r\nbody"
        storage_ref = upsert_email_message(
            conn,
            account_id="work",
            folder="INBOX",
            folder_slug="INBOX",
            uid=10,
            uidvalidity=123,
            message_id="<m1@example.test>",
            subject="hello",
            sender="a@example.test",
            recipient="b@example.test",
            date_iso="2026-05-17T09:00:00+08:00",
            raw_sha256="1" * 64,
            raw_eml=raw,
            attachments=[],
            ingest_ts="2026-05-17T09:01:00+08:00",
        )

        rows = list_messages(conn, account_ids=["work"], limit=5)

        assert fetch_raw_email_by_storage_ref(conn, storage_ref) == raw
        assert rows[0]["storage_ref"] == storage_ref
        assert rows[0]["subject"] == "hello"
    finally:
        conn.close()


def test_search_messages_combines_metadata_and_body_hits(tmp_path: Path) -> None:
    conn = connect_email_store(tmp_path / "mail.sqlite")
    try:
        for uid, subject, body in [
            (1, "Metadata match", "ordinary body"),
            (2, "Other subject", "body-only needle"),
        ]:
            upsert_email_message(
                conn,
                account_id="work",
                folder="INBOX",
                folder_slug="INBOX",
                uid=uid,
                uidvalidity=123,
                message_id=f"<search-{uid}@example.test>",
                subject=subject,
                sender="a@example.test",
                recipient="b@example.test",
                date_iso=f"2026-05-1{uid}T09:00:00+08:00",
                raw_sha256=str(uid) * 64,
                raw_eml=f"Subject: {subject}\r\n\r\n{body}".encode(),
                attachments=[],
                ingest_ts=f"2026-05-1{uid}T09:01:00+08:00",
            )

        rows = search_messages(
            conn,
            queries=["Metadata", "needle"],
            limit=10,
        )

        assert [row["uid"] for row in rows] == [2, 1]
        assert rows[0]["body_hit"] is True
    finally:
        conn.close()


def test_list_messages_filters_by_date_window(tmp_path: Path) -> None:
    conn = connect_email_store(tmp_path / "mail.sqlite")
    try:
        for uid, subject, date_iso in [
            (1, "before", "2026-05-16T09:00:00+08:00"),
            (2, "inside", "2026-05-17T09:00:00+08:00"),
            (3, "after", "2026-05-18T09:00:00+08:00"),
        ]:
            upsert_email_message(
                conn,
                account_id="work",
                folder="INBOX",
                folder_slug="INBOX",
                uid=uid,
                uidvalidity=123,
                message_id=f"<m{uid}@example.test>",
                subject=subject,
                sender="a@example.test",
                recipient="b@example.test",
                date_iso=date_iso,
                raw_sha256=str(uid) * 64,
                raw_eml=b"Subject: test\r\n\r\nbody",
                attachments=[],
                ingest_ts=date_iso,
            )

        rows = list_messages(
            conn,
            account_ids=["work"],
            since="2026-05-17T00:00:00+08:00",
            until="2026-05-18T00:00:00+08:00",
            limit=10,
        )

        assert [row["subject"] for row in rows] == ["inside"]
    finally:
        conn.close()
