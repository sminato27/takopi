import json
import os
import textwrap
from pathlib import Path

import pytest

from takopi.config import ConfigError
from takopi.managed import AiMemoryConfig
from takopi.model import CompletedEvent, StartedEvent
from takopi.runners.claude import build_runner as build_claude
from takopi.runners.codex import build_runner as build_codex
from takopi.utils.paths import reset_run_base_dir, set_run_base_dir


@pytest.fixture
def managed_cli(tmp_path, monkeypatch):
    executable = tmp_path / "ai-memory"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        + textwrap.dedent("""
        import json, os, pathlib, sys, time, uuid
        args = sys.argv[1:]
        root = pathlib.Path(os.environ['FAKE_MEMORY_ROOT'])
        selector = '--new' if '--new' in args else '--workstream'
        workstream = args[args.index(selector) + 1]
        engine = args[args.index(selector) + 2]
        saved = root / (workstream + '.json')
        prompt = sys.stdin.read()
        if selector == '--new':
            native_selector = '--resume' if engine == 'claude' else 'resume'
            session = args[args.index(native_selector) + 1] if native_selector in args else str(uuid.uuid4())
        else:
            session = json.loads(saved.read_text())['session']
        payload = dict(args=args, cwd=os.getcwd(), session=session, prompt=prompt,
                       preserved=os.environ.get('PRESERVED_CONFIG'))
        saved.write_text(json.dumps(payload))
        if engine == 'codex':
            events = [dict(type='thread.started', thread_id=session),
                      dict(type='item.completed', item=dict(id='a', type='agent_message', text=prompt)),
                      dict(type='turn.completed', usage=dict(input_tokens=1, cached_input_tokens=0, output_tokens=1))]
        else:
            events = [dict(type='system', subtype='init', session_id=session,
                           permissionMode=os.environ.get('FAKE_PERMISSION_MODE','auto')),
                      dict(type='result', subtype='success', session_id=session, result=prompt,
                           is_error=False, duration_ms=1, duration_api_ms=1, num_turns=1)]
        for event in events:
            print(json.dumps(event), flush=True)
        time.sleep(0.03)
        (root / 'finished').write_text('imported')
        sys.exit(int(os.environ.get('FAKE_EXIT_CODE', '0')))
    """)
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_MEMORY_ROOT", str(tmp_path))
    monkeypatch.setenv("PRESERVED_CONFIG", "keep-me")
    return tmp_path


def runner_for(engine, directory):
    config = {"ai_memory": True}
    if engine == "codex":
        config.update(mode="exec", extra_args=[])
        return build_codex(config, directory / "takopi.toml")
    return build_claude(config, directory / "takopi.toml")


@pytest.mark.anyio
@pytest.mark.parametrize("engine", ["codex", "claude"])
async def test_managed_new_resume_and_restart_preserve_identity(managed_cli, engine):
    directory = managed_cli
    marker = set_run_base_dir(directory)
    try:
        runner = runner_for(engine, directory)
        events = [event async for event in runner.run("hello; $(touch NEVER)", None)]
        started = next(e for e in events if isinstance(e, StartedEvent))
        assert isinstance(events[-1], CompletedEvent)
        assert events[-1].ok
        assert events[-1].answer == "hello; $(touch NEVER)"
        assert not (directory / "NEVER").exists()
        record = json.loads(next(directory.glob("tg-*.json")).read_text())
        assert record["cwd"] == str(directory)
        assert record["preserved"] == "keep-me"
        assert "--require-server" in record["args"]
        assert "--no-autowire" in record["args"]
        if engine == "claude":
            assert "--permission-mode" in record["args"]
            assert "--allowedTools" not in record["args"]
            assert "--dangerously-skip-permissions" not in record["args"]
        else:
            assert "--yolo" in record["args"]
        runner = runner_for(engine, directory)
        resumed = [event async for event in runner.run("second", started.resume)]
        assert resumed[-1].resume == started.resume
        assert len(list(directory.glob("tg-*.json"))) == 1
        record = json.loads(next(directory.glob("tg-*.json")).read_text())
        assert "--workstream" in record["args"]
        assert "--new" not in record["args"]
        fresh = [event async for event in runner.run("independent", None)]
        assert fresh[-1].resume != started.resume
        assert len(list(directory.glob("tg-*.json"))) == 2
    finally:
        reset_run_base_dir(marker)


