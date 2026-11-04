"""Read local CLI histories without changing their stores or authentication."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class NativeSession:
    engine: str
    session_id: str
    cwd: Path | None
    source: Path
    title: str
    modified: float


def same_directory(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return left == right


def native_sessions() -> list[NativeSession]:
    rows: dict[tuple[str, str], NativeSession] = {}
    codex = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    databases = sorted(
        codex.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if databases:
        try:
            with closing(
                sqlite3.connect(databases[0].resolve().as_uri() + "?mode=ro", uri=True)
            ) as db:
                for sid, cwd, source, title, modified in db.execute(
                    "SELECT id,cwd,rollout_path,title,updated_at FROM threads"
                ):
                    rows["codex", sid] = NativeSession(
                        "codex", sid, Path(cwd), Path(source), title, modified
                    )
        except sqlite3.Error:
            pass
    # Include rollouts absent from the index, including archived histories.
    for folder in (codex / "sessions", codex / "archived_sessions"):
        for path in folder.rglob("*.jsonl"):
            try:
                with path.open() as stream:
                    record = json.loads(stream.readline())
                meta = record.get("payload", {})
                sid, cwd = meta.get("id"), meta.get("cwd")
                if record.get("type") == "session_meta" and sid and cwd:
                    rows.setdefault(
                        ("codex", sid),
                        NativeSession(
                            "codex", sid, Path(cwd), path, sid, path.stat().st_mtime
                        ),
                    )
            except (OSError, ValueError):
                continue
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    for path in (claude / "projects").glob("*/*.jsonl"):
        try:
            cwd = None
            title = None
            with path.open() as stream:
                for index, line in enumerate(stream):
                    if index >= 200:
                        break
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    cwd = cwd or record.get("cwd")
                    if record.get("type") == "user":
                        content = record.get("message", {}).get("content")
                        if isinstance(content, list):
                            content = " ".join(
                                block.get("text", "")
                                for block in content
                                if isinstance(block, dict)
                            )
                        if (
                            isinstance(content, str)
                            and content.strip()
                            and not content.lstrip().startswith("<")
                        ):
                            title = " ".join(content.split())[:100]
                    if cwd and title:
                        break
            if cwd:
                rows["claude", path.stem] = NativeSession(
                    "claude",
                    path.stem,
                    Path(cwd),
                    path,
                    title or path.stem,
                    path.stat().st_mtime,
                )
        except OSError:
            continue
    for row in agy_sessions():
        rows[row.engine, row.session_id] = row
    return sorted(rows.values(), key=lambda row: row.modified, reverse=True)


def agy_sessions() -> list[NativeSession]:
    root = Path.home() / ".gemini/antigravity-cli"
    database = root / "conversation_summaries.db"
    if not database.is_file():
        return []
    result = []
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            for sid, title, uris, modified in db.execute(
                "SELECT conversation_id,title,workspace_uris,last_modified_time FROM conversation_summaries"
            ):
                cwd = None
                try:
                    paths = json.loads(uris or "[]")
                    if (
                        isinstance(paths, list)
                        and len(paths) == 1
                        and isinstance(paths[0], str)
                    ):
                        uri = urlparse(paths[0])
                        if uri.scheme == "file" and uri.netloc in {"", "localhost"}:
                            cwd = Path(unquote(uri.path))
                    timestamp = datetime.fromisoformat(modified).timestamp()
                except (TypeError, ValueError):
                    timestamp = database.stat().st_mtime
                result.append(
                    NativeSession(
                        "agy",
                        sid,
                        cwd,
                        root / "conversations" / f"{sid}.db",
                        title or sid,
                        timestamp,
                    )
                )
    except (OSError, sqlite3.Error):
        return []
    return result


def native_directory_busy(cwd: Path) -> bool:
    """Conservatively refuse a checkout occupied by an interactive native CLI."""
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            args = (proc / "cmdline").read_bytes().split(b"\0")
            if not args or Path(os.fsdecode(args[0])).name not in {
                "codex",
                "claude",
                "agy",
                "kiro-cli",
            }:
                continue
            if same_directory(proc / "cwd", cwd):
                return True
        except (OSError, ValueError):
            continue
    return False
