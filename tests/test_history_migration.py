import json
from pathlib import Path
import runpy


migration = runpy.run_path(
    str(Path(__file__).parents[1] / "scripts/migrate_claude_histories.py")
)


def test_windows_paths_are_metadata_only_and_conflicts_are_preserved(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    folder = source / "projects/old"
    folder.mkdir(parents=True)
    old = r"C:\Users\Owner\project"
    payload = {"cwd": old, "sessionId": "sid", "message": {"content": old}}
    original = (json.dumps(payload) + "\ninvalid tail\n").encode()
    (folder / "sid.jsonl").write_bytes(original)
    (folder / "settings.json").write_text("keep old")
    output = target / "projects/-mnt-c-Users-Owner-project"
    output.mkdir(parents=True)
    (output / "settings.json").write_text("keep current")
    dry = migration["migrate"](source, target)
    assert not (output / "sid.jsonl").exists()
    assert {r["status"] for r in dry} == {"copy", "conflict"}
    migration["migrate"](source, target, apply=True)
    record = json.loads((output / "sid.jsonl").read_text().splitlines()[0])
    assert record["cwd"] == "/mnt/c/Users/Owner/project"
    assert record["message"]["content"] == old
    assert record["sessionId"] == "sid"
    assert (folder / "sid.jsonl").read_bytes() == original
    assert (output / "sid.jsonl").read_bytes().endswith(b"invalid tail\n")
    assert (output / "settings.json").read_text() == "keep current"
    assert {r["status"] for r in migration["migrate"](source, target, apply=True)} == {
        "identical",
        "conflict",
    }


def test_explicit_project_copy_mapping_changes_metadata_without_rewriting_messages():
    old = r"C:\worktree"
    data = (json.dumps({"cwd": old, "message": {"content": old}}) + "\n").encode()
    result = json.loads(
        migration["converted"](data, {"/mnt/c/worktree": "/home/me/project"})
    )
    assert result["cwd"] == "/home/me/project"
    assert result["message"]["content"] == old


def test_mixed_workspace_sessions_and_subagents_keep_their_workspace(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    folder = source / "projects/old"
    folder.mkdir(parents=True)
    for sid, cwd in [("one", "/workspace/one"), ("two", "/workspace/two")]:
        (folder / f"{sid}.jsonl").write_text(json.dumps({"cwd": cwd}) + "\n")
        sub = folder / sid / "subagents/agent.jsonl"
        sub.parent.mkdir(parents=True)
        sub.write_text(json.dumps({"cwd": cwd}) + "\n")
    migration["migrate"](source, target, apply=True)
    for sid in ["one", "two"]:
        dest = target / f"projects/-workspace-{sid}"
        assert (dest / f"{sid}.jsonl").is_file()
        assert (dest / sid / "subagents/agent.jsonl").is_file()