@pytest.mark.anyio
async def test_completion_waits_for_memory_finalization_and_reports_failure(
    managed_cli, monkeypatch
):
    monkeypatch.setenv("FAKE_EXIT_CODE", "7")
    runner = runner_for("codex", managed_cli)
    async for event in runner.run("hello", None):
        if isinstance(event, CompletedEvent):
            assert (managed_cli / "finished").exists()
            assert not event.ok
            assert "code 7" in event.error


@pytest.mark.anyio
async def test_resume_rejects_changed_cwd_or_config_home(managed_cli, monkeypatch):
    runner = runner_for("codex", managed_cli)
    marker = set_run_base_dir(managed_cli)
    try:
        events = [e async for e in runner.run("first", None)]
        resume = events[-1].resume
        monkeypatch.setenv("CODEX_HOME", str(managed_cli / "different-config"))
        with pytest.raises(ConfigError, match="no ai-memory binding"):
            _ = [e async for e in runner.run("must not execute", resume)]
        monkeypatch.delenv("CODEX_HOME")
        other = managed_cli / "other"
        other.mkdir()
        other_marker = set_run_base_dir(other)
        try:
            with pytest.raises(ConfigError, match="no ai-memory binding"):
                _ = [e async for e in runner.run("must not execute", resume)]
        finally:
            reset_run_base_dir(other_marker)
        assert (
            json.loads(next(managed_cli.glob("tg-*.json")).read_text())["prompt"]
            == "first"
        )
    finally:
        reset_run_base_dir(marker)


@pytest.mark.anyio
async def test_claude_wrong_effective_mode_stops_run(managed_cli, monkeypatch):
    monkeypatch.setenv("FAKE_PERMISSION_MODE", "bypassPermissions")
    runner = runner_for("claude", managed_cli)
    with pytest.RaisesGroup(
        pytest.RaisesExc(RuntimeError, match="did not start in auto")
    ):
        _ = [event async for event in runner.run("hello", None)]


def test_incompatible_configuration_is_rejected(managed_cli):
    with pytest.raises(ConfigError, match="app-server"):
        build_codex({"ai_memory": True}, managed_cli / "takopi.toml")
    with pytest.raises(ConfigError, match="require permission_mode"):
        build_claude(
            {"ai_memory": True, "dangerously_skip_permissions": True},
            managed_cli / "takopi.toml",
        )
    with pytest.raises(ConfigError, match="true or false"):
        AiMemoryConfig.from_config({"ai_memory": "yes"}, Path("test.toml"))


@pytest.mark.anyio
@pytest.mark.parametrize("engine", ["codex", "claude"])
async def test_imported_native_session_is_adopted_then_resumed(
    managed_cli, monkeypatch, engine
):
    from takopi.model import ResumeToken

    monkeypatch.setattr("takopi.native_sessions.native_directory_busy", lambda _: False)
    runner = runner_for(engine, managed_cli)
    resume = ResumeToken(engine, "11111111-2222-4333-8444-555555555555")
    marker = set_run_base_dir(managed_cli)
    try:
        with pytest.raises(ConfigError, match="no ai-memory binding"):
            _ = [e async for e in runner.run("no", resume)]
        runner.ai_memory.allow_import(engine, resume.value, managed_cli)
        events = [e async for e in runner.run("imported", resume)]
        assert events[-1].ok and events[-1].resume == resume
        assert (
            "--new"
            in json.loads(next(managed_cli.glob("tg-*.json")).read_text())["args"]
        )
        events = [e async for e in runner_for(engine, managed_cli).run("again", resume)]
        assert events[-1].ok and events[-1].resume == resume
        assert (
            "--workstream"
            in json.loads(next(managed_cli.glob("tg-*.json")).read_text())["args"]
        )
        monkeypatch.setattr(
            "takopi.native_sessions.native_directory_busy", lambda _: True
        )
        with pytest.raises(ConfigError, match="CLI ativo"):
            _ = [e async for e in runner.run("must not execute", resume)]
    finally:
        reset_run_base_dir(marker)
