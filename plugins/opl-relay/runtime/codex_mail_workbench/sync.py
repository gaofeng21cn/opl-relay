from __future__ import annotations

import hashlib
import json
import re
import fcntl
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .config import load_account
from .native import Himalaya, NativeError
from .message import extract_attachments, parse_headers
from .paths import default_config_path, default_db_path, default_sync_state_dir
from .store import ACTIVE_FOLDERS, HISTORY_FOLDERS, connect_email_store, reconcile_folder, upsert_email_message


DEFAULT_SYNC_MAX_SECONDS = 120.0


class SyncBudgetExceeded(Exception):
    """The bounded sync window elapsed before the requested scope completed."""


class SyncBudget:
    def __init__(self, max_duration_sec: float | None) -> None:
        seconds = DEFAULT_SYNC_MAX_SECONDS if max_duration_sec is None else float(max_duration_sec)
        if seconds <= 0:
            raise ValueError("max_duration_sec must be greater than zero")
        self.seconds = seconds
        self.deadline = time.monotonic() + seconds

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def before_io(self, client: Himalaya | None, stage: str) -> None:
        remaining = self.remaining()
        if remaining <= 0:
            raise SyncBudgetExceeded(f"sync time budget exceeded before {stage}")


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


def should_sync_folder(name: str, include: Iterable[str], exclude: Iterable[str]) -> bool:
    if name in exclude:
        return False
    include_set = list(include)
    return "*" in include_set or name in include_set


def load_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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


def fetch_message_batch(client, uids, *, folder, budget=None):
    """Delegate MIME transport; retain byte-count and stable-read verification."""
    from concurrent.futures import ThreadPoolExecutor
    if budget:
        budget.before_io(client, "message metadata")
    metadata = client.metadata(folder, uids)
    def retrieve(uid):
        raw = client.mime(uid, folder=folder)
        if not raw or (len(raw) != metadata[uid]["size"] and client.mime(uid, folder=folder) != raw):
            raise ValueError(f"Native FETCH byte count mismatch for UID {uid}")
        return raw
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = [(uid, pool.submit(retrieve, uid)) for uid in uids if uid in metadata]
        for uid, future in pending:
            yield uid, future.result()


