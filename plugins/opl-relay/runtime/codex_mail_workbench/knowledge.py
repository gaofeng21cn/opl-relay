from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from .config import load_toml


@dataclass(frozen=True)
class KnowledgeSource:
    source_id: str
    kind: str
    path: Path
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    max_bytes: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_sources_config(path: Path) -> dict[str, KnowledgeSource]:
    if not path.exists():
        return {}
    data = load_toml(path)
    raw_sources = data.get("sources", [])
    if not isinstance(raw_sources, list):
        raise ValueError("sources must be a list")
    sources: dict[str, KnowledgeSource] = {}
    for index, value in enumerate(raw_sources):
        if not isinstance(value, dict):
            raise ValueError(f"sources[{index}] must be an object")
        if value.get("enabled", True) is False:
            continue
        source_id = str(value.get("source_id") or "").strip()
        kind = str(value.get("type") or "").strip().lower()
        raw_path = str(value.get("path") or "").strip()
        if not source_id:
            raise ValueError(f"sources[{index}].source_id is required")
        if source_id in sources:
            raise ValueError(f"duplicate source_id: {source_id}")
        if kind != "obsidian":
            raise ValueError(f"unsupported knowledge source type: {kind}")
        if not raw_path:
            raise ValueError(f"sources[{index}].path is required")
        source_path = Path(raw_path).expanduser()
        if not source_path.is_absolute():
            source_path = (path.parent / source_path).resolve()
        include = value.get("include", ["*.md", "**/*.md"])
        exclude = value.get(
            "exclude",
            [".obsidian/**", ".trash/**", "**/.obsidian/**", "**/.trash/**"],
        )
        if not isinstance(include, list) or not all(isinstance(item, str) for item in include):
            raise ValueError(f"sources[{index}].include must be a list of strings")
        if not isinstance(exclude, list) or not all(isinstance(item, str) for item in exclude):
            raise ValueError(f"sources[{index}].exclude must be a list of strings")
        max_bytes = value.get("max_bytes", 2_000_000)
        if not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError(f"sources[{index}].max_bytes must be a positive integer")
        sources[source_id] = KnowledgeSource(
            source_id=source_id,
            kind=kind,
            path=source_path,
            include=tuple(include),
            exclude=tuple(exclude),
            max_bytes=max_bytes,
        )
    return sources


def _glob_regex(pattern: str) -> re.Pattern[str]:
    pieces = ["^"]
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    pieces.append("(?:.*/)?")
                    index += 1
                else:
                    pieces.append(".*")
                continue
            pieces.append("[^/]*")
        elif char == "?":
            pieces.append("[^/]")
        else:
            pieces.append(re.escape(char))
        index += 1
    pieces.append("$")
    return re.compile("".join(pieces))


def _matches(relative_path: str, patterns: Iterable[str]) -> bool:
    return any(_glob_regex(pattern).match(relative_path) for pattern in patterns)


