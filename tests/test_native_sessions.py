import json
import sqlite3
from contextlib import closing

from takopi.native_sessions import native_sessions


def test_native_catalog_merges_index_rollouts_and_claude_without_writes(
    tmp_path, monkeypatch
):
    codex, claude = tmp_path / "codex", tmp_path / "claude"
    codex.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    dbpath = codex / "state_5.sqlite"
    with closing(sqlite3.connect(dbpath)) as db, db:
        db.execute(
            "CREATE TABLE threads(id TEXT,cwd TEXT,rollout_path TEXT,title TEXT,updated_at INTEGER)"
        )
        db.execute(
            "INSERT INTO threads VALUES (?,?,?,?,?)",
            ("indexed", str(tmp_path), "missing-rollout", "saved", 10),
        )
    for folder, sid in [("sessions", "indexed"), ("archived_sessions", "archived")]:
        target = codex / folder / "history.jsonl"
        target.parent.mkdir()
        target.write_text(
            json.dumps(
                {"type": "session_meta", "payload": {"id": sid, "cwd": str(tmp_path)}}
            )
            + "\n"
        )
    target = claude / "projects/project/session.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text(
        "invalid\n" + json.dumps({"cwd": str(tmp_path / "unavailable")}) + "\n"
    )
    before = dbpath.read_bytes()
    rows = native_sessions()
    assert {(r.engine, r.session_id) for r in rows} == {
        ("codex", "indexed"),
        ("codex", "archived"),
        ("claude", "session"),
    }
    assert next(r for r in rows if r.session_id == "indexed").title == "saved"
    assert dbpath.read_bytes() == before
