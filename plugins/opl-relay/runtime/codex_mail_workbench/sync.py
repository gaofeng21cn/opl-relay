from __future__ import annotations

import hashlib
import imaplib
import json
import re
import socket
import fcntl
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .config import MailAccount, keychain_read_secret, load_account
from .message import extract_attachments, parse_headers
from .paths import default_config_path, default_db_path, default_sync_state_dir
from .store import ACTIVE_FOLDERS, HISTORY_FOLDERS, connect_email_store, reconcile_folder, upsert_email_message


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def sanitize_folder_name(folder: str) -> str:
    text = folder.strip()
    if not text:
        return "UNKNOWN"
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    text = text.replace("\\", "_").replace("/", "__")
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    cleaned = text.strip("._")
    if cleaned:
        return cleaned
    return f"folder_{digest}"


def parse_imap_list_name(raw_line: bytes) -> str:
    line = raw_line.decode("utf-8", errors="replace")
    match = re.match(r'^\(.*\)\s+"[^"]*"\s+(.*)$', line)
    if not match:
        return line.strip()
    name = match.group(1).strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return name


def should_sync_folder(name: str, include: Iterable[str], exclude: Iterable[str]) -> bool:
    if name in exclude:
        return False
    include_set = list(include)
    return "*" in include_set or name in include_set


def connect_imap(account: MailAccount, timeout_sec: float = 60.0) -> imaplib.IMAP4:
    security = account.imap.security.lower()
    if security == "ssl":
        client = imaplib.IMAP4_SSL(account.imap.host, account.imap.port, timeout=timeout_sec)
    else:
        client = imaplib.IMAP4(account.imap.host, account.imap.port, timeout=timeout_sec)
        if security == "starttls":
            client.starttls()
    sock = getattr(client, "sock", None)
    if sock is not None:
        sock.settimeout(timeout_sec)
    return client


def load_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_fetch_uid_rfc822_map(data: list[object]) -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    for item in data:
        if not (isinstance(item, tuple) and len(item) >= 2):
            continue
        meta, raw = item[0], item[1]
        if not (isinstance(meta, bytes) and isinstance(raw, bytes)):
            continue
        match = re.search(rb"UID\s+(\d+)", meta)
        if not match:
            continue
        out[int(match.group(1))] = raw
    return out


def fetch_message_batch(client: imaplib.IMAP4, uids: list[int]):
    """Bound IMAP responses by bytes; large attachments use partial PEEK reads."""
    typ, data = client.uid("fetch", ",".join(map(str, uids)), "(UID RFC822.SIZE)")
    if typ != "OK":
        raise ValueError("FETCH sizes rejected")
    sizes = {}
    for item in data:
        if not isinstance(item, bytes):
            continue
        uid_match = re.search(rb"UID\s+(\d+)", item)
        size_match = re.search(rb"RFC822.SIZE\s+(\d+)", item)
        if uid_match and size_match:
            sizes[int(uid_match[1])] = int(size_match[1])
    small = []
    small_bytes = 0

    def refetch_is_identical(uid: int, expected: bytes) -> bool:
        """Accept a size discrepancy only when a second full read is byte-identical.

        Some servers advertise an RFC822.SIZE that disagrees with the literal
        they actually return. A single short read could also be a truncated
        body, so the anomaly is confirmed by an independent re-retrieval rather
        than by trusting the declared size.
        """
        try:
            typ, fetched = client.uid("fetch", str(uid), "(UID BODY.PEEK[])")
        except (OSError, imaplib.IMAP4.error):
            return False
        if typ != "OK":
            return False
        return parse_fetch_uid_rfc822_map(fetched).get(uid) == expected

    def fetch_small(group):
        typ, fetched = client.uid("fetch", ",".join(map(str, group)), "(UID BODY.PEEK[])")
        if typ != "OK":
            raise ValueError("FETCH body rejected")
        raw_map = parse_fetch_uid_rfc822_map(fetched)
        for uid in group:
            raw = raw_map.get(uid)
            if raw is None:
                continue  # A concurrently removed message remains a retryable gap.
            if len(raw) != sizes[uid]:
                if not raw or not refetch_is_identical(uid, raw):
                    raise ValueError(f"FETCH byte count mismatch for UID {uid}")
            yield uid, raw

    for uid in uids:
        size = sizes.get(uid)
        if size is None:
            continue
        if small and small_bytes + size > 4 * 1024 * 1024:
            yield from fetch_small(small)
            small, small_bytes = [], 0
        if size <= 4 * 1024 * 1024:
            small.append(uid)
            small_bytes += size
            continue
        parts = []
        for offset in range(0, size, 1024 * 1024):
            length = min(1024 * 1024, size - offset)
            try:
                typ, fetched = client.uid("fetch", str(uid), f"(UID BODY.PEEK[]<{offset}.{length}>)")
            except (OSError, imaplib.IMAP4.error) as exc:
                raise ValueError(f"partial FETCH failed for UID {uid} at byte {offset}: {type(exc).__name__}") from exc
            part = parse_fetch_uid_rfc822_map(fetched).get(uid) if typ == "OK" else None
            if part is None or len(part) != length:
                raise ValueError(f"partial FETCH byte count mismatch for UID {uid}")
            parts.append(part)
        yield uid, b"".join(parts)
    if small:
        yield from fetch_small(small)


