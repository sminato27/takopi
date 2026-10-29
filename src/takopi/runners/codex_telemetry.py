"""Read only the current turn's status from its exact native Codex rollout."""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from ..telemetry import Quota, Telemetry, number, timestamp

_SESSION_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")
_READ_LIMIT = 1024 * 1024


class CodexTelemetryReader:
    def __init__(self, *, home: Path | None = None, since: float | None = None):
        self.home = home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        self.since = time.time() if since is None else since
        self.path: Path | None = None
        self.offset = 0
        self.status = Telemetry()

    def _locate(self, session_id: str) -> Path | None:
        if not _SESSION_ID.fullmatch(session_id):
            return None
        databases = sorted(self.home.glob("state_*.sqlite"), reverse=True)
        for database in databases:
            try:
                with closing(
                    sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
                ) as db:
                    row = db.execute(
                        "SELECT rollout_path FROM threads WHERE id=?", (session_id,)
                    ).fetchone()
                if row and Path(row[0]).is_file():
                    return Path(row[0])
            except (sqlite3.Error, ValueError, TypeError):
                continue
        for folder in ("sessions", "archived_sessions"):
            for path in (self.home / folder).glob(f"**/*-{session_id}.jsonl"):
                return path
        return None

    def read(self, session_id: str) -> Telemetry:
        try:
            if self.path is None:
                path = self._locate(session_id)
                if path is None:
                    return self.status
                # Do not trust a stale/misassociated index row.
                with path.open("rb") as stream:
                    first = json.loads(stream.readline(_READ_LIMIT))
                if first.get("payload", {}).get("id") != session_id:
                    return self.status
                self.path = path
            with self.path.open("rb") as stream:
                size = stream.seek(0, 2)
                start = max(self.offset, size - _READ_LIMIT)
                if start > size:
                    start = 0
                stream.seek(start)
                if start > self.offset:
                    stream.readline()  # discard a partial first record
                while line := stream.readline(_READ_LIMIT + 1):
                    if len(line) > _READ_LIMIT:
                        # Oversized content records are not telemetry. Skip to
                        # their end in bounded chunks instead of getting stuck.
                        while not line.endswith(b"\n"):
                            line = stream.readline(_READ_LIMIT)
                            if not line:
                                break
                        self.offset = stream.tell()
                        continue
                    if not line.endswith(b"\n"):
                        break  # retry an incomplete record on the next event
                    self.offset = stream.tell()
                    try:
                        self._consume(json.loads(line))
                    except (ValueError, TypeError, AttributeError):
                        continue
        except (OSError, ValueError, TypeError, AttributeError):
            return self.status
        return self.status

    def _consume(self, record: dict) -> None:
        captured = timestamp(record.get("timestamp"))
        if captured is None or captured < self.since:
            return
        payload = record.get("payload", {})
        if record.get("type") == "turn_context":
            model, effort = payload.get("model"), payload.get("effort")
            self.status = replace(
                self.status,
                model=model if isinstance(model, str) else None,
                effort=effort if isinstance(effort, str) else None,
            )
        elif record.get("type") == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info") or {}
            last = info.get("last_token_usage") or {}
            tokens = number(last.get("total_tokens"))
            window = number(info.get("model_context_window"))
            if tokens is not None:
                self.status = replace(self.status, context_tokens=int(tokens))
            if window is not None and window > 0:
                self.status = replace(self.status, context_window=int(window))
            limits = payload.get("rate_limits") or {}
            for key in ("primary", "secondary"):
                bucket = limits.get(key) or {}
                duration = bucket.get("window_minutes")
                field = (
                    {300: "five_hour", 10080: "weekly"}.get(duration)
                    if isinstance(duration, int)
                    else None
                )
                if field:
                    quota = Quota.from_values(
                        bucket.get("used_percent"), bucket.get("resets_at")
                    )
                    self.status = replace(self.status, **{field: quota})