def _sync_account(
    *,
    config_path: Path = default_config_path(),
    db_path: Path = default_db_path(),
    state_dir: Path = default_sync_state_dir(),
    account_id: str,
    mode: str = "incremental",
    limit_per_folder: int | None = None,
    dry_run: bool = False,
    scope: str = "active",
    folder: str = "",
    max_duration_sec: float | None = DEFAULT_SYNC_MAX_SECONDS,
) -> dict[str, object]:
    account = load_account(config_path, account_id)
    if account.provider == "microsoft_graph":
        from .graph import sync_graph_account
        return sync_graph_account(account=account, db_path=db_path, state_dir=state_dir,
                                  mode=mode, limit_per_folder=limit_per_folder,
                                  dry_run=dry_run, scope=scope, folder=folder,
                                  max_duration_sec=max_duration_sec)
    budget = SyncBudget(max_duration_sec)
    client = Himalaya(account, deadline=budget.deadline)
    state_path = state_dir / f"{account_id}.json"
    state = load_json(state_path, {"account_id": account_id, "folders": {}})
    assert isinstance(state, dict)
    folders_state = state.setdefault("folders", {})
    assert isinstance(folders_state, dict)
    conn = connect_email_store(db_path)
    summary: dict[str, object] = {
        "ok": True,
        "account": account_id,
        "credential_source": "keychain",
        "backend": "himalaya",
        "mode": mode,
        "folders": [],
        "new_messages": 0,
        "dry_run": dry_run,
        "max_duration_sec": budget.seconds,
        "timed_out": False,
        "next_folders": [],
    }
    try:
        try:
            budget.before_io(client, "folder list")
            boxes = client.folders()
        except SyncBudgetExceeded as exc:
            summary["ok"] = False
            summary["timed_out"] = True
            summary["error"] = str(exc)
            return summary
        except (OSError, NativeError) as exc:
            summary["ok"] = False
            summary["timed_out"] = budget.remaining() <= 0 or getattr(exc, "code", "") == "timeout"
            summary["error"] = f"{type(exc).__name__}: {exc}"
            return summary
        sync_folders = [b["name"] for b in boxes if "\\noselect" not in b["flags"]]
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
        for folder_index, folder_name in enumerate(sync_folders):
            summary["next_folders"] = sync_folders[folder_index:]
            folder_slug = sanitize_folder_name(folder_name)
            if not dry_run:
                with conn:
                    conn.execute("UPDATE folder_snapshots SET complete=0 WHERE account_id=? AND folder_slug=?", (account_id, folder_slug))
            try:
                budget.before_io(client, f"STATUS {folder_name}")
                identity = client.status(folder_name)
                uidvalidity = identity["uidvalidity"]
                expected_count = identity["count"]
                budget.before_io(client, f"SEARCH {folder_name}")
                remote_uids = sorted(client.ids(folder_name))
                if uidvalidity <= 0 or any(u <= 0 for u in remote_uids) or len(set(remote_uids)) != expected_count or len(remote_uids) != expected_count:
                    raise ValueError("STATUS/SEARCH snapshot mismatch; retry sync")
                checked_at = now_iso()
            except SyncBudgetExceeded as exc:
                summary["ok"] = False
                summary["timed_out"] = True
                summary["error"] = str(exc)
                summary["folders"].append({"folder": folder_name, "complete": False, "error": str(exc)})
                break
            except (TypeError, ValueError, IndexError, OSError, NativeError) as exc:
                summary["ok"] = False
                summary["timed_out"] = budget.remaining() <= 0 or getattr(exc, "code", "") == "timeout"
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
            if dry_run:
                # A dry-run is an enumeration preview. It must never download
                # message bodies: the old behavior turned a harmless planning
                # command into a full historical fetch and kept the Weixin
                # foreground turn polling until its 900-second limit.
                if scan_uids:
                    fetch_errors.append("dry-run: message bodies not fetched")
            else:
                for offset in range(0, len(scan_uids), 10):
                    batch = scan_uids[offset:offset + 10]
                    try:
                        for uid, raw_msg in fetch_message_batch(client, batch, folder=folder_name, budget=budget):
                            fetched_uids.append(uid)
                            headers = parse_headers(raw_msg)
                            raw_hash = hashlib.sha256(raw_msg).hexdigest()
                            upsert_email_message(
                                conn, account_id=account_id, folder=folder_name,
                                folder_slug=folder_slug, uid=uid, uidvalidity=uidvalidity,
                                message_id=headers["message_id"], subject=headers["subject"],
                                sender=headers["from"], recipient=headers["to"], date_iso=headers["date"],
                                raw_sha256=raw_hash, raw_eml=raw_msg,
                                attachments=extract_attachments(raw_msg), ingest_ts=now_iso(),
                            )
                            folder_new += 1
                    except SyncBudgetExceeded as exc:
                        fetch_errors.append(str(exc))
                        summary["timed_out"] = True
                        break
                    except (TimeoutError, OSError, ValueError) as exc:
                        fetch_errors.append(f"{type(exc).__name__}: {exc}")
                        summary["timed_out"] = budget.remaining() <= 0 or getattr(exc, "code", "") == "timeout"
                        break  # The socket may be unusable; retry missing UIDs next run.
            available = known | set(fetched_uids)
            remaining = set(remote_uids) - available
            failed = set(scan_uids) - set(fetched_uids)
            complete = not remaining and (dry_run or not failed)
            if not dry_run and not summary["timed_out"]:
                try:
                    budget.before_io(client, "folder identity readback")
                    if client.status(folder_name)["uidvalidity"] != uidvalidity:
                        raise ValueError("UID epoch changed during sync")
                except (SyncBudgetExceeded, ValueError, OSError) as exc:
                    summary["ok"] = False
                    summary["timed_out"] = budget.remaining() <= 0 or getattr(exc, "code", "") == "timeout"
                    summary["new_messages"] += folder_new
                    summary["folders"].append({"folder": folder_name, "complete": False, "new_messages": folder_new, "error": str(exc)})
                    break
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
            folder_result = {
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
            if dry_run:
                folder_result["would_fetch"] = len(scan_uids)
                folder_result["fetch_skipped"] = True
            summary["folders"].append(folder_result)
            summary["next_folders"] = sync_folders[folder_index + 1:]
            if summary["timed_out"]:
                break
        if not dry_run:
            dump_json(state_path, state)
        return summary
    finally:
        conn.close()