def sync_account(**kwargs: object) -> dict[str, object]:
    # All hosts share this lock. Avoid overlapping syncs and stale cursor writes.
    db_path = Path(kwargs.get("db_path", default_db_path()))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with db_path.with_suffix(".sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"ok": False, "error": "sync already running", "folders": []}
        return _sync_account(**kwargs)


def _sync_account(
    *,
    config_path: Path = default_config_path(),
    db_path: Path = default_db_path(),
    state_dir: Path = default_sync_state_dir(),
    account_id: str,
    mode: str = "incremental",
    limit_per_folder: int | None = None,
    dry_run: bool = False,
    scope: str = "all",
    folder: str = "",
) -> dict[str, object]:
    account = load_account(config_path, account_id)
    credential = keychain_read_secret(account.imap.credential_ref,
                                      fallback_keychain=account.imap.fallback_keychain)
    client = connect_imap(account)
    client.login(account.imap.username, credential.value)
    state_path = state_dir / f"{account_id}.json"
    state = load_json(state_path, {"account_id": account_id, "folders": {}})
    assert isinstance(state, dict)
    folders_state = state.setdefault("folders", {})
    assert isinstance(folders_state, dict)
    conn = connect_email_store(db_path)
    summary: dict[str, object] = {
        "ok": True,
        "account": account_id,
        "credential_source": credential.source,
        "mode": mode,
        "folders": [],
        "new_messages": 0,
        "dry_run": dry_run,
    }
    try:
        typ, box_lines = client.list()
        if typ != "OK" or not box_lines:
            raise RuntimeError("无法读取 IMAP 文件夹列表")
        sync_folders = [
            parse_imap_list_name(raw)
            for raw in box_lines
            if isinstance(raw, bytes)
        ]
        sync_folders = [
            folder
            for folder in sync_folders
            if should_sync_folder(folder, account.include_folders, account.exclude_folders)
        ]
        if folder:
            sync_folders = [name for name in sync_folders if name == folder or sanitize_folder_name(name) == folder]
            if not sync_folders:
                raise ValueError("requested folder not found in configured IMAP scope")
        elif scope != "all":
            names = ACTIVE_FOLDERS if scope == "active" else HISTORY_FOLDERS
            sync_folders = [name for name in sync_folders if name.lower() in names or (
                scope == "history" and (name.lower() == "bill" or
                name.lower().startswith(("archive/", "archives/", "bill/", "bill "))))]
        for folder_name in sync_folders:
            folder_slug = sanitize_folder_name(folder_name)
            if not dry_run:
                with conn:
                    conn.execute("UPDATE folder_snapshots SET complete=0 WHERE account_id=? AND folder_slug=?", (account_id, folder_slug))
            select_name = folder_name.replace("\\", "\\\\").replace('"', '\\"')
            try:
                typ_sel, exists_data = client.select(f'"{select_name}"', readonly=True)
                if typ_sel != "OK":
                    raise ValueError("SELECT failed")
                uidvalidity = int(client.response("UIDVALIDITY")[1][0])
                expected_count = int(exists_data[0])
                typ_search, uid_data = client.uid("search", "ALL")
                if typ_search != "OK" or len(uid_data) != 1 or not isinstance(uid_data[0], bytes):
                    raise ValueError("incomplete SEARCH ALL response")
                remote_uids = sorted(int(u) for u in uid_data[0].split())
                if uidvalidity <= 0 or any(u <= 0 for u in remote_uids) or len(set(remote_uids)) != expected_count or len(remote_uids) != expected_count:
                    raise ValueError("SELECT/SEARCH snapshot mismatch; retry sync")
                checked_at = now_iso()
            except (TypeError, ValueError, IndexError, OSError, imaplib.IMAP4.error) as exc:
                summary["ok"] = False
                summary["folders"].append({"folder": folder_name, "complete": False, "error": str(exc)})
                continue
            known = {row[0] for row in conn.execute(
                "SELECT uid FROM email_messages WHERE account_id=? AND folder_slug=? AND uidvalidity=? AND deleted=0",
                (account_id, folder_slug, uidvalidity))}
            missing_uids = [uid for uid in remote_uids if uid not in known]
            # Fresh mail must arrive before a slow historical attachment.
            scan_uids = sorted(remote_uids if mode == "initial" else missing_uids, reverse=True)
            if limit_per_folder is not None:
                max_scan = max(0, int(limit_per_folder))
                scan_uids = scan_uids[:max_scan]
            folder_new = 0
            fetched_uids: list[int] = []
            fetch_errors: list[str] = []
            for offset in range(0, len(scan_uids), 10):
                batch = scan_uids[offset:offset + 10]
                try:
                    for uid, raw_msg in fetch_message_batch(client, batch):
                        fetched_uids.append(uid)
                        headers = parse_headers(raw_msg)
                        raw_hash = hashlib.sha256(raw_msg).hexdigest()
                        if not dry_run:
                            upsert_email_message(
                                conn, account_id=account_id, folder=folder_name,
                                folder_slug=folder_slug, uid=uid, uidvalidity=uidvalidity,
                                message_id=headers["message_id"], subject=headers["subject"],
                                sender=headers["from"], recipient=headers["to"], date_iso=headers["date"],
                                raw_sha256=raw_hash, raw_eml=raw_msg,
                                attachments=extract_attachments(raw_msg), ingest_ts=now_iso(),
                            )
                        folder_new += 1
                except (socket.timeout, TimeoutError, OSError, ValueError, imaplib.IMAP4.error) as exc:
                    fetch_errors.append(f"{type(exc).__name__}: {exc}")
                    break  # The socket may be unusable; retry missing UIDs next run.
            available = known | set(fetched_uids)
            remaining = set(remote_uids) - available
            failed = set(scan_uids) - set(fetched_uids)
            complete = not remaining and not failed
            if not dry_run:
                reconcile_folder(conn, account=account_id, folder=folder_name,
                                 folder_slug=folder_slug, uidvalidity=uidvalidity,
                                 uids=remote_uids, checked_at=checked_at, complete=complete)
                # Informational cursor only. Actual resumption uses missing UIDs
                # so older holes are retried even if a newer UID was fetched.
                state_uid = 0
                for uid in remote_uids:
                    if uid not in available:
                        break
                    state_uid = uid
                folders_state[folder_name] = {
                    "folder_name": folder_name,
                    "last_uid_synced": state_uid,
                    "uidvalidity": uidvalidity,
                    "errors": fetch_errors,
                    "last_sync_at": checked_at,
                    "remote_count_last_seen": len(remote_uids),
                    "complete": complete,
                    "remaining": len(remaining),
                }
            if not complete:
                summary["ok"] = False
            summary["new_messages"] = int(summary["new_messages"]) + folder_new
            assert isinstance(summary["folders"], list)
            summary["folders"].append(
                {
                    "folder": folder_name,
                    "folder_slug": folder_slug,
                    "remote_count": len(remote_uids),
                    "scanned": len(scan_uids),
                    "new_messages": folder_new,
                    "complete": complete,
                    "remaining": len(remaining),
                    "failed": len(failed),
                    "uidvalidity": uidvalidity,
                    "errors": fetch_errors,
                }
            )
        if not dry_run:
            dump_json(state_path, state)
        return summary
    finally:
        conn.close()
        try:
            client.logout()
        except Exception:
            pass
