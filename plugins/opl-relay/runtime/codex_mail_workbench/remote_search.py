"""Server search on demand; cache only matches, never claim a folder snapshot."""
from __future__ import annotations

import hashlib
import time
from datetime import datetime

from .message import extract_attachments, parse_headers
from .native import Himalaya
from .store import ACTIVE_FOLDERS, HISTORY_FOLDERS, connect_email_store, get_message_by_storage_ref
from .sync import now_iso, sanitize_folder_name


def search_remote(*, account, db_path, query, scope="active", folder="", limit=20,
                  since="", until="", max_seconds=120):
    if not query.strip() or not 1 <= limit <= 100:
        raise ValueError("Remote search needs a query and a limit between 1 and 100")
    deadline = time.monotonic() + max_seconds
    control, graph = None, None
    conn = connect_email_store(db_path)
    result = {"ok": True, "source": "server", "scope": scope, "messages": [],
              "folders_searched": [], "search_complete": True, "result_limit": limit,
              "folder_snapshot_updated": False, "errors": []}
    try:
        if account.graph:
            from .graph import GraphClient, _schema
            graph = GraphClient(account, deadline=deadline)
            graph.identity()
            folders = graph.folders()
            _schema(conn)
        else:
            from .mailbox import _list_mailboxes
            control = Himalaya(account, deadline=deadline)
            folders = [{"id": f.name, "path": f.name, "roles": f.flags} for f in _list_mailboxes(control)
                       if "\\NOSELECT" not in {a.upper() for a in f.flags}]
        def selected(item):
            name = item["path"].lower()
            if folder:
                return folder in {item["id"], item["path"], sanitize_folder_name(item["path"])}
            if scope == "all":
                return True
            roles = {r.upper() for r in item.get("roles", ())}
            if scope == "active":
                return name in ACTIVE_FOLDERS or "\\SENT" in roles
            return (name in HISTORY_FOLDERS or roles.intersection({"\\SENT", "\\ALL", "\\ARCHIVE"}) or
                    name == "bill" or name.startswith(("archive/", "archives/", "bill/", "bill ")))
        folders = [f for f in folders if selected(f)]
        if not folders:
            raise ValueError("No remote folders match the requested scope")
        seen = set()
        for item in folders:
            remaining = limit - len(result["messages"])
            if remaining <= 0 or time.monotonic() >= deadline:
                result["search_complete"] = False
                break
            try:
                tool = Himalaya(account, token=graph._access_token() if graph else None, deadline=deadline)
                # Server DSL uses day boundaries. Exact requested ISO bounds
                # are checked on the original Date header below.
                page = tool.search(query, folder=item["id"], limit=remaining + 1)
                envelopes = page["envelopes"]
                if len(envelopes) > remaining:
                    result["search_complete"] = False
                result["folders_searched"].append(item["path"])
                epoch = None
                if control:
                    epoch = control.status(item["path"])["uidvalidity"]
                for envelope in envelopes[:remaining]:
                    remote_id = envelope["id"]
                    if graph:
                        meta = graph.metadata(remote_id)
                        if meta["parentFolderId"] != item["id"]:
                            raise ValueError("Search match moved; repeat the server search")
                        remote_id = meta["id"]  # Read-only conversion to ImmutableId.
                    raw = tool.mime(remote_id, folder=item["path"])
                    headers = parse_headers(raw)
                    advertised = envelope.get("message-id", "")
                    if advertised and headers["message_id"].strip("<>") != advertised.strip("<>"):
                        raise ValueError("Search result identity differs from MIME")
                    date = datetime.fromisoformat(headers["date"]) if headers["date"] else None
                    if ((since and (date is None or date < datetime.fromisoformat(since))) or
                            (until and (date is None or date >= datetime.fromisoformat(until)))):
                        continue
                    digest = hashlib.sha256(raw).hexdigest()
                    if digest in seen:
                        continue
                    if graph:
                        from .graph import _store_mime
                        if graph.metadata(remote_id)["parentFolderId"] != item["id"]:
                            raise ValueError("Search match moved during readback")
                        ref = _store_mime(conn, account.account_id, item, remote_id, raw)
                    else:
                        from .store import upsert_email_message
                        if control.status(item["path"])["uidvalidity"] != epoch:
                            raise ValueError("UID epoch changed during remote search")
                        ref = upsert_email_message(conn, account_id=account.account_id, folder=item["path"],
                            folder_slug=sanitize_folder_name(item["path"]), uid=int(remote_id), uidvalidity=epoch,
                            message_id=headers["message_id"], subject=headers["subject"], sender=headers["from"],
                            recipient=headers["to"], date_iso=headers["date"], raw_sha256=digest, raw_eml=raw,
                            attachments=extract_attachments(raw), ingest_ts=now_iso())
                    conn.commit()
                    seen.add(digest)
                    result["messages"].append(get_message_by_storage_ref(conn, ref))
            except Exception as exc:
                result["ok"], result["search_complete"] = False, False
                result["errors"].append({"folder": item["path"], "reason": type(exc).__name__})
        return result
    finally:
        conn.close()
