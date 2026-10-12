"""Graph server review drafts, using Relay's existing request ledger and MIME."""
from __future__ import annotations

import base64
import hashlib
import re
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from urllib.parse import quote, urlencode

from .graph import GraphClient, GraphError
from .server_drafts import (_is_superseded, _ledger, _message_date,
                            _subject_key, content_snapshot)


def _schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS graph_server_drafts (
        request_id TEXT PRIMARY KEY, remote_id TEXT NOT NULL,
        verified_raw BLOB NOT NULL)""")


def _canonical(raw):
    snapshot = content_snapshot(raw)
    for name in ("From", "To", "Cc", "Bcc"):
        snapshot["headers"][name] = sorted(address.casefold() for _, address in
                                           getaddresses([snapshot["headers"][name]]))
    # MIME encodings/line endings and trailing text newlines may be rewritten
    # by Exchange. Preserve internal spacing, HTML, thread headers and all
    # attachment bytes; never use a fuzzy match to overwrite mobile edits.
    parts = []
    for part in BytesParser(policy=policy.default).parsebytes(raw).walk():
        if part.is_multipart():
            continue
        if part.get_content_maintype() == "text":
            text = part.get_content().replace("\r\n", "\n").rstrip("\n")
            if part.get_content_type() == "text/html":
                # Exchange inserts this charset-only head into an otherwise
                # unchanged HTML draft. Do not strip arbitrary heads, styles,
                # markup, internal whitespace, links or user-authored metadata.
                text = re.sub(r'\A<html>\s*<head>\s*<meta http-equiv="Content-Type" content="text/html; charset=utf-8">\s*</head>\s*',
                              "<html>", text, count=1)
            body = text.encode()
        else:
            body = part.get_payload(decode=True) or b""
        parts.append((part.get_content_type(), part.get_filename(),
                      part.get_content_disposition(), str(part.get("Content-ID", "")),
                      hashlib.sha256(body).hexdigest()))
    snapshot["parts"] = parts
    return snapshot


def _assert_draft(client, remote_id, expected, drafts_id):
    meta = client.request(f"/me/messages/{quote(remote_id, safe='')}?$select=id,isDraft,parentFolderId")
    if not meta.get("isDraft") or meta.get("parentFolderId") != drafts_id:
        raise ValueError("Message is not in server Drafts; do not recreate or resend")
    raw = client.mime(remote_id)
    if _canonical(raw) != _canonical(expected):
        raise ValueError("Server draft differs; preserve mobile edits and do not overwrite")
    return raw


def save_graph_draft(*, account, conn, row, raw, request_id, inspect):
    _schema(conn)
    client = GraphClient(account)
    client.identity()
    drafts_id = client.request("/me/mailFolders/drafts?$select=id")["id"]
    binding = conn.execute("SELECT remote_id,verified_raw FROM graph_server_drafts WHERE request_id=?", (request_id,)).fetchone()
    if binding:
        remote_id, baseline = binding
        verified = _assert_draft(client, remote_id, bytes(baseline), drafts_id)
    else:
        message_id = str(BytesParser(policy=policy.default).parsebytes(raw)["Message-ID"])
        query = urlencode({"$filter": "internetMessageId eq '" + message_id.replace("'", "''") + "'",
                           "$select": "id,isDraft,parentFolderId", "$top": "100"})
        matches = list(client.pages("/me/mailFolders/drafts/messages?" + query))
        if len(matches) > 1:
            raise ValueError("Multiple server drafts share the same request identity")
        if matches:
            remote_id = matches[0]["id"]
            verified = _assert_draft(client, remote_id, raw, drafts_id)
        else:
            if inspect or row["state"] != "prepared":
                raise ValueError("Graph draft absent or create outcome unknown; do not recreate")
            if not any(s in client.scopes for s in ("mail.readwrite", "https://graph.microsoft.com/mail.readwrite")):
                raise ValueError("Mail.ReadWrite consent required")
            conn.execute("UPDATE server_draft_requests SET state='attempted',folder='Drafts' WHERE request_id=?", (request_id,))
            conn.commit()  # Persist before POST; never send or retry an unknown create.
            conn.execute("BEGIN IMMEDIATE")
            created = client.request("/me/messages", method="POST", data=base64.b64encode(raw))
            remote_id = created["id"]
            conn.execute("INSERT INTO graph_server_drafts VALUES (?,?,?)", (request_id, remote_id, raw))
            conn.commit()  # Retain returned immutable identity even if readback fails.
            conn.execute("BEGIN IMMEDIATE")
            verified = _assert_draft(client, remote_id, raw, drafts_id)
        conn.execute("INSERT OR REPLACE INTO graph_server_drafts VALUES (?,?,?)", (request_id, remote_id, verified))
    conn.execute("UPDATE server_draft_requests SET state='verified',folder='Drafts' WHERE request_id=?", (request_id,))
    conn.commit()
    headers = content_snapshot(verified)["headers"]
    return {"request_id": request_id, "remote_id": remote_id, "folder": "Drafts",
            "server_verified": True, "state": "draft", "send_allowed": False,
            "message_id": headers["Message-ID"], "to": headers["To"], "cc": headers["Cc"],
            "subject": headers["Subject"]}


def reconcile_graph_drafts(*, account, ledger_path, apply=False):
    conn = _ledger(ledger_path)
    entries = []
    try:
        _schema(conn)
        rows = conn.execute("""SELECT r.request_id,g.remote_id,g.verified_raw
            FROM server_draft_requests r JOIN graph_server_drafts g USING(request_id)
            WHERE r.account_id=? AND r.state='verified'""", (account.account_id,)).fetchall()
        if not rows:
            return {"account_id": account.account_id, "checked": 0, "candidates": 0, "cleaned": 0, "drafts": []}
        client = GraphClient(account)
        client.identity()
        drafts_id = client.request("/me/mailFolders/drafts?$select=id")["id"]
        trash_id = client.request("/me/mailFolders/deleteditems?$select=id")["id"]
        for row in rows:
            entry = {"request_id": row["request_id"], "remote_id": row["remote_id"], "action": "keep"}
            entries.append(entry)
            try:
                meta = client.request(f"/me/messages/{quote(row['remote_id'], safe='')}?$select=id,isDraft,parentFolderId")
            except GraphError as exc:
                if exc.status != 404:
                    raise
                entry.update(action="absent", reason="draft no longer on server")
                continue
            if not meta.get("isDraft") or meta.get("parentFolderId") != drafts_id:
                entry.update(action="absent", reason="message is no longer in Drafts")
                continue
            expected = bytes(row["verified_raw"])
            raw = client.mime(row["remote_id"])
            if _canonical(raw) != _canonical(expected):
                entry.update(reason="mobile draft was edited; preserve edits", user_edited=True)
                continue
            draft = BytesParser(policy=policy.default).parsebytes(raw)
            since = _message_date(draft)
            params = {"$select": "id,subject", "$top": "100"}
            if since:
                params["$filter"] = "sentDateTime ge " + (since - timedelta(days=2)).isoformat()
            found = None
            for sent_meta in client.pages("/me/mailFolders/sentitems/messages?" + urlencode(params)):
                if _subject_key(sent_meta.get("subject", "")) != _subject_key(str(draft.get("Subject", ""))):
                    continue
                sent = BytesParser(policy=policy.default).parsebytes(client.mime(sent_meta["id"]))
                if _is_superseded(draft, sent):
                    found = str(sent.get("Message-ID", ""))
                    break
            if not found:
                entry.update(reason="no exact sent reply covers this draft")
                continue
            entry.update(action="candidate", reason="an exact sent reply covers this draft", sent_message_id=found)
            if apply:
                # Live content and Drafts membership are rechecked at the write.
                _assert_draft(client, row["remote_id"], expected, drafts_id)
                moved = client.request(f"/me/messages/{quote(row['remote_id'], safe='')}/move", method="POST", data={"destinationId": trash_id})
                target_id = moved.get("id")
                if not target_id:
                    raise ValueError("Draft cleanup identity is missing")
                after = client.metadata(target_id)
                query = urlencode({"$filter": "internetMessageId eq '" + str(draft["Message-ID"]).replace("'", "''") + "'",
                                   "$select": "id", "$top": "100"})
                if (after.get("parentFolderId") != trash_id or
                        _canonical(client.mime(target_id)) != _canonical(expected) or
                        list(client.pages("/me/mailFolders/drafts/messages?" + query))):
                    raise ValueError("Draft cleanup outcome uncertain; reconcile before retry")
                conn.execute("UPDATE server_draft_requests SET state='cleaned' WHERE request_id=?", (row["request_id"],))
                conn.commit()
                entry["action"] = "cleaned"
        return {"account_id": account.account_id, "checked": len(entries),
                "candidates": sum(e["action"] in {"candidate", "cleaned"} for e in entries),
                "cleaned": sum(e["action"] == "cleaned" for e in entries), "drafts": entries}
    finally:
        conn.close()
