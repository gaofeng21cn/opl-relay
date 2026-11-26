from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codex_mail_workbench import cli
from codex_mail_workbench.native import prune_stale_mail_tool_dirs
from codex_mail_workbench.store import (
    connect_email_store,
    inspect_mail_storage,
    prune_mail_storage,
    record_mailbox_move,
    upsert_email_message,
)


def _insert_sample(conn, *, account: str, folder: str, uid: int, body: bytes) -> tuple[str, str]:
    raw_hash = hashlib.sha256(body).hexdigest()
    ref = upsert_email_message(
        conn,
        account_id=account,
        folder=folder,
        folder_slug=folder.lower(),
        uid=uid,
        uidvalidity=1,
        message_id=f"<msg-{uid}@example.test>",
        subject=f"Subject {uid}",
        sender="alice@example.test",
        recipient="bob@example.test",
        date_iso="2026-10-01T00:00:00+00:00",
        raw_sha256=raw_hash,
        raw_eml=body,
        attachments=[],
        ingest_ts="2026-10-01T00:00:00+00:00",
    )
    return ref, raw_hash


def test_prune_stale_mail_tool_dirs_respects_age_and_symlinks(tmp_path: Path) -> None:
    stale = tmp_path / "mail-tool-old123"
    fresh = tmp_path / "mail-tool-new456"
    other = tmp_path / "backups"
    outside = tmp_path / "outside-target"
    symlink = tmp_path / "mail-tool-link"
    for d in (stale, fresh, other, outside):
        d.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    symlink.symlink_to(outside)

    now = 1_800_000_000.0
    os.utime(stale, (now - 90_000, now - 90_000))
    os.utime(fresh, (now - 60, now - 60))

    preview = prune_stale_mail_tool_dirs(tmp_path, now=now, dry_run=True)
    assert preview == ["mail-tool-old123"]
    assert stale.exists()

    removed = prune_stale_mail_tool_dirs(tmp_path, now=now, dry_run=False)
    assert removed == ["mail-tool-old123"]
    assert not stale.exists()
    assert fresh.exists()
    assert other.exists()
    assert symlink.is_symlink()
    assert (outside / "keep.txt").exists()


def test_storage_inspect_and_prune_preserves_live_and_history_messages(tmp_path: Path) -> None:
    db_path = tmp_path / "mail.sqlite"
    conn = connect_email_store(db_path)
    try:
        live_body = b"Subject: live\r\n\r\n" + (b"L" * 4096)
        hist_body = b"Subject: hist\r\n\r\n" + (b"H" * 4096)
        old_del_body = b"Subject: olddel\r\n\r\n" + (b"D" * 8192)
        recent_del_body = b"Subject: newdel\r\n\r\n" + (b"R" * 4096)
        orphan_body = b"Subject: orphan\r\n\r\n" + (b"O" * 2048)

        _insert_sample(conn, account="acct", folder="INBOX", uid=1, body=live_body)
        hist_ref, hist_hash = _insert_sample(conn, account="acct", folder="INBOX", uid=2, body=hist_body)
        conn.execute("UPDATE email_messages SET present=0 WHERE storage_ref=?", (hist_ref,))
        old_del_ref, old_del_hash = _insert_sample(conn, account="acct", folder="INBOX", uid=3, body=old_del_body)
        recent_del_ref, recent_del_hash = _insert_sample(conn, account="acct", folder="INBOX", uid=4, body=recent_del_body)
        now = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
        record_mailbox_move(
            conn,
            account_id="acct",
            storage_ref=old_del_ref,
            source_folder="INBOX",
            source_uid=3,
            destination_folder="Trash",
            method="imap_move",
            raw_sha256=old_del_hash,
            occurred_at=(now - timedelta(days=45)).isoformat(),
        )
        record_mailbox_move(
            conn,
            account_id="acct",
            storage_ref=recent_del_ref,
            source_folder="INBOX",
            source_uid=4,
            destination_folder="Trash",
            method="imap_move",
            raw_sha256=recent_del_hash,
            occurred_at=(now - timedelta(days=5)).isoformat(),
        )
        orphan_hash = hashlib.sha256(orphan_body).hexdigest()
        conn.execute("INSERT INTO email_blobs VALUES (?, ?)", (orphan_hash, orphan_body))
        conn.commit()

        stale_tool = tmp_path / "mail-tool-stale1"
        stale_tool.mkdir()
        os.utime(stale_tool, (now.timestamp() - 90_000, now.timestamp() - 90_000))

        stats = inspect_mail_storage(conn, state_dir=tmp_path, db_path=db_path)
        assert stats["ok"] is True
        assert stats["orphan_blob_count"] == 1
        assert stats["deleted_only_blob_count"] == 2
        assert stats["stale_mail_tool_dirs"] == ["mail-tool-stale1"]

        # Dry-run with include_deleted does not mutate DB or dirs.
        dry = prune_mail_storage(conn, state_dir=tmp_path, include_deleted=True, deleted_retention_days=30, dry_run=True, now=now)
        assert dry["orphan_blob_count"] == 1
        assert dry["pruned_deleted_blob_count"] == 1
        assert dry["removed_mail_tool_dirs"] == ["mail-tool-stale1"]
        assert stale_tool.exists()

        # Execute cleans orphan + old deleted (>30d) + stale mail-tool dir, while preserving live, present=0 history, and recent deleted (<30d).
        applied = prune_mail_storage(conn, state_dir=tmp_path, include_deleted=True, deleted_retention_days=30, vacuum=True, dry_run=False, now=now)
        assert applied["dry_run"] is False
        assert applied["removed_blob_count"] == 2
        assert applied["vacuumed"] is True
        assert not stale_tool.exists()

        remaining_hashes = {row[0] for row in conn.execute("SELECT raw_sha256 FROM email_blobs").fetchall()}
        assert orphan_hash not in remaining_hashes
        assert old_del_hash not in remaining_hashes
        assert hist_hash in remaining_hashes
        assert recent_del_hash in remaining_hashes
    finally:
        conn.close()


def test_cli_storage_stats_and_prune_default_dry_run(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("OPL_MAIL_STATE_DIR", str(tmp_path))
    db_path = tmp_path / "mail.sqlite"
    conn = connect_email_store(db_path)
    orphan = b"Subject: cli-orphan\r\n\r\nbody"
    orphan_hash = hashlib.sha256(orphan).hexdigest()
    conn.execute("INSERT INTO email_blobs VALUES (?, ?)", (orphan_hash, orphan))
    conn.commit()
    conn.close()

    rc = cli.main(["--db", str(db_path), "--json", "storage-stats"])
    assert rc == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["orphan_blob_count"] == 1

    rc = cli.main(["--db", str(db_path), "--json", "storage-prune"])
    assert rc == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True
    assert dry["removed_blob_count"] == 1

    rc = cli.main(["--db", str(db_path), "--json", "storage-prune", "--execute"])
    assert rc == 0
    executed = json.loads(capsys.readouterr().out)
    assert executed["dry_run"] is False
    assert executed["removed_blob_count"] == 1