def _title(path: Path, content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return path.stem


def _excerpt(content: str, query: str, *, max_chars: int = 600) -> str:
    compact = " ".join(content.split())
    if len(compact) <= max_chars:
        return compact
    position = compact.casefold().find(query.casefold())
    if position < 0:
        return compact[: max_chars - 1] + "..."
    start = max(0, position - max_chars // 3)
    end = min(len(compact), start + max_chars)
    prefix = "..." if start else ""
    suffix = "..." if end < len(compact) else ""
    return prefix + compact[start:end] + suffix


class KnowledgeIndex:
    """Derived local index for read-only external knowledge providers."""

    def __init__(self, path: Path):
        self.path = path

    def _connect(self, *, create: bool) -> sqlite3.Connection | None:
        if not create and not self.path.exists():
            return None
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=30)
        else:
            conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        if create:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_documents (
                  source_id TEXT NOT NULL,
                  relative_path TEXT NOT NULL,
                  source_ref TEXT NOT NULL UNIQUE,
                  title TEXT NOT NULL,
                  content TEXT NOT NULL,
                  content_sha256 TEXT NOT NULL,
                  mtime_ns INTEGER NOT NULL,
                  indexed_at TEXT NOT NULL,
                  PRIMARY KEY (source_id, relative_path)
                );

                CREATE INDEX IF NOT EXISTS idx_knowledge_source
                ON knowledge_documents(source_id, relative_path);
                """
            )
            conn.commit()
        return conn

    def index_source(self, source: KnowledgeSource) -> dict[str, Any]:
        if not source.path.is_dir():
            raise FileNotFoundError(f"knowledge source directory not found: {source.path}")
        conn = self._connect(create=True)
        assert conn is not None
        seen: set[str] = set()
        indexed = 0
        unchanged = 0
        skipped = 0
        try:
            for path in sorted(source.path.rglob("*.md")):
                if not path.is_file() or path.is_symlink():
                    skipped += 1
                    continue
                relative = path.relative_to(source.path).as_posix()
                if not _matches(relative, source.include) or _matches(
                    relative, source.exclude
                ):
                    continue
                if path.stat().st_size > source.max_bytes:
                    skipped += 1
                    continue
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    skipped += 1
                    continue
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                seen.add(relative)
                existing = conn.execute(
                    """
                    SELECT content_sha256 FROM knowledge_documents
                    WHERE source_id=? AND relative_path=?
                    """,
                    (source.source_id, relative),
                ).fetchone()
                if existing and str(existing["content_sha256"]) == digest:
                    unchanged += 1
                    continue
                source_ref = f"obsidian://{source.source_id}/{quote(relative)}"
                conn.execute(
                    """
                    INSERT INTO knowledge_documents (
                      source_id, relative_path, source_ref, title, content,
                      content_sha256, mtime_ns, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id, relative_path) DO UPDATE SET
                      source_ref=excluded.source_ref,
                      title=excluded.title,
                      content=excluded.content,
                      content_sha256=excluded.content_sha256,
                      mtime_ns=excluded.mtime_ns,
                      indexed_at=excluded.indexed_at
                    """,
                    (
                        source.source_id,
                        relative,
                        source_ref,
                        _title(path, content),
                        content,
                        digest,
                        path.stat().st_mtime_ns,
                        utc_now(),
                    ),
                )
                indexed += 1
            current = conn.execute(
                "SELECT relative_path FROM knowledge_documents WHERE source_id=?",
                (source.source_id,),
            ).fetchall()
            stale = [str(row["relative_path"]) for row in current if row["relative_path"] not in seen]
            for relative in stale:
                conn.execute(
                    "DELETE FROM knowledge_documents WHERE source_id=? AND relative_path=?",
                    (source.source_id, relative),
                )
            conn.commit()
            return {
                "source_id": source.source_id,
                "type": source.kind,
                "path": str(source.path),
                "scanned": len(seen),
                "indexed": indexed,
                "unchanged": unchanged,
                "deleted": len(stale),
                "skipped": skipped,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def source_counts(self) -> dict[str, int]:
        conn = self._connect(create=False)
        if conn is None:
            return {}
        try:
            return {
                str(row["source_id"]): int(row["count"])
                for row in conn.execute(
                    """
                    SELECT source_id, COUNT(*) AS count
                    FROM knowledge_documents GROUP BY source_id ORDER BY source_id
                    """
                ).fetchall()
            }
        except sqlite3.OperationalError:
            return {}
        finally:
            conn.close()

    def search(
        self,
        query: str,
        *,
        source_ids: Iterable[str] = (),
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        query = " ".join(query.split())
        if not query:
            return []
        conn = self._connect(create=False)
        if conn is None:
            return []
        try:
            source_values = tuple(dict.fromkeys(source_ids))
            where = ""
            params: list[Any] = []
            if source_values:
                where = f"WHERE source_id IN ({','.join('?' for _ in source_values)})"
                params.extend(source_values)
            try:
                rows = conn.execute(
                    f"""
                    SELECT source_id, relative_path, source_ref, title, content,
                           content_sha256, indexed_at
                    FROM knowledge_documents {where}
                    """,
                    params,
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            key = query.casefold()
            matches: list[tuple[int, sqlite3.Row]] = []
            for row in rows:
                title = str(row["title"])
                content = str(row["content"])
                title_hits = title.casefold().count(key)
                content_hits = content.casefold().count(key)
                if not title_hits and not content_hits:
                    continue
                matches.append((title_hits * 10 + content_hits, row))
            matches.sort(
                key=lambda item: (
                    -item[0],
                    str(item[1]["source_id"]),
                    str(item[1]["relative_path"]),
                )
            )
            return [
                {
                    "source_id": str(row["source_id"]),
                    "source_ref": str(row["source_ref"]),
                    "path": str(row["relative_path"]),
                    "title": str(row["title"]),
                    "excerpt": _excerpt(str(row["content"]), query),
                    "content_sha256": str(row["content_sha256"]),
                    "indexed_at": str(row["indexed_at"]),
                }
                for _, row in matches[: max(1, min(int(limit), 100))]
            ]
        finally:
            conn.close()
