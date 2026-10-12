import hashlib

from codex_mail_workbench import sync
from codex_mail_workbench.folders import folder_in_scope, folder_slug, legacy_folder_slug, normalize_folder
from codex_mail_workbench.store import (bind_imap_folders, connect_email_store, fetch_raw_email_by_storage_ref,
    get_message_by_storage_ref, list_messages, record_reviews, upsert_email_message)


SENT = "[Gmail]/已发邮件"
ALL = "[Gmail]/所有邮件"
TRASH = "[Gmail]/已删除邮件"
ENCODED_SENT = "[Gmail]/&XfJT0ZCuTvY-"
RAW = b"Message-ID: <isolated@example.test>\r\nSubject: itinerary\r\n\r\nbody"


def put(conn, folder, *, slug=None, uid=7, raw=RAW):
    return upsert_email_message(conn, account_id="work", folder=folder,
        folder_slug=slug or folder_slug(folder), uid=uid, uidvalidity=19,
        message_id="<isolated@example.test>", subject="itinerary", sender="", recipient="",
        date_iso="2026-10-12T00:00:00+00:00", raw_sha256=hashlib.sha256(raw).hexdigest(),
        raw_eml=raw, attachments=[], ingest_ts="2026-10-12T00:00:00+00:00")


def test_encoded_name_and_unicode_share_one_collision_free_identity():
    assert normalize_folder(ENCODED_SENT) == SENT
    assert folder_slug(normalize_folder(ENCODED_SENT)) == folder_slug(SENT)
    assert normalize_folder("R&D") == "R&D"
    assert normalize_folder("Rock &- Roll") == "Rock & Roll"
    assert len({folder_slug(name) for name in (SENT, ALL, TRASH, "[Gmail]/草稿")}) == 4
    assert folder_slug("a/b") != folder_slug("a__b")
    assert folder_slug("Rock &- Roll") != folder_slug("Rock & Roll")
    assert folder_slug("INBOX") == "INBOX"


def test_same_uid_and_raw_in_two_folders_do_not_overwrite_each_other(tmp_path):
    conn = connect_email_store(tmp_path / "mail.sqlite")
    refs = [put(conn, folder) for folder in (SENT, ALL)]
    assert refs[0] != refs[1]
    assert [get_message_by_storage_ref(conn, ref)["folder"] for ref in refs] == [SENT, ALL]
    assert all(get_message_by_storage_ref(conn, ref)["present"] for ref in refs)
    conn.close()


def test_legacy_rebinding_preserves_refs_raw_reviews_and_does_not_duplicate(tmp_path):
    conn = connect_email_store(tmp_path / "mail.sqlite")
    ref = put(conn, ENCODED_SENT, slug=legacy_folder_slug(ENCODED_SENT))
    record_reviews(conn, [{"storage_ref": ref, "action": "fyi", "status": "closed", "note": "completed"}])
    bind_imap_folders(conn, "work", [{"name": SENT, "flags": {"\\sent"}}])
    row = get_message_by_storage_ref(conn, ref)
    assert row["folder"] == SENT and row["folder_slug"] == folder_slug(SENT)
    assert fetch_raw_email_by_storage_ref(conn, ref) == RAW
    assert put(conn, SENT) == ref
    assert conn.execute("SELECT count(*) FROM email_messages").fetchone()[0] == 1
    assert tuple(conn.execute("SELECT status,note FROM mail_reviews").fetchone()) == ("closed", "completed")
    assert list_messages(conn, scope="active")[0]["storage_ref"] == ref
    conn.close()


def test_roles_control_local_active_history_and_trash_exclusion(tmp_path):
    conn = connect_email_store(tmp_path / "mail.sqlite")
    inbox, sent, archive = [put(conn, name, uid=uid) for uid, name in enumerate(("INBOX", SENT, ALL), 1)]
    boxes = [{"name": "INBOX", "flags": set()}, {"name": SENT, "flags": {"\\sent"}},
             {"name": ALL, "flags": {"\\all"}}, {"name": TRASH, "flags": {"\\trash"}}]
    bind_imap_folders(conn, "work", boxes)
    assert {row["storage_ref"] for row in list_messages(conn, scope="active")} == {inbox, sent}
    assert {row["storage_ref"] for row in list_messages(conn, scope="history")} == {inbox, sent, archive}
    put(conn, TRASH, uid=4)
    assert list_messages(conn, scope="history") == []  # Same original now known in Trash.
    assert folder_in_scope(SENT, "active", {"\\Sent"})
    assert not folder_in_scope(ALL, "active", {"\\All"})
    assert folder_in_scope(ALL, "history", {"\\All"})
    assert not folder_in_scope(TRASH, "history", {"\\Trash"})
    conn.close()


def test_batch_fetch_limits_bytes_and_isolates_large_messages():
    small = b"Subject: small\r\n\r\nbody\xff"
    large = b"Subject: large\r\n\r\n" + b"x" * (5 * 1024 * 1024)
    class Tool:
        messages = {1: small, 2: small, 3: large}
        batch_calls, single_calls = [], []
        def metadata(self, folder, uids):
            return {uid: {"size": len(self.messages[uid])} for uid in uids}
        def fetch_bodies(self, folder, uids):
            self.batch_calls.append(uids)
            return {uid: self.messages[uid] for uid in uids}
        def mime(self, uid, *, folder):
            self.single_calls.append(uid)
            return self.messages[uid]
    tool = Tool()
    assert list(sync.fetch_message_batch(tool, [1, 2, 3], folder="INBOX")) == [(1, small), (2, small), (3, large)]
    assert tool.batch_calls == [[1, 2]] and tool.single_calls == [3]
