"""Mailbox names and roles shared by transport, sync and local queries."""
from __future__ import annotations

import base64
import hashlib
import re


def normalize_folder(name: str) -> str:
    """Accept persisted IMAP modified UTF-7 names at the adapter boundary."""
    def decode(match):
        payload = match[1]
        if not payload:
            return "&"
        try:
            raw = base64.b64decode(payload.replace(",", "/") + "=" * (-len(payload) % 4), validate=True)
            return raw.decode("utf-16-be")
        except (ValueError, UnicodeError):
            return match[0]
    return re.sub(r"&([A-Za-z0-9+,]*)-", decode, name)


def legacy_folder_slug(folder: str) -> str:
    text = folder.strip()
    if not text:
        return "UNKNOWN"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text.replace("\\", "_").replace("/", "__")).strip("._")
    return cleaned or "folder_" + hashlib.sha1(text.encode()).hexdigest()[:10]


def folder_slug(folder: str) -> str:
    # Callers supply the canonical name from LIST; literal '&...-' names
    # must not be mistaken for a persisted wire-encoded alias.
    name = folder
    cleaned = legacy_folder_slug(name)
    # Retain unchanged ASCII identities; every lossy name gets its own suffix.
    if cleaned == name:
        return cleaned
    return cleaned + "--" + hashlib.sha256(name.encode()).hexdigest()[:16]


def folder_role(name: str, flags=()) -> str:
    roles = {str(flag).casefold().lstrip("\\") for flag in flags}
    for role in ("trash", "junk", "drafts", "sent", "all", "archive", "inbox"):
        if role in roles:
            return role
    return {
        "inbox": "inbox", "sent": "sent", "sent items": "sent", "sent messages": "sent",
        "[gmail]/sent mail": "sent", "archive": "archive", "archives": "archive",
        "[gmail]/all mail": "all", "trash": "trash", "deleted items": "trash",
        "deleted messages": "trash", "bin": "trash", "[gmail]/trash": "trash",
        "junk": "junk", "junk email": "junk", "junk e-mail": "junk", "spam": "junk",
        "[gmail]/spam": "junk", "drafts": "drafts", "[gmail]/drafts": "drafts",
    }.get(normalize_folder(name).casefold(), "")


def folder_in_scope(name: str, scope: str, flags=()) -> bool:
    if scope == "all":
        return True
    role = folder_role(name, flags)
    if role in {"trash", "junk", "drafts"}:
        return False
    if role in {"inbox", "sent"}:
        return True
    name = normalize_folder(name).casefold()
    return scope == "history" and (role in {"archive", "all"} or name == "bill" or
        name.startswith(("archive/", "archives/", "bill/", "bill ")))
